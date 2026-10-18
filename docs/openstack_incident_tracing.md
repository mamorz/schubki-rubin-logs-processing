# OpenStack incident tracing with the existing schema

This note covers which incident-tracing questions the loaded `openstack_logs` data
can answer, without schema changes, new indexes or reloads. Every number here
comes from a SELECT that was actually run against the local SereneDB
(`26.09.1.3`, `127.0.0.1:17890`, full 207,820-row OpenStack corpus). Timings are
the median of three warm runs measured at the client, including network
round-trip. The queries are in
[`sql/examples/openstack_tracing.sql`](../sql/examples/openstack_tracing.sql):

```bash
scripts/jury-compose.sh --profile tools run --rm psql -f /sql/examples/openstack_tracing.sql
```

## 1. Data-quality audit

The table has 207,820 rows from three source files:
- `openstack_abnormal`: 2017-05-14 19:39 → 21:56
- `openstack_normal1`: 2017-05-16 00:00 → 06:25
- `openstack_normal2`: 2017-05-16 15:15 → 2017-05-17 12:02

| Field | Non-null | % | Distinct | Can it link records? / limitations |
|---|---:|---:|---:|---|
| `ts` | 207,820 | 100 | 201,235 | Millisecond precision. Ties exist, so order by `ts, source_file, line_no`. |
| `service` | 207,820 | 100 | 3 | nova-api 102,868, nova-compute 104,086, nova-scheduler 866. |
| `level` | 207,820 | 100 | 4 | INFO 204,506, WARNING 3,118, ERROR 194, CRITICAL 2. **The abnormal file has 0 ERROR/CRITICAL lines.** |
| `component` | 207,820 | 100 | 12 | Python logger name. Reliable. |
| `message` | 207,820 | 100 | 139,782 | Full-text indexed. Instance UUIDs inside it can be found with `phrase` search. |
| `request_id` | 192,174 | 92.5 | 90,406 | **Partly links records.** 4,129 IDs span nova-api and nova-compute (37,162 lines), which are real boot or delete flows. 86,277 IDs occur in one service only, mostly single GET polls. **Two IDs are background contexts, not user requests:** `req-addc1839-…` (52,349 lines) and `req-3ea4052c-…` (12,248 lines), used by imagecache, resource-tracker and lifecycle callbacks. nova-scheduler IDs never match API or compute IDs. |
| `user_id` / `tenant_id` | 115,504 | 55.6 | 4 / 2 | NULL wherever the context is `[-]`. Too few values to be useful for segmentation. |
| `instance_id` | 51,553 | 24.8 | 2,069 | **Set on nova-compute lines only** (parsed from `[instance: …]`). nova-api lines carry the UUID only in the URL or event text, so match those with `strpos(message, …)`. |
| `http_method` / `http_path` / `http_status` / `response_len` / `latency_s` | 98,733 | 47.5 | 3 / 2,453 / 5 / 71 / 69,170 | **Only on nova-api wsgi access lines.** There is no HTTP latency for compute or scheduler. Compute durations are only in message text ("Took N seconds to …"). |
| `is_anomaly` | 207,820 | 100 | 2 | Only **100** rows are true: 4 labelled VMs × 25 compute lines. It depends on `instance_id`, so it **misses 3 nova-api lines per VM** (12 total). It's a label for 4 injected VMs, not an anomaly detector. |
| `openstack_anomalous_instances` | 4 rows | | | Ground-truth labels from `anomaly_labels.txt`. |

## 2. Which questions the data supports

| Question | Supported? | How / caveat |
|---|---|---|
| What happened to request X? | **Yes, for the 4,129 cross-service IDs** | Q1. Exclude the two background IDs. |
| Which services logged request X? | **Yes (api + compute only)** | No request ID is shared with nova-scheduler. |
| Which VMs have the most anomalous events? | **Weakly** | All 4 labelled VMs have exactly 28 lines (25 flagged), so ranking by count means nothing. Spawn duration (Q3) is the signal that actually separates them. |
| Which HTTP error codes are most frequent? | **Yes** | Q5. 404 POST `os-server-external-events` 2,064; 404 GET metadata `user_data` 1,905; 503 GET `servers/detail` 2. |
| Is latency associated with services or errors? | **API only** | Q5. Compute spawn time comes from message text (Q3, Q6). |
| Can the event sequence be reconstructed? | **Yes, per instance** | Q2. UUID match ordered by time gives the full claim → spawn → delete → stop lifecycle (median 25 lines per VM). |

## 3. Tested queries

### Q1 — What happened to one request? (19 ms)
**Question:** Which API call started this flow, and what did compute do for it?
Filter: `request_id = 'req-d6143bd4-9784-4e95-a0c8-83fba00530b6'`.

**Result: 12 rows.**
- Row 1 is nova-api `POST /v2/…/servers`, status 202, latency 0.479 s, at 2017-05-14 21:07:36.772.
- The other 11 are nova-compute lines with `instance_id = 544fd51c-…`:
  - 7 claim lines, then "Claim successful" at 21:07:37.138
  - "Creating image" at 21:07:37.924
  - "Took 41.81 seconds to spawn the instance on the hypervisor." at 21:08:19.736
  - "Took 42.81 seconds to build instance." at 21:08:19.900

**Limitations:**
- Lifecycle events (VM Started, Paused, Resumed) are logged under the background ID `req-3ea4052c-…`, so a request trace alone misses them. Use Q2 for the full VM story.

### Q2 — Timeline of one VM (28 ms)
**Question:** What is the full ordered sequence for one instance?
Filter: `instance_id = '544fd51c-4edc-4780-baae-ba1d80a0acfc' OR strpos(message, '544fd51c-…') > 0`.

**Result: 28 rows across nova-api and nova-compute, with 5 request IDs** (full timeline in §5). Lines that only `strpos` catches:
- the API `GET /servers/{id}`
- the API `DELETE`
- the `network-vif-plugged` external event

**Limitations:**
- `strpos` is a substring scan with no index (still under 30 ms at this size).
- `is_anomaly` is false on the 3 API lines even for a labelled VM.

### Q3 — Spawn time, labelled vs not (18 ms, whole corpus)
**Question:** Do the labelled VMs behave differently in a measurable way?

**Result:**

| is_anomaly | spawns | p50 s | p95 s | max s |
|---|---:|---:|---:|---:|
| false | 2,062 | 19.74 | 20.99 | 26.69 |
| true | 4 | 36.54 | 49.38 | 50.72 |

**Limitations:**
- Durations are parsed with a regex from message text.
- With n = 4 for the labelled group, this is a description of 4 VMs, not a statistical test.

### Q4 — Coverage of the labelled anomalous VMs (56 ms)
**Question:** Where do the labelled VMs appear, and when?

**Result:** each VM has 28 lines, 25 flagged, 5 request IDs, and appears in both nova-api and nova-compute.

| instance | first seen | last seen |
|---|---|---|
| 544fd51c-4edc-4780-baae-ba1d80a0acfc | 21:07:37.102 | 21:08:42.508 |
| ae651dff-c7ad-43d6-ac96-bbcd820ccca8 | 21:08:40.907 | 21:09:33.816 |
| a445709b-6ad0-40ec-8860-bec60b6ca0c2 | 21:42:42.319 | 21:43:56.880 |
| 1643649d-2f42-4303-bfcd-7798baec19f9 | 21:46:48.622 | 21:47:43.028 |

All on 2017-05-14, in two clusters.

**Limitations:**
- The order of `string_agg(DISTINCT service)` is not deterministic.

### Q5 — HTTP status and latency (16 ms, whole corpus)
**Question:** Which status codes occur, and how slow are they?

**Result:**

| method | status | requests | p50 s | p95 s | max s |
|---|---:|---:|---:|---:|---:|
| GET | 200 | 88,568 | 0.260 | 0.360 | 4.164 |
| POST | 202 | 2,065 | 0.504 | 0.648 | 1.793 |
| POST | 200 | 2,065 | 0.095 | 0.108 | 2.145 |
| POST | 404 | 2,064 | 0.090 | 0.101 | 0.720 |
| DELETE | 204 | 2,064 | 0.261 | 0.297 | 1.251 |
| GET | 404 | 1,905 | 0.215 | 0.265 | 0.575 |
| GET | 503 | 2 | 0.009 | 0.010 | 0.010 |

What the paths show (follow-up query on `http_path`):
- All 2,064 POST 404s are `os-server-external-events`.
- All 1,905 GET 404s are metadata `/openstack/2013-10-17/user_data`.
- Both 503s are `GET servers/detail`, at 06:25 on 2017-05-16 and 2017-05-17.

**Limitations:**
- nova-api only.
- These are counts of log lines, not incidents.
- The 404 counts match the boot and delete volumes one-to-one, which suggests routine behaviour of the test workload rather than failures. That is a hypothesis, not established.

### Q6 — Which spawn phase is slow? (19 ms, whole corpus)
**Question:** Where inside the spawn does the extra time go?

**Result:** per-VM durations in seconds, measured between lifecycle messages:

| anomalous | VMs | "Creating image" → "VM Started" p50 / max | "VM Started" → "spawned" p50 / max |
|---|---:|---|---|
| false | 2,061 | 13.1 / 19.8 | 6.6 / 8.2 |
| true | 4 | **29.9 / 44.5** | 6.6 / 7.2 |

**Limitations:**
- Phase boundaries are inferred from message text.
- VMs missing any of the three messages are excluded.

## 4. Gaps in the MCP interface

These were checked against `log_insights/tools.py` and `log_insights/queries.py`, plus live calls on 2026-10-18:

1. **Records hide the structured columns.** `search_logs` and `get_nearby_events` return only `record_id, event_id, timestamp, component, severity, message`. `service`, `request_id`, `instance_id`, `http_status`, `latency_s` and `is_anomaly` are invisible to the agent.
2. **No request-ID lookup.** `request_id` was parsed out of `message` at load time, so full-text search can't find it. Q1 can't be done through MCP.
3. **Instance trace works, but blind.** `search_logs(dataset="openstack", query="544fd51c-4edc-4780-baae-ba1d80a0acfc", match_mode="phrase")` returns all 28 lines (`total_corpus_match_count` 28, not truncated). Without `service` or `request_id`, the agent can't tie compute steps to the API call that started them.
4. **`query` is mandatory.** There is no structured-only filter (for example "all lines for this request, in order").
5. **Nearby events are global.** `get_nearby_events(record_id=1000011911, before=3, after=8)` returned 11 neighbours; 8 were unrelated `GET servers/detail` polls and 1 belonged to another VM. There is no same-request or same-instance option.
6. **Summaries can't use OpenStack fields.** There's no `group_by` service, http_status or instance, no anomaly filter, and no latency or duration statistics (Q3, Q5, Q6). This is left to the ongoing pattern-aware aggregation work.

## 5. Recommended minimal extension (proposal, not implemented)

Extend **`search_logs`** for `dataset="openstack"` and leave `summarize_logs`
alone, since the MCP developer is working on aggregation there.

- Add optional `request_id: str` → `request_id = %(request_id)s`.
- Add optional `instance_id: str` → `(instance_id = %(instance_id)s OR strpos(message, %(instance_id)s) > 0)`. The `strpos` branch catches the API URL and event lines.
- Make `query` optional when either ID filter is given. Results are then ordered by `ts, record_id`, which gives a trace view.
- Add `service`, `request_id`, `instance_id`, `http_status`, `latency_s` and `is_anomaly` to OpenStack records.
- Reject the ID filters for `dataset="bgl"`.
- Keep the existing limit (50), `total_corpus_match_count`, `truncated` and SQL/parameter provenance.

This turns Q1 and Q2 into single tool calls and lets the agent see which
service and request each line belongs to.

**Estimated effort: 0.5–1 day**:
- query-plan builder
- tool signature and description
- SQL-generation unit tests
- one integration test against the full corpus
- README notes

All values stay bound parameters and every query stays SELECT-only.
The API change should be agreed with the MCP developer before implementation.

## 6. Demo scenario: slow boot of VM `544fd51c-4edc-4780-baae-ba1d80a0acfc`

**Start:** a VM labelled anomalous in `openstack_anomalous_instances` (Q4).

**Evidence (Q1, Q2; source `openstack_abnormal`, 2017-05-14):**

| line_no | time | service | request | event |
|---:|---|---|---|---|
| 11899 | 21:07:36.772 | nova-api | req-d6143bd4 | `POST /servers` 202, 0.479 s |
| 11908 | 21:07:37.138 | nova-compute | req-d6143bd4 | Claim successful (2048 MB, 20 GB, 1 vCPU) |
| 11911 | 21:07:37.924 | nova-compute | req-d6143bd4 | Creating image |
| 11960 | 21:08:12.571 | nova-compute | req-3ea4052c (bg) | VM Started, **34.6 s after Creating image** |
| 11961 | 21:08:12.636 | nova-compute | req-3ea4052c (bg) | VM Paused |
| 11976 | 21:08:19.714 | nova-api | req-c5e59d61 | network-vif-plugged event |
| 11978 | 21:08:19.729 | nova-compute | req-3ea4052c (bg) | VM Resumed |
| 11980 | 21:08:19.736 | nova-compute | req-d6143bd4 | Took 41.81 s to spawn |
| 12004 | 21:08:27.258 | nova-api | req-483e1cca | `DELETE /servers/{id}` 204, 0.448 s |
| 12011 | 21:08:28.299 | nova-compute | req-483e1cca | Took 1.00 s to destroy |
| 12035 | 21:08:42.508 | nova-compute | — | VM Stopped |

MCP record IDs are `1000000000 + line_no` (for example 1000011911).

**Comparison with the whole corpus (Q3, Q6):**
- Normal spawn: p50 19.74 s, p95 20.99 s (n = 2,062).
- This VM: 41.81 s.
- Normal "Creating image → VM Started": median 13.1 s, max 19.8 s. This VM: 34.6 s.
- "VM Started → spawned" is normal at about 7 s.

**Observed facts**
- All 4 labelled VMs spawned in 29.46–50.72 s. Every one of the 2,062 unlabelled spawns took ≤ 26.69 s.
- The extra time falls between "Creating image" and "VM Started". Network plug and resume timing is normal.
- No WARNING, ERROR or CRITICAL line mentions any labelled VM. The abnormal file contains no ERROR or CRITICAL lines at all. **This anomaly is silent; only duration reveals it.**
- The labelled VMs fall in two windows on 2017-05-14: 21:07–21:09 and 21:42–21:47.

**Hypotheses (not established by these logs)**
- Slow image or backing-file preparation, or disk I/O contention on the compute host.
- A delayed libvirt or QEMU domain start.
- The logs don't record which fault was injected.

**Recommended next checks**
1. Compare host disk and I/O and libvirt metrics for 21:07–21:09 and 21:42–21:47. These are not in this corpus.
2. Inspect VM `d7d68893-c44f-4e7c-8d4b-0e7801d62776`: the slowest unlabelled spawn (26.69 s), at 21:44:37 → 21:45:04, inside the second window. It's a candidate for an unlabelled affected VM. Note its image → started time (19.8 s) is the normal-group maximum.
3. Add an alert when spawn time is above roughly 25 s. That threshold sits above the normal maximum seen in the logs, so it would have caught all 4 labelled VMs.

**Per role**
- **Engineer:** run Q2 and Q1 for the timeline, then check host I/O for the window.
- **Team lead:** 4 of 2,066 spawns (0.19%) were slow. All were in nova-compute, in two windows, with no error lines.
- **Manager:** a silent slow-boot pattern that error-count dashboards don't catch. Fund a spawn-duration alert. Impact on users or cost is unknown from these logs.
