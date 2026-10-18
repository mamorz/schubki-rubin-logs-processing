# OpenStack VM lifecycle tracing

How to reliably rebuild one VM's full lifecycle from the existing
`openstack_logs` table, without schema changes, new indexes or reloads. This
follows on from [OpenStack incident tracing](openstack_incident_tracing.md).

All numbers come from SELECT statements run against the local SereneDB
(`26.09.1.3`, full corpus: 207,820 rows, 2,069 VMs). Timings are the median of three
warm runs, measured at the client. Every query is in
[`sql/examples/openstack_vm_tracing.sql`](../sql/examples/openstack_vm_tracing.sql)
(V1–V8):

```bash
scripts/jury-compose.sh --profile tools run --rm psql -f /sql/examples/openstack_vm_tracing.sql
```

## Recommended strategy

A VM's timeline is:

> **every line whose message contains the VM UUID**
> **∪ every line sharing one of the VM's *user* request IDs**
> ordered by `ts, source_file, line_no`.

```sql
WITH bg AS (SELECT request_id FROM openstack_logs WHERE request_id IS NOT NULL
            GROUP BY 1 HAVING count(DISTINCT instance_id) > 1 OR count(*) > 100),
reqs AS (SELECT DISTINCT request_id FROM openstack_logs
         WHERE instance_id = %(vm)s AND request_id IS NOT NULL
           AND request_id NOT IN (SELECT request_id FROM bg))
SELECT * FROM openstack_logs WHERE strpos(message, %(vm)s) > 0
UNION
SELECT l.* FROM openstack_logs l JOIN reqs USING (request_id)
ORDER BY ts, source_file, line_no
LIMIT 50;
```

Notes:
- The two halves need each other. The UUID match finds 28 of the 29 records for
  the example VM. Request expansion adds the boot `POST /servers`, which is the
  only line that contains no UUID.
- `UNION` (not `UNION ALL`) removes rows found by both methods. For the example
  VM, 17 of the 18 request-linked rows also match by UUID.
- Background request IDs are excluded **only when collecting the VM's request
  IDs** (`reqs`). They are not excluded from the UUID matches. VM Started, Paused
  and Resumed are logged under a background ID and are real lifecycle events.
- Run time for one VM is 35 ms (V7).

## Correlation methods

### Linking nova-api and nova-compute by `request_id` (V5, V8)

- Only two API calls share a request ID with nova-compute: the boot
  `POST /v2/{tenant}/servers` 202 and the `DELETE /v2/{tenant}/servers/{id}` 204.
- GET polls, the `network-vif-plugged` external event and every nova-scheduler
  line use their own request IDs.
- Out of 2,069 VMs, a request ID links:
  - the boot POST for **2,065**
  - the DELETE for **2,064**
  - both for **2,062**
- The largest user request has 12 lines and touches 1 VM (V2). Expanding by
  request ID therefore never pulls in another VM's lines.

### VM UUIDs inside nova-api URLs (V3, V4)

- All **2,067 of 2,067** distinct UUIDs in `/servers/{uuid}` URLs match a
  nova-compute `instance_id`.
- The 2 compute VMs with no API URL each have a single "VM Stopped" line at a
  file start.
- No log line mentions more than one known VM (V4). So
  `strpos(message, <vm uuid>)` can't mix VMs together.
- **Don't extract every UUID generically.** The messages also contain UUIDs that
  are not VMs:

| UUID kind | Where | Lines |
|---|---|---:|
| Image `0673dd71-34c5-4fbb-86c4-40623fbe45b4` | API `GET /images/{id}` | 144 |
| Same image | `nova.virt.libvirt.imagecache` (nova-compute) | 24,086 |
| Network port | `network-vif-plugged:<port uuid>` external events | 2,065 |

  Match a known VM UUID; don't extract arbitrary UUIDs.
- The tenant ID in URLs is 32 hex digits with no dashes, so the dashed UUID
  pattern doesn't match it.

### Record counts and deduplication (V5, whole corpus)

All sets are deduplicated on `(instance_id, source_file, line_no)` before
counting. A line that contains the same UUID twice, such as qemu-img paths, counts
once: 32 UUID mentions collapse to 24 lines.

| Method | Records | Extra over the previous method |
|---|---:|---:|
| M1 `instance_id = X` (parsed column) | 51,553 | — |
| M2 `strpos(message, X)`, a superset of M1 | 57,784 | +6,231 |
| M3 user-`request_id` expansion | 37,168 | (overlaps M1/M2) |
| **M2 ∪ M3 (recommended)** | **59,849** | **+2,065 over M2** |

Records that `instance_id` alone misses but the UUID match finds (+6,231):

| Service | Component | Event | Lines | VMs |
|---|---|---|---:|---:|
| nova-api | `nova.osapi_compute.wsgi.server` | `GET /servers/{id}` 200 | 2,066 | 2,066 |
| nova-api | `…server_external_events` | network-vif-plugged | 2,065 | 2,065 |
| nova-api | `nova.osapi_compute.wsgi.server` | `DELETE /servers/{id}` 204 | 2,064 | 2,064 |
| nova-compute | `oslo_service.periodic_task` | qemu-img "Could not open … disk: No such file" | 24 | 8 |
| nova-compute | `nova.virt.libvirt.driver` | "couldn't obtain the vcpu count from domain id" | 12 | 12 |

Records that the UUID match misses but request expansion finds (+2,065): the boot
`POST /servers` 202 (nova-api), one per VM.

## The four labelled anomalous VMs (V6)

| VM | `is_anomaly` true | M1 `instance_id` | M2 UUID | M3 request | **M2 ∪ M3** | User requests | Spawn |
|---|---:|---:|---:|---:|---:|---:|---:|
| 544fd51c-4edc-4780-baae-ba1d80a0acfc | 25 | 25 | 28 | 18 | **29** | 2 | 41.81 s |
| ae651dff-c7ad-43d6-ac96-bbcd820ccca8 | 25 | 25 | 28 | 18 | **29** | 2 | 29.46 s |
| a445709b-6ad0-40ec-8860-bec60b6ca0c2 | 25 | 25 | 28 | 18 | **29** | 2 | 50.72 s |
| 1643649d-2f42-4303-bfcd-7798baec19f9 | 25 | 25 | 28 | 18 | **29** | 2 | 31.27 s |

All four timelines are complete. Each contains:
- the boot POST
- the claim
- Creating image
- VM Started
- network-vif-plugged
- Instance spawned
- the DELETE
- VM Stopped

`is_anomaly` covers only 25 of the 29 records, because it is set only where
`instance_id` was parsed. To find labelled VMs, use the
`openstack_anomalous_instances` table, not the `is_anomaly` flag.

## Validated timeline: VM `544fd51c-4edc-4780-baae-ba1d80a0acfc`

Source file `openstack_abnormal`, date 2017-05-14. Output of V7: 29 records. In
MCP, `record_id = 1000000000 + line_no`. In the request column, **bg** marks the
background request ID `req-3ea4052c-…`.

| # | line | time | service | request | found by | event |
|---:|---:|---|---|---|---|---|
| 1 | 11899 | 21:07:36.772 | nova-api | req-d6143bd4 | **request** | `POST /servers` 202, 0.479 s |
| 2 | 11901 | 21:07:37.102 | nova-compute | req-d6143bd4 | uuid | Attempting claim: memory 2048 MB, disk 20 GB, vcpus 1 CPU |
| 3 | 11902 | 21:07:37.103 | nova-compute | req-d6143bd4 | uuid | Total memory: 64172 MB, used: 512.00 MB |
| 4 | 11903 | 21:07:37.103 | nova-compute | req-d6143bd4 | uuid | memory limit: 96258.00 MB, free: 95746.00 MB |
| 5 | 11904 | 21:07:37.104 | nova-compute | req-d6143bd4 | uuid | Total disk: 15 GB, used: 0.00 GB |
| 6 | 11905 | 21:07:37.105 | nova-compute | req-d6143bd4 | uuid | disk limit not specified, defaulting to unlimited |
| 7 | 11906 | 21:07:37.105 | nova-compute | req-d6143bd4 | uuid | Total vcpu: 16 VCPU, used: 0.00 VCPU |
| 8 | 11907 | 21:07:37.106 | nova-compute | req-d6143bd4 | uuid | vcpu limit not specified, defaulting to unlimited |
| 9 | 11908 | 21:07:37.138 | nova-compute | req-d6143bd4 | uuid | Claim successful |
| 10 | 11910 | 21:07:37.372 | nova-api | req-208eff6b | uuid | `GET /servers/{id}` 200, 0.201 s |
| 11 | 11911 | 21:07:37.924 | nova-compute | req-d6143bd4 | uuid | Creating image |
| 12 | 11960 | 21:08:12.571 | nova-compute | bg | uuid | **VM Started**, 34.6 s after Creating image (normal median 13.1 s) |
| 13 | 11961 | 21:08:12.636 | nova-compute | bg | uuid | VM Paused |
| 14 | 11962 | 21:08:12.757 | nova-compute | bg | uuid | sync_power_state: pending task (spawning), skip |
| 15 | 11976 | 21:08:19.714 | nova-api | req-c5e59d61 | uuid | Creating event network-vif-plugged |
| 16 | 11978 | 21:08:19.729 | nova-compute | bg | uuid | VM Resumed |
| 17 | 11979 | 21:08:19.735 | nova-compute | — | uuid | Instance spawned successfully |
| 18 | 11980 | 21:08:19.736 | nova-compute | req-d6143bd4 | uuid | Took 41.81 seconds to spawn the instance on the hypervisor |
| 19 | 11981 | 21:08:19.873 | nova-compute | bg | uuid | sync_power_state: pending task (spawning), skip |
| 20 | 11982 | 21:08:19.874 | nova-compute | bg | uuid | VM Resumed |
| 21 | 11983 | 21:08:19.900 | nova-compute | req-d6143bd4 | uuid | Took 42.81 seconds to build instance |
| 22 | 12004 | 21:08:27.258 | nova-api | req-483e1cca | uuid | `DELETE /servers/{id}` 204, 0.448 s |
| 23 | 12005 | 21:08:27.296 | nova-compute | req-483e1cca | uuid | Terminating instance |
| 24 | 12007 | 21:08:27.511 | nova-compute | — | uuid | Instance destroyed successfully |
| 25 | 12009 | 21:08:28.178 | nova-compute | req-483e1cca | uuid | Deleting instance files …_del |
| 26 | 12010 | 21:08:28.179 | nova-compute | req-483e1cca | uuid | Deletion of …_del complete |
| 27 | 12011 | 21:08:28.299 | nova-compute | req-483e1cca | uuid | Took 1.00 seconds to destroy the instance on the hypervisor |
| 28 | 12013 | 21:08:28.836 | nova-compute | req-483e1cca | uuid | Took 0.54 seconds to deallocate network for instance |
| 29 | 12035 | 21:08:42.508 | nova-compute | — | uuid | VM Stopped |

The timeline contains 5 request IDs in total:
- 2 user IDs that link API and compute: `req-d6143bd4` (boot) and `req-483e1cca` (delete)
- 2 API-only IDs: `req-208eff6b` (GET) and `req-c5e59d61` (vif event)
- 1 background ID

## Limitations

**Background request IDs (V1)**
- `req-addc1839-2ed5-4778-b57e-5854eb7b8b09` has 52,349 lines across 5
  components (imagecache, resource tracker and similar) and 0 VMs.
- `req-3ea4052c-895d-4b64-9e2d-04d64c4d94ab` has 12,248 lines in
  `nova.compute.manager`, covering lifecycle and power-sync events for 2,066 VMs.
- The rule `count(DISTINCT instance_id) > 1 OR count(*) > 100` selects exactly
  these two. It is a heuristic tuned to this corpus: user requests reach at most
  12 lines. Expanding by a background ID would pull in thousands of unrelated
  lines.

**Records with no UUID or request ID**
- A line with neither a VM UUID nor a user request ID can't be attributed to a
  VM. "Instance spawned successfully", "Instance destroyed successfully" and
  "VM Stopped" have a NULL request ID and are found only because they contain the
  UUID.
- nova-scheduler logs only host-level messages: "Successfully synced instances
  from host" (502 lines) and "instance sync … did not match" (364 lines). A VM's
  scheduling decision is not in these logs.

**Incomplete lifecycles (V8)**
- 7 VMs lack a linked boot POST, a linked DELETE, or both.
- 6 of them sit at the start or end of a log file, where the lifecycle is cut off.
  Two of those (`2b590f10`, `49ffab2b`) consist of a single "VM Stopped" line.
- `96c58299-fc8f-4be0-942e-8bcf09431769` (2017-05-17 06:24:44 → 06:25:06, 18
  lines) has no DELETE, yet it is not at a file edge and the file has no gap
  there. Two `GET servers/detail` 503s also occur at 06:25 on both days. Whether
  the two are related is **not established**.

**Cross-file ordering**
- Inside each file, line order matches time order: 0 lines go backwards in time.
  The three files cover separate time ranges, in the same order as the file
  numbers in `record_id`. So sorting by `ts` or by `record_id` gives the same
  order.
- Timestamps are not unique: 6,025 timestamp values are shared by 12,610 lines.
  Always add `source_file, line_no` (or `record_id`) as a tiebreaker.
- VM `0f079bdd-4117-4f6a-8b49-f3fb720b483c` appears in both `openstack_normal1`
  (9 lines, 06:24:49) and `openstack_normal2` (2 lines, 15:15:54–15:16:08). The
  logs have no coverage between 06:25 and 15:15, so this VM's timeline has a
  9-hour hole, and its DELETE is not in the logs.

**Other**
- `strpos` scans the whole table. That is fine at this size (35 ms per VM), but
  it is not indexed. `search_logs(match_mode="phrase", query=<uuid>)` returns the
  same 28 UUID-matched lines through the full-text index.
- `string_agg(DISTINCT …)` output order is not deterministic.

## Recommendations for MCP VM tracing

These are proposals; the API should be agreed with the MCP developer before
implementation.

1. **Use the recommended strategy above for an `instance_id` filter.** Combine a
   UUID match on `message` with request expansion over user request IDs, and
   deduplicate with `UNION`. Don't filter on the `instance_id` column alone: on
   this corpus that loses the GET, vif and DELETE API lines plus the qemu-img
   and libvirt warnings (6,231 lines), and the request expansion adds the boot
   POST (2,065 more).
2. **Return a `found_by` value per record** (`uuid` or `request`) and the
   `request_id`, so the agent can explain why each line is in the timeline.
3. **Derive the background request IDs once, at startup,** using V1. Report them
   in the provenance and never use them for expansion. Keep their UUID-matched
   lines.
4. **Order results by `ts, record_id`.** Report `truncated` and the total count.
   A full lifecycle is 25–29 records, so the existing limit of 50 fits one VM.
5. **Flag incomplete lifecycles.** Report whether a boot POST and a DELETE were
   found. When either is missing, say the lifecycle may be cut off at a file edge
   or a gap in log coverage; don't infer a failure.
6. **Don't use `is_anomaly` to find labelled VMs.** Use
   `openstack_anomalous_instances`.
