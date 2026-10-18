# OpenStack VM spawn performance

`analyze_vm_spawn_performance` discovers slow measured spawns without an instance
ID and computes exact full-population duration statistics. It supports the
SQL-backed OpenStack corpus only. It adds a fifth MCP tool; the four existing
tools, their parameters, response fields and health response remain intact.
Use MCP `tools/list` as the authoritative capability catalog.

## Interface

All parameters are optional:

```python
analyze_vm_spawn_performance(
    dataset="openstack",
    start_time=None,
    end_time=None,
    slow_threshold_seconds=None,
    baseline_multiplier=1.25,
    baseline_scope="corpus",
    interval="day",
    top_limit=10,
    bucket_limit=100,
    sort_direction="desc",
    include_anomaly_labels=False,
)
```

| Parameter | Contract |
| --- | --- |
| `dataset` | Only `openstack`, with `MCP_DATASET_BACKEND=sql` |
| `start_time`, `end_time` | Nullable naive ISO 8601 timestamps; start inclusive, end exclusive |
| `slow_threshold_seconds` | Nullable finite number >= 0; overrides relative detection |
| `baseline_multiplier` | Finite number > 1; used only for relative detection |
| `baseline_scope` | `corpus` (all loaded candidates) or `analysis` (selected time range) |
| `interval` | `hour`, `day`, or null to disable buckets |
| `top_limit` | Integer 1..50 |
| `bucket_limit` | Integer 1..100 |
| `sort_direction` | `asc` or `desc`, for time buckets only |
| `include_anomaly_labels` | Boolean; optionally includes stored `is_anomaly` on ranked events |

Numeric strings, booleans used as numbers, non-finite values, unsupported enums,
timezones, reversed/equal time bounds and invalid limits are rejected. The exact
generated JSON input schema is captured by the verification script.

An event is slow only when its **unrounded DOUBLE duration is strictly greater
than the effective threshold**. Without an absolute threshold, that threshold
is baseline P95 multiplied by `baseline_multiplier`. The baseline includes all
valid candidates, independent of stored labels. `baseline_scope="corpus"` is a
retrospective reference, so it can include periods after an analysis window.
It does not claim to be a healthy-only or causal historical baseline.

## Counts, ranking and empty results

- `counts` separates candidate records, valid spawn events, malformed/non-finite
  durations, distinct non-null VM IDs and valid events with missing VM IDs.
- `statistics` contains mean, median, P95, P99, minimum and maximum seconds over
  every valid analysis event. There is no LIMIT in the statistical population.
- `baseline` reports its own full counts/statistics and observed time coverage.
- `detection` reports the method, effective threshold, comparison, complete slow
  event count and percentage. Percentages use the complete valid-event count.
- `slowest_spawn_events` contains the top **events**, not deduplicated VMs. Each
  includes record ID, timestamp, nullable instance ID, component, duration and
  `is_slow`. Optional stored labels never affect selection, baselines or ranking.
- `ranking.truncated` indicates omitted valid events. The separate
  `slow_events_truncated` flag indicates whether the output omitted slow events.
- Time buckets contain full counts and duration statistics, plus slow counts and
  percentages using the **same global effective threshold**. Their complete
  count and truncation flag are separate from ranking limits.

Ties are ordered by duration descending, timestamp ascending, then source-aware
record ID ascending. Mean accumulation is explicitly ordered by record ID for
repeatable DOUBLE results. Buckets are ordered explicitly in SQL. Changing
either output limit cannot alter statistics, baselines or detection.

Invalid-only buckets remain visible with zero valid events and null duration
statistics. Empty periods are omitted. Bucket timestamps are completion-log time,
not inferred boot-start time; boundary buckets may be partial. No timezone
conversion is performed.

An empty analysis returns null duration statistics, empty rankings and no buckets.
An available corpus baseline can still produce a threshold and a zero slow count.
An empty/nonpositive relative baseline or overflowed relative threshold instead
returns `status="unavailable_baseline"`, null threshold/slow count and an explicit
warning. An absolute threshold remains usable. Percentages with zero valid events
are null. Relative baselines below 20 valid events carry a low-support warning.

## SQL implementation

One parameterized SELECT computes all response sections from materialized CTEs:

1. Select candidate IDs with the existing indexed phrase `spawn the instance on
   the hypervisor` and restrict base rows to `nova.compute.manager`.
2. Read source-aware record IDs, typed timestamps, VM IDs and messages from the
   base table. Read `is_anomaly` only when explicitly requested.
3. Parse once with a versioned, private anchored pattern and
   `TRY_CAST(NULLIF(regexp_extract(...), '') AS DOUBLE)`.
4. Separate analysis and baseline populations, retaining full invalid counts.
5. Compute exact `quantile_cont` statistics, the threshold and full slow count.
6. Return bounded rankings and hourly/daily groups as ordered JSON arrays.

Materialization is query-local; it creates no table or persisted objects. The
single statement avoids cross-statement population drift. Python validates and
formats SQL output and computes scalar percentages/truncation metadata from full
SQL counts; it never estimates population statistics from returned rows.

Timestamp predicates apply to materialized **base-table-derived timestamps**,
never to the inverted relation, retaining the SereneDB 26.09 timestamp workaround.
Time, numeric and limit values remain bound parameters; interval/order SQL is
selected only from code-owned allowlists. There are no caller-defined patterns
or SQL identifiers. Provenance returns the full SELECT, actual bound values,
applied filters, parser version, percentile convention and statement count.

The parser accepts the known optional `[instance: ...]` prefix, integer/decimal
nonnegative seconds and the exact spawn suffix, with an optional final period.
Missing/negative/malformed/overflowed durations become null and are excluded from
metrics, not from quality counts. Missing VM IDs do not exclude valid durations.
`TRY_CAST` and finite checks prevent malformed values from aborting the query.

## Verified full-corpus results

Actual SereneDB 26.09.1.3 results from the existing 207,820-row OpenStack corpus:

| Measurement | Result |
| --- | ---: |
| Candidate / valid spawn events | 2,066 / 2,066 |
| Invalid durations / missing instance IDs | 0 / 0 |
| Distinct VM IDs among spawn events | 2,066 |
| Mean | 19.829172313649536 s, within DOUBLE precision |
| Median | 19.74 s |
| Population P95 | 21.0075 s |
| Population P99 | 21.6635 s |
| Minimum / maximum | 10.12 / 50.72 s |
| Absolute threshold 25 s | 5 events |
| Absolute threshold 30 s | 3 events |
| Default P95 × 1.25 threshold | 26.259375 s; 5 events (0.2420135527589545%) |

The earlier research's 20.99-second P95 is the rounded **unlabelled subset**
P95. The new baseline does not exclude labelled events.

Call the new tool with `{}` for the default fleet analysis. To inspect every
currently slow event with optional ground-truth labels:

```json
{"slow_threshold_seconds":25,"top_limit":5,"include_anomaly_labels":true}
```

The actual ranked durations are 50.72, 41.81, 31.27, 29.46 and 26.69 seconds.
Their stored labels are true, true, true, true and false respectively. The last
event is unlabelled, which does not establish that it is healthy or a false
positive. This response has `ranking.truncated=true` (five of 2,066 events) but
`ranking.slow_events_truncated=false` (all five slow events returned).

The reference event returned is:

```json
{
  "record_id":1000011980,
  "timestamp":"2017-05-14T21:08:19.736000",
  "instance_id":"544fd51c-4edc-4780-baae-ba1d80a0acfc",
  "component":"nova.compute.manager",
  "duration_seconds":41.81,
  "is_slow":true,
  "is_anomaly":true
}
```

For hourly trends in an analysis window, while retaining the corpus baseline:

```json
{"start_time":"2017-05-16T00:00:00","end_time":"2017-05-17T00:00:00","interval":"hour","bucket_limit":2}
```

That actual call measures 1,313 events with zero slow events; its baseline still
contains all 2,066. The two returned hourly buckets are explicitly truncated.

## Validation, performance and reproducibility

The complete Compose regression run passed **537 tests, 0 failures, 11 skips in
24.42 s**. The skips remain the ten legacy sample/staging tests and the one
deployed-endpoint test; the running services were not redeployed. Both temporary
HTTP protocol modes passed with Pattern Analytics, Incident Tracing and the new
tool in the same session.

New tests include independent raw-table/index candidate identity reconciliation,
full-population Python percentile references, absolute strict thresholds, both
baseline scopes, independent changes to each output limit, optional labels,
synthetic label flips with a positively detected slow event, malformed/overflowed
durations, zero/missing durations, missing/repeated VM IDs, empty analysis and
baseline populations, timestamp endpoints, leap-day grouping, deterministic ties,
SQL injection payloads and MCP schema/strict numeric validation. Synthetic SQL
uses SELECT/VALUES only; no production rows or schema were modified. The final
strengthened label-flip test passed separately and in the complete suite.

Five warm-cache measurements after one warm-up per case, including execution and
fetching the complete single SELECT (excluding connection setup and MCP overhead):

| Request | Median |
| --- | ---: |
| Default analysis + daily buckets | 90.724 ms |
| Absolute 25 s threshold | 89.500 ms |
| Absolute 30 s threshold | 87.279 ms |
| top_limit=1, bucket_limit=1 | 86.542 ms |
| Include stored labels | 94.082 ms |
| Hourly buckets | 89.762 ms |
| No time buckets | 64.924 ms |
| May 14 analysis / corpus baseline | 88.940 ms |
| May 14 analysis / analysis baseline | 89.050 ms |

These timings measure the complete implementation, including additional metrics
and quality counts; they are not the narrower research prototype timings, a
cold-cache claim or a latency guarantee. The index semi-join can still scan base
rows; no claim is made that all scans are eliminated. BGL is never queried.

With the existing Compose services already running:

```bash
./scripts/test-compose.sh
./scripts/test-compose.sh tests/test_vm_performance.py tests/test_vm_performance_integration.py
mkdir -p artifacts
./scripts/jury-compose.sh run --rm --no-deps -T \
  -v "$PWD:/workspace:ro" -v "$PWD/artifacts:/evidence" -w /workspace \
  mcp-server python -B -m scripts.verify_vm_spawn_performance \
  --repetitions 5 --output /evidence/vm_spawn_performance_report.json
```

The generated report contains the exact tool schema, actual MCP requests and
full responses, all SQL/parameters and timing samples. It lives in ignored local
`artifacts/`; no credentials are written to it. After a separately authorized
MCP image update, verify the deployed service using:

```bash
./scripts/jury-compose.sh exec -T mcp-server python -m scripts.verify_mcp_http \
  --url http://127.0.0.1:8000/mcp --patterns --tracing --performance
```

No Docker deployment, schema/index change, dataset load or new dependency is
needed for implementation verification. The feature has not been deployed here.

## Limitations

This measures observed duration log events, not deduplicated physical boots,
end-to-end readiness, failures without a completion log, or complete lifecycles.
Other message formats are outside the versioned parser contract. Populations can
span different workloads and gaps in source coverage. The relative threshold is
an explicit heuristic, not a statistical anomaly probability or root-cause claim.
Percentiles use all valid events at DOUBLE precision; no approximate/sampled
fallback is used. Very large future populations will need fresh resource and
latency benchmarks rather than a silent change to approximate statistics.
