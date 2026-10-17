# Query catalog (`log_analysis/queries.py`)

The SQL behind the agent's tools. Each function validates its inputs and returns a
`Query` (SQL with bound `%(name)s` parameters, row limit, human-readable scope).
`run_query(conn, query)` executes it and returns the evidence the brief asks for:

```python
{
  "query": "search_logs", "scope": "bgl: full-text 'parity', ts >= 2005-09-01, ts < 2005-10-01",
  "sql": "...", "params": {...},
  "row_count": 5, "total_available": 485, "truncated": True, "elapsed_ms": 4.4,
  "rows": [{...}, ...]
}
```

`total_available` is the number of matching lines/groups in the whole filtered
scope, so the agent can report how often something occurs without fetching every
row. For `search_logs` it comes from a separate `SELECT count(*)` over the same
filters (`Query.count_sql`, run by `run_query`; both statements appear in `sql`);
for grouped queries from a `count(*) OVER ()` column. All counts are
**log lines, not incidents**.

Usage:

```python
import psycopg
from log_analysis import queries as q

with psycopg.connect(host="127.0.0.1", port=7890, user="postgres", autocommit=True) as conn:
    result = q.run_query(conn, q.search_logs("bgl", '"data TLB error interrupt"', alerts_only=True, limit=10))
```

Evidence for every query on the full corpus: `evidence/queries_report.md` (regenerate
with `.venv/bin/python scripts/verify_queries.py`). Timings and the index vs. scan
comparison: `evidence/performance.md` (`scripts/benchmark.py`).

## Schema the queries use

| Table | Key columns |
|---|---|
| `bgl_logs` | `line_no, ts, epoch, node, alert_label, is_alert, type, component, level, message` |
| `openstack_logs` | `source_file, line_no, log_file, service, ts, pid, level, component, request_id, user_id, tenant_id, instance_id, http_method, http_path, http_status, response_len, latency_s, is_anomaly, message` |
| `ownership` | `dataset, component, team, area` |
| `openstack_anomalous_instances` | `instance_id` |

Timestamps are `ts` (TIMESTAMP, as written in the logs), levels are `level`.
Full-text search goes through the indexes `bgl_logs_idx` / `openstack_logs_idx`
(`message @@ websearch_to_tsquery(...)`); the time filter runs inside the index scan.

Common optional filters (where the dataset has them): `start`, `end` (ISO timestamp,
`[start, end)`), `level`, `component`, `team`, `node` (BGL), `alert_label` (BGL),
`alerts_only` (BGL), `service` (OpenStack), `text` (full-text, web-search syntax).

## Engineer: what failed, where, and what happened around it

| Function | Purpose | Key inputs | Returns |
|---|---|---|---|
| `search_logs(dataset, text, start, end, ..., limit=20)` | Full-text search, oldest first. `text`: plain words must **all** occur (AND), `a OR b` matches either, `-word` excludes, `"quoted phrase"` must match in order. Quote dotted/dashed identifiers (`"nova.virt.libvirt"`), otherwise their parts are AND-ed anywhere in the line. | dataset, text, filters, limit ≤ 200 | matching lines + `total_available` |
| `nearby_events(dataset, line_no, source_file=None, before=10, after=10, same_node=False, window_seconds=300)` | Lines just before/after one hit, in file order. `same_node=True` (BGL): only that node's events within ± window. | line_no from a search hit (+ source_file for OpenStack) | lines with `offset_lines` (0 = anchor) |
| `instance_timeline(instance_id, limit=100)` | OpenStack: full history of one VM. | instance_id | lines incl. `is_anomaly` |
| `slowest_instances(step, limit=10)` | OpenStack: instances with the longest lifecycle step (default: spawn). | step text | instance_id, is_anomaly, seconds |

## Team lead: what recurs, how often, which teams

| Function | Purpose | Key inputs | Returns |
|---|---|---|---|
| `count_by(dataset, group_by, start, end, ..., limit=50)` | Line counts per key over the whole filtered scope. `group_by` BGL: `component, team, level, alert_label, node, rack`; OpenStack: `component, team, level, service, http_status, source_file`. | group key, filters | key, lines, pct, first/last_seen |
| `trend(dataset, bucket="day", start, end, split_by=None, ...)` | Counts per hour/day/week/month, optionally split by a group key (e.g. team). | bucket, filters | bucket, [split], lines |
| `recurring_patterns(dataset, start, end, ..., limit=20)` | Most frequent message patterns (numbers/hex/ids → `<*>`), with owning team and spread. | filters | pattern, component, team, lines, nodes/instances, active_days |
| `latency_summary(group_by="endpoint", ...)` | OpenStack HTTP latency by endpoint/method/status/service. | group key, slow threshold | requests, avg/p50/p95/p99/max, slow & error counts |
| `latency_trend(bucket="hour", endpoint=None, ...)` | OpenStack request count and p50/p95 per bucket. | bucket, endpoint template | bucket, requests, p50/p95/max |
| `error_rates(group_by="endpoint", ...)` | OpenStack 4xx/5xx share per endpoint/status, or WARNING/ERROR share per component/service (with team). | group key | counts and percentages |

## Manager: what to prioritise

| Function | Purpose | Key inputs | Returns |
|---|---|---|---|
| `team_priorities(dataset, start, end, recent_days=7)` | Per team: problem lines (BGL alerts / OpenStack WARNING+), distinct patterns, affected nodes/instances, active days, last `recent_days` vs. the `recent_days` before. Measured counts only. | window, recent_days | one row per team |
| `anomaly_comparison()` | OpenStack lifecycle step durations, labelled anomalous vs. normal instances. | – | per step: samples, avg/min/p50/p95/max |
| `corpus_overview()` | Whole base corpus: lines, span, problem lines per dataset. | – | one row per dataset |

## Notes for the tool layer

- Every value is bound; identifiers come from closed lists in `queries.py`. Invalid
  inputs raise `ValueError` before anything reaches the database.
- `limit` is capped at 200. Report `total_available` and `truncated` to the agent so it
  never treats a page of examples as the full count.
- Search semantics, checked against full scans on BGL: `error interrupt` → 154,201
  lines containing both words; `error OR interrupt` → 885,786; `error -interrupt` →
  403,841. Unquoted `nova.virt.libvirt` matches 49,506 OpenStack lines that merely
  contain all three words; quoted it is a phrase.
- BGL `level = FATAL` is not the same as an alert: use `alerts_only` / `alert_label`
  (the dataset's labels) for "real problems".
- OpenStack has 4 labelled anomalous instances (`is_anomaly`); all their lines are INFO,
  the anomaly shows in lifecycle timing (`anomaly_comparison`, `slowest_instances`).
