# OpenStack tracing through MCP

`search_logs` supports literal `request_id` and `instance_id` equality filters on
the existing SQL-backed OpenStack corpus. Either identifier permits omitting
`query` (or passing null). Both identifiers, any supplied text query, time bounds,
component and severity combine with **AND**. There is no implicit OR expansion
to API messages or other request contexts.

Every SQL-backed OpenStack search record now also contains `request_id`,
`instance_id`, `service`, `http_status`, `latency_s`, and `is_anomaly`, alongside
all original fields. Stored nulls and numeric precision are preserved. BGL and
legacy sample projections remain unchanged. Identifier filters on those backends
are rejected with an error directing the caller to SQL-backed OpenStack.

All four message match modes retain their semantics. A supplied blank token
query is rejected, even with identifiers; omit it for structured-only lookup.
With no query, `match_mode` does not add a message predicate.
Existing `exact` support for empty/whitespace messages remains available.
Identifiers reject blank, non-string and NUL-containing values; surrounding
whitespace is stripped as with the existing component/severity filters.

## Updated tool schema

Only `dataset` is unconditionally required in the generated MCP schema. The
conditional query/identifier requirement is checked before database access.
Below is the generated schema without cosmetic title annotations:

```json
{
  "type": "object",
  "required": ["dataset"],
  "properties": {
    "dataset": {"type": "string"},
    "query": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": null},
    "start_time": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": null},
    "end_time": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": null},
    "component": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": null},
    "severity": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": null},
    "limit": {"type": "integer", "default": 20},
    "match_mode": {"type": "string", "enum": ["any", "phrase", "exact", "all"], "default": "any"},
    "request_id": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": null},
    "instance_id": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": null}
  }
}
```

Results remain bounded to at most 50 records (default 20), ordered by timestamp
and stable source-aware record ID. The independent `total_corpus_match_count`
counts the entire filtered dataset. `truncated` uses an extra fetched row and
does not imply a sampled total. Executed record/count SQL, bound values and
applied filters remain in provenance. No new MCP tools were introduced.

## Actual example calls

The following calls ran against SereneDB 26.09.1.3 and the existing 207,820-row
OpenStack corpus on 2026-10-18. Complete schema and responses, including SQL and
bound parameters, are captured in `artifacts/openstack_tracing_report.json` by
the verification script. The JSON below contains response excerpts only.

Request trace:

```json
{"dataset":"openstack","request_id":"req-d6143bd4-9784-4e95-a0c8-83fba00530b6","limit":50}
```

```json
{"returned_record_count":12,"total_corpus_match_count":12,"truncated":false}
```

The first record is `1000011899`, at `2017-05-14T21:07:36.772000`, service
`nova-api`, HTTP status 202, latency **0.4794919** seconds, null `instance_id`,
and `is_anomaly=false`. The remaining 11 records are compute records for the
example VM. The request trace does not include its background lifecycle events.

Structured VM lookup with an intentionally small limit:

```json
{"dataset":"openstack","instance_id":"544fd51c-4edc-4780-baae-ba1d80a0acfc","limit":2}
```

```json
{"returned_record_count":2,"total_corpus_match_count":25,"truncated":true}
```

This matches the structured column only: all 25 complete matches are compute
records. Supplying both the example request and VM identifiers returns 11 rows.

Find the observed slow spawn within that VM:

```json
{"dataset":"openstack","instance_id":"544fd51c-4edc-4780-baae-ba1d80a0acfc","query":"41.81 seconds to spawn","match_mode":"phrase","limit":50}
```

```json
{
  "match_mode":"phrase",
  "records":[{
    "record_id":1000011980,
    "event_id":null,
    "timestamp":"2017-05-14T21:08:19.736000",
    "component":"nova.compute.manager",
    "severity":"INFO",
    "message":"[instance: 544fd51c-4edc-4780-baae-ba1d80a0acfc] Took 41.81 seconds to spawn the instance on the hypervisor.",
    "request_id":"req-d6143bd4-9784-4e95-a0c8-83fba00530b6",
    "instance_id":"544fd51c-4edc-4780-baae-ba1d80a0acfc",
    "service":"nova-compute",
    "http_status":null,
    "latency_s":null,
    "is_anomaly":true
  }],
  "returned_record_count":1,
  "total_corpus_match_count":1,
  "truncated":false
}
```

An explicit message-mention search can recover API references for this VM:

```json
{"dataset":"openstack","query":"544fd51c-4edc-4780-baae-ba1d80a0acfc","match_mode":"phrase","limit":50}
```

This returns **28** records, untruncated: 25 compute records and three API
mentions (GET, DELETE and the network external event). The three API records
retain null structured VM IDs and false anomaly labels. The raw-table
`instance_id = ... OR strpos(message, ...) > 0` research query independently
returns 28 for this example. Phrase matching uses token normalization; it is
evidence of a message mention, not a general guarantee of literal substring
identity or lifecycle completeness. Inspect the returned raw messages.

## Reproduced investigation and limitations

The checked-in Q1–Q6 SQL research was executed again without modification. Q3
confirms a normal spawn median of **19.74 s** across 2,062 unlabelled spawns
(maximum 26.69 s). The four labelled spawn messages are **29.46, 31.27, 41.81
and 50.72 s**. Each labelled VM has 25 structured compute records and 28
message-mention records, all INFO; the latter include three API records per VM.

The MCP can directly retrieve a request, a structured VM trace, the slow-spawn
message, and explicit UUID message mentions. Population duration parsing,
quantiles and phase-duration aggregates (Q3/Q6), HTTP latency statistics (Q5),
and the label-table audit (Q4) still require external SQL. `summarize_logs`
continues to provide pattern counts; this change adds no duration statistics or
structured filters to aggregation. No root cause or injected fault is inferred.

Request equality also permits inspecting these two background contexts:

| Request context | Complete record count | Interpretation |
| --- | ---: | --- |
| `req-addc1839-2ed5-4778-b57e-5854eb7b8b09` | 52,349 | Background context; no structured VM IDs |
| `req-3ea4052c-895d-4b64-9e2d-04d64c4d94ab` | 12,248 | Background callbacks spanning 2,066 structured VM IDs |

They are not individual lifecycle requests. Lookups remain bounded and report
full counts and truncation. There is no automatic traversal of request-ID links.
`get_nearby_events` retains global chronology, including unrelated API polls;
its description now makes this explicit. `is_anomaly` is a stored injected-VM
label, not an anomaly detector; false does not establish normal operation.

A larger lifecycle feature would need explicit, opt-in UUID URL/message
matching, evidence for each correlation, background-context exclusions, and a
clear coverage contract. Strict structured equality is the supported interface
here. Neither this lookup nor a message search establishes a complete lifecycle.

## Correctness, tests and timings

SQL-backed OpenStack text searches now use the existing full-text index to
select stable IDs, then retrieve metadata and apply other filters from the base
table. The index does not project `latency_s` or `is_anomaly`. Structured-only
and exact searches use the base table directly. Timestamp predicates remain
outside the index, with existing psycopg text-wire datetime binds. BGL and
legacy sample SQL remain unchanged. Every caller value is bound; identifiers
are never inserted into SQL syntax.

Tests cover all four text modes, both identifiers separately/together, omitted
queries, invalid inputs/backends, source-ID collisions, SQL injection payloads,
null metadata, empty results, independent complete counts, exact-limit and
truncated results, timestamp endpoints/microsecond windows, the previous BGL
timestamp regression, and existing aggregation behavior. Real HTTP tests use
temporary servers with both current and legacy protocol modes.

Verification results:

- Full Compose suite through `scripts/test-compose.sh`: **437 passed, 0 failed,
  11 skipped in 14.69 s**, including live Pattern Analytics, Incident Tracing,
  timestamp regression and both HTTP protocol modes.
- Host suite: **377 passed, 0 failed, 71 skipped in 5.46 s**. Live integration
  flags were disabled in this host run.
- Previously failing shell/download cases plus the new missing-curl guard:
  **11 passed, 0 failed, 0 skipped in 1.71 s** in Compose.
- The 11 skipped Compose tests are 10 legacy sample/staging tests and the one
  test targeting the unchanged deployed HTTP container. Updated-code HTTP tests
  did run successfully in the temporary test container.

The initial failures were `test_download_fresh_repeat_and_missing_member` and
`test_corrupt_existing_source_is_preserved`. Their shared download fixture
provided a fake network fetcher but relied on a host-installed `unzip`, absent
from the slim runtime. Both stopped at `Install unzip first.`, before extraction
or the expected corrupt-file checksum check. The fixture now supplies only the
used `unzip -p` operation through Python's standard-library ZIP reader, extracting
real fixture archive bytes. A new regression verifies the actual missing-host-
executable error; the interruption test also verifies that it reaches curl.

A later run without the executable fixture mount failed ten cases: six workflow
cases and three download cases could not execute scripts on `/tmp` (`noexec`),
and the missing-unzip test passed None from `shutil.which` into `symlink_to`.
Original scripts, copied scripts and the fixture curl had mode 0755. On the
observed Linux `noexec` mount, `os.access(..., X_OK)` was false despite those mode
bits, so executable lookup rejected the fixture curl; the slim image has no
system curl fallback. The prerequisite fixture now references its own curl path
directly, checks lookup failures for actual host utilities explicitly, and tests
both missing curl and missing unzip. Production prerequisite failures remain
errors, not skips. The test-only runner consistently supplies the executable
bind mount without weakening production `/tmp` or adding runtime packages.

This was a test-environment gap, not an MCP runtime dependency. `jury-up.sh`
executes `download.sh` on the host before starting/loading database services.
The documented host `unzip` prerequisite still applies to clean setup and
judging: if absent, setup fails clearly before downloading or loading data.
Production scripts, the runtime image and Compose configuration are unchanged.
No clean dataset initialization was rerun during this SELECT-only verification.

Five warm-cache benchmark repetitions followed one warm-up per case. Medians
below include SQL execution/fetching of bounded rows **plus the complete count**;
connection setup and MCP overhead are excluded. These are Compose-network
measurements, not Windows-specific or cold-cache claims.

| Lookup | Complete matches | Median |
| --- | ---: | ---: |
| Request, limit 50 | 12 | 4.234 ms |
| Structured VM, limit 50 | 25 | 1.776 ms |
| Both IDs, limit 50 | 11 | 3.542 ms |
| VM + phrase | 1 | 7.583 ms |
| VM + timestamp bounds | 25 | 1.427 ms |
| Full UUID phrase | 28 | 15.103 ms |
| Background request 1, limit 2 | 52,349 | 4.671 ms |
| Background request 2, limit 2 | 12,248 | 4.713 ms |

## Docker Compose verification without redeployment

Run from the repository root with the existing SereneDB service already running.
The existing MCP image supplies the locked dependencies; current source is
mounted read-only. `run --no-deps` creates only a disposable test container; it
does not build, restart or replace the running services. Tests use the Compose
service's `SERENEDB_DSN` (`serenedb:7890`), with no host/server-specific address.

```bash
./scripts/jury-compose.sh ps
./scripts/jury-compose.sh run --rm --no-deps -T \
  -v "$PWD:/workspace:ro" -w /workspace \
  -e RUN_SQL_BACKEND_INTEGRATION_TESTS=1 \
  mcp-server python -m pytest \
  tests/test_openstack_tracing.py tests/test_openstack_tracing_integration.py \
  tests/test_search_match_modes.py tests/test_search_match_modes_integration.py \
  tests/test_aggregation_match_modes.py tests/test_aggregation_match_modes_integration.py \
  tests/test_sql_backend_integration.py \
  -q -p no:cacheprovider -rs

mkdir -p artifacts
./scripts/jury-compose.sh run --rm --no-deps -T \
  -v "$PWD:/workspace:ro" -v "$PWD/artifacts:/evidence" -w /workspace \
  mcp-server python -m scripts.verify_openstack_tracing \
  --repetitions 5 --output /evidence/openstack_tracing_report.json
```

For the full suite including shell-workflow fixtures, use the test-only runner.
It provides the writable executable fixture mount and `--basetemp`, enables the
SELECT-only SQL and temporary HTTP tests, and uses current source read-only. The
test fixtures supply their own curl and ZIP extractor; no extra runtime OS
packages or live database writes are needed:

```bash
./scripts/test-compose.sh
# Optional pytest selection:
./scripts/test-compose.sh tests/test_jury_setup.py
```

After a separately authorized deployment, the extended HTTP verifier supports:

```bash
./scripts/jury-compose.sh exec -T mcp-server python -m scripts.verify_mcp_http \
  --url http://127.0.0.1:8000/mcp --patterns --tracing
```

That last command checks the code in the deployed image; it will not verify new
features until the image has been updated. No deployment was performed here.
