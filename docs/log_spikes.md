# BGL log spike screening

`detect_log_spikes` is the sixth MCP tool. It screens the SQL-backed BGL corpus
for unusually large component log volumes without requiring a known suspicious
window. It changes none of the five existing tool interfaces or response fields.
Use `tools/list` for the capability catalog; the historical health response is
preserved.

## Interface

All arguments are optional:

```python
detect_log_spikes(
    dataset="bgl",
    start_time=None,
    end_time=None,
    component=None,
    interval="hour",
    signal="severity_volume",
    severities=None,
    baseline_scope="corpus",
    baseline_start_time=None,
    baseline_end_time=None,
    baseline_quantile=0.99,
    threshold_multiplier=1.5,
    min_bucket_count=20,
    min_baseline_buckets=100,
    max_findings=10,
    patterns_per_finding=3,
    evidence_per_pattern=1,
)
```

| Argument | Contract |
| --- | --- |
| `dataset` | Only `bgl` with the SQL backend |
| `start_time`, `end_time` | Naive ISO 8601; start inclusive, end exclusive |
| `component` | Nullable exact component filter; unknown names return no component findings |
| `interval` | `hour` or `day`, on the original source clock |
| `signal` | `severity_volume` or `total_volume` |
| `severities` | Null defaults to WARNING, ERROR, SEVERE, FAILURE, FATAL; otherwise a list of 1..6 entries drawn from those levels plus INFO |
| `baseline_scope` | `corpus` or `reference` |
| `baseline_start_time`, `baseline_end_time` | Reference scope only; explicit ordered bounds required along with disjoint explicit analysis bounds |
| `baseline_quantile` | Finite number strictly between 0 and 1 |
| `threshold_multiplier` | Finite number >= 1 |
| `min_bucket_count` | Integer 1..1,000,000,000 |
| `min_baseline_buckets` | Integer 1..1,000,000 |
| `max_findings` | Integer 1..20 |
| `patterns_per_finding` | Integer 1..5 |
| `evidence_per_pattern` | Integer 1..3 |

`total_volume` includes all levels and rejects a supplied severity list. Severity
names are case-sensitive, duplicates are removed deterministically, and CRITICAL
is rejected because BGL does not have that level. Numeric strings, booleans used
as numbers, fractional limits, non-finite numbers, timezones, reversed/equal
ranges and unsupported argument combinations fail before opening a connection.

## Method and population

Each component's baseline comprises eligible **component-active** buckets: at
least one record of any severity exists for that component. For the default
signal, INFO-only buckets are genuine zero selected-record observations.
Silent periods are not zero-filled, and lack of component records never proves
health or collection completeness.

An eligible bucket must be entirely inside the corpus timestamp span and, when
applicable, the requested analysis/reference span. The first or last bucket is
excluded when its boundary extends outside the corresponding span. Coverage
bounds do not establish continuous observation within a bucket.

Clock offsets are computed as `round((epoch(ts) - epoch) / 3600.0)`. Any bucket
containing multiple such offsets, or missing epoch values, is excluded globally,
even if the record causing the ambiguity belongs to another component. This
preserves naive source-clock timestamps and does not infer a timezone. The loaded
corpus has offsets -7 and -8 hours and one mixed hour, 2005-10-30 01:00.
This guard detects rounded-hour offset differences, not arbitrary clock drift.

For every component with sufficient baseline support:

```text
threshold = quantile_cont(selected records per eligible bucket, baseline_quantile)
            × threshold_multiplier
elevated = selected_record_count >= min_bucket_count
           AND selected_record_count > threshold
```

Comparison uses the unrounded DOUBLE threshold. Defaults are P99 × 1.5, at least
20 selected records, and at least 100 baseline buckets. A zero quantile is usable:
a positive burst can pass the count gate; ratios to zero baselines remain null.
Insufficient baselines and non-finite derived thresholds have explicit statuses
and their buckets are not classified as normal. Small empirical tails remain
unstable even when the minimum support gate is met.

`corpus` is retrospective, includes the analysis population and can include later
periods. `reference` uses only whole buckets inside the supplied disjoint range;
a reference after the analysis is allowed and explicitly warned about. Neither
scope excludes stored alert labels, and neither is a healthy-only baseline.

Adjacent elevated buckets merge only within the same component. Missing,
excluded, and non-elevated buckets break the window. First/last matching record
times accompany the bucket boundaries; neither establishes incident onset or
resolution. Windows, records and node identifiers are never counted as incidents.
The rule is a configurable screening heuristic, not a calibrated false-positive
probability. It does not adjust for seasonality or changing workload.

## Response and evidence

- `scope`: corpus table, timestamp conventions, count unit and complete-population
  aggregation declaration.
- `method`: version, quantile/multiplier, count/support gates, comparison rule,
  and explicit label/seasonality independence.
- `counts`: full corpus and exact time/component input counts; eligible and
  evaluated component-bucket populations; elevated records/buckets; complete
  spike-window counts and returned window count.
- `coverage`: full corpus time span, global calendar/observed/unobserved buckets,
  gap counts, bounded gap ranges and clock diagnostics. Exclusion counters in
  `counts` can overlap; their sum is not a unique excluded-population count.
- `baselines`: per-component support, counts, time span, mean, median, P95, P99,
  selected quantile, threshold and status. At most 100, ordered by component,
  with complete component counts and a separate truncation flag.
- `findings`: windows ordered by selected-record count descending, then start
  time and component. Each contains complete record/bucket counts, peak and mean
  selected counts per bucket, baseline comparison ratios, selected-to-total
  percentage and distinct non-null node identifier counts.
- `truncation`: independent finding, pattern, evidence and baseline limits.
- `provenance`: one executed SELECT, actual bound parameters, applied filters,
  percentile convention and methodology version.

Contributors group by **exact message and severity**, preserving case,
punctuation, whitespace and empty messages. They are repeated log-message groups,
not inferred incident templates. Each has a complete count, percentage of the
window's selected population, first/last timestamps, distinct node identifiers
and bounded original record references. Evidence ordering is timestamp then
record ID; pattern ties use exact message then severity.

The complete contributor population is checked against the window count before
formatting. Returned contributor counts plus `omitted_pattern_record_count`
must equal `selected_record_count`; percentages similarly reconcile to 100%.
Distinct node counts are calculated over the whole window, not summed across
patterns. SQL null node values stay null; literal strings such as `"NULL"` are
preserved as stored and are not invented physical-node identities.

Messages longer than 1000 characters have an explicit truncation flag and full
character count. A display fingerprint distinguishes long-message examples, but
hashes never replace exact-text grouping/equality. Evidence record IDs support
follow-up with `get_nearby_events`; displayed excerpts are not exact-search keys
when truncated. Gap/clock diagnostic arrays are capped at 20 with complete counts
and explicit truncation flags.

## SQL strategy and correctness

One SELECT uses materialized bucket counts, coverage, baselines, scoring and
window CTEs. The raw source and window-row projections are NOT MATERIALIZED,
allowing narrow column scans without retaining every corpus message in memory.
Exact-message aggregation and evidence ranking stay inside SereneDB. Python
formats bounded results and scalar ratios from complete SQL counts; it never
estimates a population from returned evidence.

All population calculations precede finding/pattern/evidence LIMITs. All caller
values are bound parameters; interval SQL comes only from an hour/day allowlist.
Timestamp filtering and evidence joins use base-table-derived timestamps with
text-wire timestamp binds, never timestamp predicates on the inverted relation.
The full SELECT provides one statement's view of all response populations.
No persisted tables, indexes or caches are created. Stored `is_alert` and
`alert_label` are never selected by the production query.

## Actual full-corpus results and examples

Verified on SereneDB 26.09.1.3 against **4,747,963 BGL records**:

| Measurement | Default hourly result |
| --- | ---: |
| Selected input records | 1,012,140 |
| Component-active buckets | 5,017 |
| Eligible component buckets after exclusions | 5,013 |
| Evaluated component buckets with sufficient baselines | 4,934 |
| Elevated component-hours | 24 |
| Records in all elevated windows | 683,671 |
| Contiguous spike windows | 16 |
| Default returned windows | 10, explicitly truncated |
| Observed / calendar global hours | 3,618 / 5,154 |
| Unobserved global hours | 1,536 |
| Mixed-offset global hours | 1 |
| Components with insufficient history | 3 |

`{}` performs default discovery. To return all current windows with bounded
contributors/evidence:

```json
{"max_findings":20,"patterns_per_finding":5,"evidence_per_pattern":3}
```

To investigate MMCS using its corpus baseline:

```json
{"component":"MMCS","start_time":"2005-09-20T12:00:00","end_time":"2005-09-20T13:00:00"}
```

That actual call returns one window with 66,563 ERROR records, baseline support
164 buckets, baseline P99 8334.690000000086, and threshold 12502.035000000129.
Its exact contributors are:

| Exact message | Records | Share | First evidence record |
| --- | ---: | ---: | ---: |
| `idoproxydb hit ASSERT condition: ASSERT expression=0 Source file=idotransportmgr.cpp Source line=1043 Function=int IdoTransportMgr::SendPacket(IdoUdpMgr*, BglCtlPavTrace*)` | 63,468 | 95.3502696693% | 3395426 |
| `Ido packet timeout` | 3,095 | 4.6497303307% | 3412620 |

Both groups cover the same one distinct stored node identifier, the literal
string `"NULL"`. Omitted-message remainder is zero. These are log counts, not
66,563 incidents or a verified root cause.

Other verified modes: daily default detection gives two windows; total-volume
hourly detection gives 29 elevated buckets and 25 windows. Eight components have
insufficient baseline history under the daily default gate; the tool reports
that limitation rather than treating their lack of findings as normality.

## Tests, performance and reproduction

The complete Compose suite passed **627 tests, 0 failures, 11 skips in 42.70 s**,
including 90 new spike tests. Ten skips remain the legacy sample/staging tests;
one is the deployed HTTP endpoint test. Temporary HTTP servers passed both
protocol modes with all six tools and Pattern Analytics, Incident Tracing, VM
Performance and BGL spikes in the same sessions. No deployed service was updated.

An independent full-corpus oracle transferred only 5,017 aggregate rows, using
arithmetic timestamp buckets and Python inclusive-percentile interpolation and
window merging. It reproduced all 24 elevated buckets and 16 windows, including
the clock exclusion. Direct raw SELECTs independently reconciled MMCS message
counts and evidence. SELECT/VALUES fixtures exercise all severities, default zero
observations, missing/partial buckets, global clock ambiguity, missing epoch,
leap day, reference ranges, 99/100 support boundaries, strict threshold equality,
label flips, zero/overflow thresholds, long/empty/exact messages, missing nodes,
ties, SQL injection payloads, empty populations and independent output limits.
No fixture writes production data or creates database objects.

Five warm-cache execution/fetch measurements after one warm-up per case:

| Final complete SELECT, including contributors/evidence | Median |
| --- | ---: |
| Default 10 windows, 3 patterns, 1 reference each | 317.872 ms |
| One window, one pattern | 263.756 ms |
| Maximum 20 windows, 5 patterns, 3 references | 312.860 ms |
| Daily detection | 287.523 ms |
| Total-volume hourly detection | 386.373 ms |
| MMCS component | 239.990 ms |
| MMCS one-hour analysis / corpus baseline | 234.210 ms |
| MMCS one-hour analysis / explicit reference baseline | 233.533 ms |

These timings exclude connection setup, Python response formatting and MCP
overhead. They measure the full implementation's counts, diagnostics, exact
contributors and evidence; they are not the narrower research-prototype timings,
cold-cache measurements or latency guarantees. Broad queries scan the base
table, and SQL resource use still grows with source population and selected
windows. No new indexes or dependencies were introduced.

With the existing Compose database running:

```bash
./scripts/test-compose.sh
./scripts/test-compose.sh tests/test_spikes.py tests/test_spikes_integration.py
docker compose --project-name schubki-jury --env-file /dev/null -f docker-compose.yml \
  run --rm --no-deps -T -v "$PWD:/workspace:ro" -v "$PWD/artifacts:/evidence" \
  -w /workspace mcp-server python -B -m scripts.verify_log_spikes \
  --repetitions 5 --output /evidence/log_spikes_report.json
```

The ignored local report captures the generated schema, actual MCP requests and
full responses, SQL/parameters and timing samples. After a separately reviewed
MCP update, verify its deployed HTTP endpoint without changing the database:

```bash
./scripts/jury-compose.sh exec -T mcp-server python -m scripts.verify_mcp_http \
  --url http://127.0.0.1:8000/mcp --patterns --tracing --performance --spikes
```
