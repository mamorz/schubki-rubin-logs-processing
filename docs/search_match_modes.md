# Search match modes

`search_logs` adds optional `match_mode` with default `any`. Existing calls still
use the original full-text predicate and normalization. The input schema requires
only `dataset`; omitting `query` is subject to the structured-filter rules below.

| Mode | Meaning | Parameterized predicate / relation |
| --- | --- | --- |
| `any` | Existing OR-based full-text behavior: any query token | `message @@ %(text)s` on the existing inverted index |
| `phrase` | Consecutive tokens in the supplied order, anywhere in the message | `message @@ ts_phrase(%(text)s)` on the existing inverted index |
| `exact` | Equality with the entire stored `message` field | `message = %(text)s` on the base table |
| `all` | Every query token, in any order and without requiring adjacency | `message @@ plainto_tsquery(%(text)s)` on the existing inverted index |

Phrase matching uses `log_dict` normalization: lowercasing and tokenization with
positions already enabled in the existing indexes. For example, `generating core`
can match `generating core.304`; reversed tokens or intervening tokens do not
match. Punctuation/spacing differences can disappear during tokenization. This
uses the [SereneDB full-text functions](https://serenedb.com/docs/sql/functions/search/full-text#ts_phrase),
verified against the installed **26.09.1.3**, without rebuilding any index.

Exact matching does not trim, lowercase, tokenize, or perform substring matching.
Copy the returned `message` verbatim; this field excludes the log prefix parsed
into timestamp/component/etc. Empty and whitespace-only messages are supported
in exact mode. Non-string values and NUL characters are rejected. Full-text modes
continue to trim query edges and reject empty/whitespace-only input.

Examples of MCP arguments:

```json
{"dataset":"bgl","query":"generating core","match_mode":"phrase","limit":2}
```

```json
{"dataset":"bgl","query":"generating core.304","match_mode":"exact","limit":2}
```

Time, component, and severity filters and chronological ordering apply in every
mode. `total_corpus_match_count` comes from a separate COUNT query with the same
predicate and filters and **no LIMIT**. The records query fetches one extra row
to establish truncation. All existing response fields remain; `match_mode` is
added at the top level and in `provenance.applied_filters`. The legacy
`full_text_query` provenance field also holds the literal message in exact mode.
Executed records/count SQL, their bound parameters, returned count, and
`truncated` remain available.

## BGL filter-only search

For BGL, omit `query` or supply null to retrieve records regardless of message
text. Require both `start_time` and `end_time`, with start strictly before end,
and at least one nonempty `component` or `severity` filter. Both filters can be
combined. Time-only, component-only, severity-only and open-ended filter-only
requests are rejected. This applies to both SQL and sample BGL backends.

The public parameter is **`severity`**, mapped to the SQL `level` column; there
is no `level` parameter. For example, drill into the MMCS spike using:

```json
{"dataset":"bgl","component":"MMCS","severity":"ERROR","start_time":"2005-09-20T12:00:00","end_time":"2005-09-20T13:00:00","limit":50}
```

Bounds remain timezone-naive, start inclusive/end exclusive. Filter-only SQL
uses the base table with bound equality/time predicates and no message predicate
or inverted-index lookup. Counts cover all matches; only returned evidence is
limited (default 20, maximum 50), ordered by timestamp then stable record ID.
`provenance.applied_filters.full_text_query` is null. `match_mode` retains its
validated value but has no effect without text. Empty messages remain eligible.

A supplied text query retains all four matching modes and existing validation:
blank token queries are rejected, while an explicit empty exact query searches
only empty messages. OpenStack still permits omitted text with a literal
`request_id` or `instance_id`. No schema fields or successful response fields
change. MCP query sessions retain their configured SQL execution deadlines.

Pass a spike finding's component and time bounds directly; a contributor's
`severity` selects that severity only. Omitting severity returns all levels for
the component/window, which may exceed the detector's selected-record count.
Likewise, a single-severity query does not represent a multi-severity spike's
entire selected population. Returned `record_id` values support
`get_nearby_events` with `dataset="bgl"`.

The real filter tests uncovered two pre-existing SereneDB 26.09 issues: binary
datetime parameters caused `Invalid unicode`, and index timestamp predicates
included out-of-range rows even with literal timestamps. Search timestamps now
use psycopg's `%t` bound parameter format (typed text wire format). When either
time bound is present, full-text search selects candidate IDs from the existing
index, then applies filters to base-table rows through an `IN (SELECT ...)`
predicate. Both records and full counts use this corrected scope. Unbounded
full-text searches keep the original direct index path. This adds work for dated
searches but avoids silently returning incorrect records or counts.

## Read-only verification

Safe unit/fixture tests do not need SereneDB:

```bash
.venv/bin/python -m pytest tests/test_search_match_modes.py tests/test_sql_backend.py -q
```

For the existing jury database published on local port 17890, run these commands
from the host virtual environment. They require an explicit DSN and never read
`.env`, initialize a database, deploy a server, or execute data/schema writes:

```bash
SERENEDB_DSN='host=127.0.0.1 port=17890 dbname=postgres user=postgres connect_timeout=5' \
RUN_SQL_BACKEND_INTEGRATION_TESTS=1 \
.venv/bin/python -m pytest tests/test_search_match_modes_integration.py -q

SERENEDB_DSN='host=127.0.0.1 port=17890 dbname=postgres user=postgres connect_timeout=5' \
.venv/bin/python -m scripts.verify_search_modes --query 'generating core.304' --repetitions 5
```

Live checks cover phrase order, adjacency, normalization, literal raw equality,
all four modes with every filter on both datasets, unlimited counts at different
limits, and the updated MCP handlers using an in-process test server. Date-filter
regressions compare counts to independently filtered candidate rows in Python,
including single bounds and microsecond boundaries. The running HTTP service is
not updated by these tests.

## Measured full-BGL results

Measured locally on 2026-10-17 with SereneDB 26.09.1.3 and **4,747,963 BGL rows**.
Same query for every mode: `generating core.304`, no filters, limit 20. After one
warm-up per mode, five interleaved iterations on one connection gave:

| Mode | Full match count | Records median | COUNT median | Combined median |
| --- | ---: | ---: | ---: | ---: |
| `any` | 1,775,932 | 6.256 ms | 1.446 ms | 7.702 ms |
| `phrase` | 164 | 26.721 ms | 2.234 ms | 28.919 ms |
| `exact` | 164 | 3.694 ms | 7.193 ms | 10.877 ms |
| `all` | 164 | 12.198 ms | 1.427 ms | 13.625 ms |

All returned 20 records with `truncated=true`. Combined medians are computed
from each iteration's combined duration, not the sum of the two medians. These
are client wall-clock SQL execution/fetch times with a warm cache; connection
setup, MCP serialization, and client/agent overhead are excluded. Results depend
on query selectivity, hardware, and cache state, not just corpus size.

With the same query and limit plus `KERNEL`/`INFO` filters and
`2005-06-03 <= ts < 2005-07-03`, combined medians were 40.541 ms (`any`,
473,973 matches), 36.899 ms (`phrase`, 92), 3.946 ms (`exact`, 92), and
19.931 ms (`all`, 92). The old direct index date predicate returned 147 for this
phrase; independently filtering the 164 candidate rows confirms **92**. Reproduce
this case by adding `--start-time 2005-06-03T00:00:00
--end-time 2005-07-03T00:00:00 --component KERNEL --severity INFO` to the benchmark.

For a broader semantic check, `generating core` returned 1,775,869 matches in
default mode and 1,706,751 in phrase mode; reversed phrase `core generating`
returned zero. No schema, indexes, or production data were changed.
