# Pattern-aware aggregation

`summarize_logs` now accepts optional `query` and `match_mode`, using the same
message semantics as `search_logs`. Matching, existing filters, grouping and
counting all execute in SereneDB over the complete eligible corpus. No log-row
sample is used to calculate aggregates.

| Mode | Message semantics |
| --- | --- |
| `any` (default) | Any query token, existing OR full-text behavior |
| `all` | Every query token, any order, no adjacency requirement |
| `phrase` | Consecutive tokens in order, using the existing index dictionary |
| `exact` | Equality with the entire raw message, preserving case, punctuation and whitespace |

Omitting `query` or supplying `null` preserves the old SQL, bound values, response
fields, count meanings and default ordering. A blank query is rejected except in
`exact` mode, where empty and whitespace-only messages are valid values. For
example, the verified BGL corpus contains 34,470 empty messages.

Time grouping supports `hour`, `day`, `week` and `month`. Weeks start Monday;
months start on the first calendar day. Timestamps remain naive source-log time.
`sort_direction` defaults to `asc` (earliest first); `desc` returns newest first
and is valid only for time grouping. Other groups retain descending record-count
ordering with their existing tie breakers.

`total_matching_record_count` counts every matching and filtered **log record**,
independently of LIMIT. `record_count` counts records in an individual group;
`returned_group_count` counts groups actually returned. The implementation fetches
one extra group to determine `truncated`. When `truncated=true`, the displayed
groups are incomplete: their counts need not sum to the complete total. There is
no new total-group-count field. Empty periods are not synthesized. Use a narrower
time range, a larger limit (maximum 100), or coarser buckets for a complete series.

## Tool input schema

The generated MCP JSON schema below omits cosmetic `title` annotations only.
Semantic allowlists for existing string parameters remain runtime validations;
their schema types have not changed.

```json
{
  "type": "object",
  "required": ["dataset", "group_by"],
  "properties": {
    "dataset": {"type": "string"},
    "group_by": {"type": "string"},
    "interval": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": null},
    "start_time": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": null},
    "end_time": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": null},
    "component": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": null},
    "severity": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": null},
    "limit": {"type": "integer", "default": 20},
    "query": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": null},
    "match_mode": {"type": "string", "enum": ["any", "phrase", "exact", "all"], "default": "any"},
    "sort_direction": {"type": "string", "enum": ["asc", "desc"], "default": "asc"}
  }
}
```

Datasets: `bgl`, `openstack`. Group dimensions: `component`, `severity`, `event_id`,
`team`, `time`. `interval` is required only for `time`. The SQL-managed corpus
does not store EventIds, so its event grouping retains a single NULL group.
Time bounds are start-inclusive/end-exclusive ISO 8601 values without a timezone.

## Actual requests and responses

These requests were executed through the current MCP handlers against the
existing full SQL corpora on 2026-10-18. Response excerpts below omit other
unchanged fields for readability. The benchmark script captures the complete
generated schema and both full responses, including SQL, parameters, scope and
fictional ownership provenance, in `artifacts/pattern_aggregation_report.json`.

Phrase time series request:

```json
{
  "dataset": "bgl",
  "query": "data storage interrupt",
  "match_mode": "phrase",
  "group_by": "time",
  "interval": "month",
  "sort_direction": "desc",
  "limit": 3
}
```

Response excerpt:

```json
{
  "groups": [
    {"bucket_start": "2005-11-01T00:00:00", "record_count": 3},
    {"bucket_start": "2005-06-01T00:00:00", "record_count": 63490}
  ],
  "returned_group_count": 2,
  "total_matching_record_count": 63493,
  "group_limit": 3,
  "truncated": false
}
```

Fictional team aggregation request:

```json
{
  "dataset": "bgl",
  "query": "data storage interrupt",
  "match_mode": "phrase",
  "group_by": "team",
  "limit": 100
}
```

Response excerpt:

```json
{
  "groups": [{"team": "Compute Kernel", "record_count": 63493}],
  "returned_group_count": 1,
  "total_matching_record_count": 63493,
  "group_limit": 100,
  "truncated": false,
  "mapping_coverage": {
    "total_record_count": 63493,
    "mapped_record_count": 63493,
    "unmapped_record_count": 0,
    "mapped_record_percentage": 100.0,
    "total_component_count": 1,
    "mapped_component_count": 1,
    "unmapped_component_count": 0,
    "mapped_component_percentage": 100.0
  },
  "provenance": {
    "fictional_ownership": {
      "fictional": true,
      "authoritative": false,
      "statement": "Fictional hackathon mapping; not authoritative"
    }
  }
}
```

The same phrase aggregates to component `KERNEL` with 63,493 records. For
OpenStack, phrase `instance` matches 66,137 records, attributed fictionally to
Virtualization (47,151), Capacity & Scheduling (16,884), and Cloud API (2,102).

## SereneDB correctness

The existing `log_dict` dictionary stores token positions and normalizes case
without stemming. `phrase` continues to use `message @@ ts_phrase(...)` on the
existing inverted relation. `exact` uses base-table `message = ...` only.

Pattern aggregations select matching source IDs through the index, then filter
and aggregate **base-table rows**. The ID expression is shared with search,
including OpenStack's source-file-aware ID. Aggregation totals use the same
unlimited scope. Team grouping and coverage use that filtered scope before a
LEFT JOIN; unmapped components remain `UNMAPPED` and ownership remains explicitly
fictional. Existing readiness checks require unique ownership mapping keys.

Timestamp predicates stay outside the inverted relation. Pattern queries bind
timestamps with psycopg `%t` (text wire format), preserving the existing search
workaround. The derived filtered-table + ownership-join plan additionally needs
`%(fetch_limit)t`: SereneDB 26.09.1.3 misreads a binary integer LIMIT on this path.
These remain typed, bound parameters; no user values are interpolated.

The regression oracle reads the small, complete `generating core.304` candidate
set **without any SQL timestamp predicate** and filters it in Python in tests
only. For `2005-06-03 <= ts < 2005-07-03`, `KERNEL`, `INFO`, the correct phrase
total is **92**. Both aggregation and mapping coverage return 92; the historical
incorrect index-range result was 147. Start-only, end-only, exclusive upper
bounds and microsecond-sized windows are also verified for all four modes.

## Verification and benchmarks

Final combined run: **367 passed, 11 skipped in 13.37 s**. This included new unit
tests for both dataset specs and all grouping dimensions; actual SELECT-only BGL
and OpenStack aggregation tests; existing search regressions; independent JOIN
count references; full-scope reconciliation; LIMIT/empty/injection checks;
unmapped ownership/source-ID fixtures; and temporary real HTTP servers using
both current and legacy MCP protocols. Week/month boundaries include New Year,
Sunday/Monday and leap day. Descending LIMIT is checked against the newest
matching timestamp independently.

The 11 skipped tests comprise 10 legacy sample/staging integration tests gated by
`RUN_SERENEDB_INTEGRATION_TESTS` and one test targeting the deployed HTTP service.
The updated code's HTTP tests ran on temporary loopback servers connected to the
full database. The deployed container was not updated or certified as containing
the new code. No production data, tables or indexes were changed.

Run the same suite from a checkout with dependencies installed:

```bash
export SERENEDB_DSN='host=127.0.0.1 port=17890 dbname=postgres user=postgres connect_timeout=5'
RUN_SQL_BACKEND_INTEGRATION_TESTS=1 RUN_LOCAL_HTTP_FIXTURE_TESTS=1 \
  .venv/bin/python -m pytest -q -p no:cacheprovider
.venv/bin/python -m scripts.verify_aggregation_modes --repetitions 5 \
  --output artifacts/pattern_aggregation_report.json
```

Measured against **SereneDB 26.09.1.3**, BGL **4,747,963** rows and OpenStack
**207,820** rows. Timings are medians of five warm-cache runs after one warm-up
per case, measured as client wall time executing/fetching all SELECTs. They
include the independent total count and, for teams, mapping coverage. They
exclude connection setup and MCP overhead; they are not cold-cache or Windows
performance claims.

| Request | Complete matches | Combined median |
| --- | ---: | ---: |
| BGL `generating core.304`, component, any | 1,775,932 | 83.785 ms |
| Same, all | 164 | 5.554 ms |
| Same, phrase | 164 | 9.974 ms |
| Same, exact | 164 | 14.602 ms |
| BGL `data storage interrupt`, phrase, day | 63,493 | 17.699 ms |
| Same, month descending | 63,493 | 17.163 ms |
| Same, fictional team | 63,493 | 28.007 ms |
| BGL filtered timestamp regression, fictional team | 92 | 106.936 ms |
| OpenStack `instance`, phrase, day | 66,137 | 18.053 ms |
| Same, fictional team | 66,137 | 30.092 ms |

## Windows: update only MCP

Use the existing **WSL2 checkout with Docker Desktop integration**, as required
by this repository's Windows setup. First place the reviewed changed files in
that checkout. These local changes have not been committed or pushed, so a
remote pull alone cannot retrieve them until they are published separately.

Run in WSL from the repository root:

```bash
./scripts/jury-compose.sh build mcp-server
./scripts/jury-compose.sh up -d --no-deps --no-build --force-recreate \
  --wait --wait-timeout 120 mcp-server
./scripts/jury-compose.sh exec -T mcp-server python -m scripts.verify_mcp_http \
  --url http://127.0.0.1:8000/mcp --patterns
./scripts/jury-compose.sh ps mcp-server serenedb
```

The wrapper fixes the existing `schubki-jury` project, Compose file and ports.
`build mcp-server` builds only the MCP image; `up --no-deps ... mcp-server`
recreates only MCP, leaving the SereneDB service and existing data volume intact.
The HTTP smoke check executes SELECT-only queries inside the updated MCP
container. Reconnect the MCP client afterward to refresh its cached tool schema.
Do not run the initial setup/loader or a project-wide `down` for this update.
These deployment commands are instructions only; no containers were redeployed
during implementation.
