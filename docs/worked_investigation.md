# Worked investigation: the BGL "rts panic" kernel failure

One real, recurring problem in the base data, investigated through the RUBIN agent UI by all three
roles. Each role asked its own question, chose its own tools and produced its own answer and next
step from the same MCP tools and database.

| | |
|---|---|
| **Issue** | BGL `KERNEL` FATAL message `rts panic! - stopping execution` (a compute-node kernel halt), compared with OpenStack problems for prioritisation |
| **Run** | 2026-10-18, RUBIN agent UI → Kiro CLI 2.32.0 → RUBIN v1.5.1 role agents → MCP server (6 tools) → SereneDB 26.09.1.3, full base corpus |
| **Sessions** | History cleared before the run, so each role started a fresh Kiro session without earlier context |
| **Evidence** | Full-text search, nearby events, whole-corpus aggregations, team grouping, spike detection and spawn analytics over BGL **and** OpenStack |

**How the numbers were checked.** The screenshots of the run show every tool call, its arguments
and results in the UI. To keep the executed queries and results checkable, we re-ran the agents'
key tool calls with the same arguments against the same read-only MCP server. The full responses,
including the executed SQL and bound parameters, are in [`worked_investigation/evidence/`](worked_investigation/evidence/),
and [`reproduce_calls.py`](worked_investigation/reproduce_calls.py) re-creates them. Every number
below was compared with those responses. Where an agent's statement did not match, it is marked
**⚠ correction**.

---

## Role 1 — Engineer: what failed, and what happened around it?

**Question**

> In BGL, search for the phrase "rts panic". Take the earliest occurrence, show what happened just
> before and after it in the same component, and tell me what failed, in which component, a
> possible cause and the next check.

**Tool calls** (2 calls, 25.7 s, 0.425 Kiro credits)

| # | Tool | Arguments | Result | Evidence |
|---|---|---|---|---|
| 1 | `search_logs` | `dataset=bgl`, `query="rts panic"`, `match_mode=phrase`, `limit=20` | **3,983** matches in the whole corpus; 20 returned, **truncated**; earliest = record **33937**, 2005-06-05 01:55:06.697, `KERNEL`, `FATAL`, `rts panic! - stopping execution` | [E1](worked_investigation/evidence/E1_search_rts_panic.json) |
| 2 | `get_nearby_events` | `dataset=bgl`, `record_id=33937`, `same_component=true`, `before=10`, `after=10` | 10 events before (all `FATAL`, within 1.62 s), 10 after (all `INFO`, 2.70–3.27 s later) | [E2](worked_investigation/evidence/E2_nearby_33937.json) |

**Executed SQL** (from the tool's provenance; values are bound parameters)

```sql
-- search_logs, records (text = 'rts panic', fetch_limit = 21 → 20 returned + truncation check)
SELECT line_no AS record_id, CAST(NULL AS VARCHAR) AS event_id, ts AS timestamp,
       component, level AS severity, message
FROM bgl_logs_idx
WHERE message @@ ts_phrase(%(text)s)
ORDER BY ts, line_no
LIMIT %(fetch_limit)s;

-- search_logs, complete count (independent of the limit)
SELECT COUNT(*) AS total_corpus_match_count
FROM bgl_logs_idx
WHERE message @@ ts_phrase(%(text)s);

-- get_nearby_events, events before the anchor in the same component
-- (anchor_time = '2005-06-05T01:55:06.697467', record_id = 33937, component = 'KERNEL', neighbor_limit = 10)
SELECT line_no AS record_id, CAST(NULL AS VARCHAR) AS event_id, ts AS timestamp,
       component, level AS severity, message
FROM bgl_logs
WHERE (ts < %(anchor_time)s OR (ts = %(anchor_time)s AND line_no < %(record_id)s))
  AND component = %(component)s
ORDER BY ts DESC, line_no DESC
LIMIT %(neighbor_limit)s;
```

**Relevant results**

| Offset | Severity | Message (abridged) |
|---|---|---|
| −1.62 s … −0.47 s | FATAL | general-purpose register dump (`0:00000000 1:1fefff30 …`, `28:30001460 …`) |
| −0.32 s | FATAL | `special purpose registers:` |
| −0.17 s | FATAL | `lr:003625f0 cr:20000000 xer:00000002 ctr:0037f084` |
| **0** | **FATAL** | **`rts panic! - stopping execution`** (record 33937) |
| +2.70 s … +3.27 s | INFO | `25 ddr errors(s) detected and corrected on rank 0, symbol 23, bit 0`, … and `L3 EDRAM error(s) … detected and corrected` |

**Answer (summary of the agent's response)**

- **What failed:** a kernel-level runtime panic on a compute node. The `KERNEL` component dumped
  its full register state and halted (`rts panic! - stopping execution`). The phrase occurs in
  3,983 records, so it is not a one-off message.
- **Possible cause, labelled as hypotheses:**
  - **H1, memory hardware degradation:** corrected DDR (rank 0, several symbols/bits) and L3 EDRAM
    errors on the same component point to memory trouble.
  - **H2, crash-recovery artefact:** those errors appear *after* the panic, so they could be found
    during crash handling rather than cause it.
  - The agent states that timing alone confirms neither; the register dump is the stronger lead.
- **Next checks:** (1) map the link register `lr:003625f0` to the kernel source to find the failing
  code path; (2) search for DDR/rank-0 errors *before* 2005-06-05 01:55:06 to test H1 against H2;
  (3) check whether the same memory-error pattern surrounds other "rts panic" events.

**Verified:** all numbers above match the re-run. The agent called 3,983 "sample-verified, truncated
at 20 returned", which is correct: the total is a complete count, the records are a sample.

---

## Role 2 — Team lead: how often does it recur, and who should look first?

**Question**

> How often does the BGL "rts panic" pattern recur across the whole corpus? Show the weekly trend,
> which components and (fictional) owning teams are involved, and what the team should investigate
> first.

**Tool calls** (7 calls, 44.5 s, 0.719 Kiro credits)

| # | Tool | Arguments (as shown in the UI) | Result | Evidence |
|---|---|---|---|---|
| 1–4 | `summarize_logs` ×4 | `dataset=bgl`, `query="rts panic"`, `match_mode=phrase`, `group_by=time` (`interval=week`) / `component` / `team` / `severity` | **3,983** records, all complete (not truncated): **32** weekly buckets 2005-05-30 → 2006-01-02; component `KERNEL` = 3,983; team **Compute Kernel** = 3,983 (100 % mapping coverage); severity `FATAL` = 3,983 | [T1](worked_investigation/evidence/T1_weekly.json), [T2](worked_investigation/evidence/T2_component.json), [T3](worked_investigation/evidence/T3_team.json), [T4](worked_investigation/evidence/T4_severity.json) |
| 5 | `search_logs` | phrase "rts panic" in the peak week 2005-06-13 … 06-20 | 2,238 matches (sample returned, truncated) | [T6](worked_investigation/evidence/T6_peak_week_search.json) |
| 6 | `search_logs` | phrase "rts panic" in a later, low-volume period | 137 matches (sample, truncated) | — (arguments not captured) |
| 7 | `get_nearby_events` | `dataset=bgl`, `record_id=382921` (peak week) | context around 2005-06-13 15:04:06 | [T7](worked_investigation/evidence/T7_nearby_382921.json) |

**Executed SQL** (weekly trend; the component/team/severity calls differ only in the `GROUP BY`)

```sql
-- summarize_logs (text = 'rts panic', fetch_limit = 101)
SELECT date_trunc('week', ts) AS bucket_start, COUNT(*) AS record_count
FROM bgl_logs
WHERE line_no IN (SELECT line_no FROM bgl_logs_idx WHERE message @@ ts_phrase(%(text)s))
GROUP BY date_trunc('week', ts)
ORDER BY bucket_start
LIMIT %(fetch_limit)s;
```

**Relevant results: weekly trend (complete, 32 weeks)**

| Week starting | Records | Share |
|---|---|---|
| **2005-06-13** | **2,238** | **56.2 %** |
| 2005-08-29 | 439 | 11.0 % |
| 2005-08-08 | 308 | 7.7 % |
| 2005-06-27 | 217 | 5.4 % |
| 2005-08-15 | 187 | 4.7 % |
| 2005-07-11 | 152 | 3.8 % |
| 2005-05-30 | 91 | 2.3 % |
| 2005-08-22 | 72 | 1.8 % |
| 2005-09-05 … 2005-12-26 | 2–23 per week | — |
| 2006-01-02 | 1 | 0.03 % |

Context around a peak-week panic (record 382921, 2005-06-13 15:04:06):
- **~5.3 s before:** repeated `KERNEL INFO ciod: pollControlDescriptors: Detected the debugger died`.
- **0.12 s before:** `ciod: Message code 2 is not 3 or 4294967295`.
- **Same instant:** three more `rts panic!` records.
- **~70 min later:** `LINKCARD` bit sparing.
- **~2.1 h later:** `APP FATAL ciod: LOGIN chdir(...) failed`.

**Answer (summary)**

- **Recurrence:** 3,983 records across all 32 weeks of the corpus, with no week without a match.
  All are `KERNEL`/`FATAL` with the identical message, owned by the fictional **Compute Kernel**
  team.
- **Trend:** strongly uneven. One week (2005-06-13) holds 56 %, there are secondary clusters in
  late June, mid-July and August, and a low tail from September on. The agent explicitly does
  **not** call the tail "resolved": lower volume is not proof of a fix.
- **Context:** the traced panic arrived as a burst of 4 records in the same instant, after
  `ciod … debugger died` messages. The agent labels this a correlation from one sample, not a
  cause, and notes that 2,238 records in the peak week are not 2,238 independent failures.
- **Investigation order for the team:** (1) the 2005-06-13 peak week: run `detect_log_spikes` for
  `KERNEL` and trace several records to see whether the `ciod … debugger died` → panic sequence
  repeats; (2) the secondary clusters, to tell one recurring mechanism from several triggers;
  (3) the long tail, without assuming that the drop means a fix.
- **Stated limits:** counts are log records, not incidents or nodes; one traced neighbourhood
  only; ownership is fictional.

**Verified:** all counts match the re-run. The claim that every record carries the identical
message is confirmed by an exact-message count: 3,983 records equal `rts panic! - stopping
execution` ([T5](worked_investigation/evidence/T5_exact_message.json)). One small gap: the agent
said `search_logs` exposes no node IDs, which is true, but `detect_log_spikes` reports distinct
node counts and could have answered how many nodes were affected.

---

## Role 3 — Manager: what deserves attention first?

**Question**

> Across BGL and OpenStack, which observed problems deserve attention first? Compare the BGL
> "rts panic" pattern with error-level records per owning team in both datasets and with
> OpenStack slow VM launches. Give up to three priorities with the next action for each, without
> inventing impact.

**Tool calls** (9 calls, 100.7 s, 1.851 Kiro credits; the main ones)

| Tool | Arguments | Result | Evidence |
|---|---|---|---|
| `search_logs` | phrase "rts panic", BGL | 3,983 matches | [E1](worked_investigation/evidence/E1_search_rts_panic.json) |
| `summarize_logs` | phrase "rts panic", `group_by=time`, `interval=day` | **2,049** records on 2005-06-14 alone (51.5 %); daily series truncated | [M2](worked_investigation/evidence/M2_daily.json) |
| `summarize_logs` | BGL, `severity=FATAL`, `group_by=team` | 855,501 FATAL records: **Compute Kernel 626,756**, HPC Applications 228,536, Hardware & Interconnect 206, Cluster Control 3 | [M3](worked_investigation/evidence/M3_bgl_fatal_team.json) |
| `summarize_logs` | OpenStack, `severity=ERROR`, `group_by=team` | 194 ERROR records: **Virtualization 192**, Cloud API 2 | [M4](worked_investigation/evidence/M4_os_error_team.json) |
| `analyze_vm_spawn_performance` | OpenStack | 2,066 spawns, median 19.74 s, P95 21.01 s, max 50.72 s; **5** slow (> 26.26 s), all on 2017-05-14 | [M5](worked_investigation/evidence/M5_spawn.json) |
| `search_logs` | OpenStack, `query=error`, `severity=ERROR` | 16 matches: `InvalidDiskInfo … qemu-img failed` and `Error during ComputeManager._run_image_cache_manager_pass` (periodic task), 2017-05-16/17 | [M6](worked_investigation/evidence/M6_os_error_search.json) |
| `detect_log_spikes` | BGL, hourly, 10 findings | 16 spike windows (top 10 returned); largest returned: `KERNEL` 2005-06-14 09:00–12:00, **344,064** adverse records from 22,528 distinct nodes; top messages `data storage interrupt` (47,104), `instruction address: 0x00004ed8` (30,720), `data address: 0x00000002` (14,336) | [M7](worked_investigation/evidence/M7_spikes_hour.json) |

**Executed SQL** (FATAL records per team; the fictional ownership join)

```sql
-- summarize_logs (severity = 'FATAL', ownership_dataset = 'bgl', fetch_limit = 21)
SELECT COALESCE(o.team, 'UNMAPPED') AS team, COUNT(*) AS record_count
FROM bgl_logs AS l
LEFT JOIN ownership AS o
  ON o.dataset = %(ownership_dataset)s AND o.component = l.component
WHERE l.level = %(severity)s
GROUP BY COALESCE(o.team, 'UNMAPPED')
ORDER BY record_count DESC, team
LIMIT %(fetch_limit)s;
```

The spawn and spike analyses are single, larger read-only statements (CTEs over the full
population); their complete SQL and parameters are in [M5](worked_investigation/evidence/M5_spawn.json)
and [M7](worked_investigation/evidence/M7_spikes_hour.json).

**Answer (summary): three priorities; operational impact unverified throughout**

1. **BGL `KERNEL` FATAL cascade on 2005-06-14 — investigate first** (fictional owner: Compute
   Kernel).
   - "rts panic" is concentrated on that day (2,049 of 3,983 records) but is only a small part of
     a much larger event: the largest of the returned spike windows, 344,064 adverse records
     across 22,528 nodes, dominated by `data storage interrupt`.
   - It is 0.47 % of all 855,501 BGL FATAL records.
   - *Next action:* determine whether the 22,528-node `data storage interrupt` cascade is a shared
     storage/controller failure or independent node events, and whether "rts panic" is a
     downstream symptom of it.
2. **OpenStack Virtualization ERROR records** (fictional owner: Virtualization).
   - 192 of 194 OpenStack ERROR records, sampled as `qemu-img` disk-info failures from the image
     cache manager on 2017-05-16/17.
   - *Next action:* confirm whether all 192 follow this pattern, identify affected hosts and
     instances, and check for shared infrastructure with the slow launches.
3. **OpenStack slow VM launches** (fictional owner: Virtualization).
   - 5 of 2,066 (0.24 %), all on 2017-05-14; no deterioration trend over the three days.
   - The threshold (baseline P95 × 1.25) is a classification threshold, not an SLA.
   - *Next action:* trace the slowest instance (`a445709b-…`, 50.72 s) and check whether it shares
     a host with the `qemu-img` errors.

**Key comparison note from the agent:** BGL (2005–2006) and OpenStack (2017) are separate systems
from different periods, so raw counts are not directly comparable. The ranking is by breadth of
affected systems and how actionable the diagnosis is, not by business impact.

**Verified, with two corrections:**
- All aggregation, spawn and spike numbers match the re-run.
- **⚠ correction:** the agent described "rts panic" as recurring "at low daily volumes (1–14
  records/day) through January 2006". That holds from 2005-09-05 onwards, but the day before,
  2005-09-04, had **425** records, the second-highest day. The Team lead's weekly view shows this
  correctly (week of 2005-08-29: 439).
- **⚠ correction:** the agent said the sampled OpenStack ERROR records "all share" request
  `req-addc1839-…`. In the 15 returned records, the 8 image-cache-manager errors share it, while the
  7 `InvalidDiskInfo` records have no request ID.

---

## Search evidence and aggregation, side by side

| Kind | Used for | Scope |
|---|---|---|
| Full-text search (`search_logs`, phrase mode) | finding the actual records, earliest occurrence, samples per week, OpenStack error messages | sample of ≤ 20 records + complete match count; truncation reported |
| Nearby events (`get_nearby_events`) | what happened just before/after one record | 10 + 10 events around one anchor |
| Aggregation (`summarize_logs`) | how often, which component/team/severity, weekly and daily trend, FATAL/ERROR per team | **whole base corpus**, complete counts per dataset |
| Spike detection (`detect_log_spikes`) | whether the panic sits inside an abnormal volume burst | whole BGL corpus, per-component baselines |
| Spawn analytics (`analyze_vm_spawn_performance`) | OpenStack launch performance for comparison | all 2,066 OpenStack spawn events |

## What the data establishes — and what it does not

**Established (measured over the complete filtered data):**
- `rts panic! - stopping execution` occurs in **3,983** BGL records, all `KERNEL`/`FATAL`, all
  with the identical message, in every one of the 32 weeks from 2005-05-30 to 2006-01-02.
- The pattern is highly concentrated: 56 % in the week of 2005-06-13, 51 % on 2005-06-14 alone.
- 2005-06-14 09:00–12:00 is the largest of the returned BGL spike windows (top 10 of 16):
  `KERNEL`, 344,064 adverse records, 22,528 distinct nodes.
- In the traced cases, panics are preceded by register dumps (and in the peak week by `ciod …
  debugger died` messages) and followed by corrected memory errors.
- OpenStack: 192 of 194 ERROR records belong to the (fictional) Virtualization team's components;
  5 of 2,066 VM launches were slow, all on 2017-05-14.

**Not established:**
- **The root cause** of the panics. Memory errors, debugger messages and the storage-interrupt
  cascade are correlated in time, not proven causes. The Engineer's hypotheses H1/H2 stay open.
- **How many incidents or nodes** are behind the 3,983 records. Bursts of identical records
  within the same instant show they are not independent failures.
- **Whether the problem was fixed.** The tail continues at a low level until the corpus ends.
- **Any operational or business impact** (downtime, lost jobs, customers). The logs contain none,
  and all three roles marked impact as unverified.
- **Real accountability.** Team ownership is a fictional mapping (`data/ownership.csv`).
- **Any link between BGL and OpenStack.** They are different systems and years; the Manager
  compared them only for investigation priority.

## Observations about the agents

- **The roles differed as intended.** One incident and context for the Engineer; complete counts,
  trend, ownership and ordering for the Team lead; cross-dataset priorities with impact marked
  unverified for the Manager.
- **Evidence discipline held:** E-numbered evidence, sample vs. population labels, truncation
  respected, hypotheses separated from facts.
- **The two corrections above** are the kind of slip the evidence cards make visible. Exact counts
  were right; the errors were in summarising truncated or sampled results.
- **Cost:** 0.43 / 0.72 / 1.85 Kiro credits. The Manager's cross-dataset question needed 9 calls.
- **One UI rendering glitch:** in the Engineer's answer the model also wrote pseudo tool-call
  markup (`<function_calls>…`) as plain text next to its real tool call. It is not evidence; the
  real call is the tool card.
