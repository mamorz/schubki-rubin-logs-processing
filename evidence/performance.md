# Performance on the full corpus

Median of 5 warm runs per query, measured by `scripts/benchmark.py` through psycopg (includes client round-trip). BGL 4,747,963 lines, OpenStack 207,820 lines.

Server: `PostgreSQL 18.3 (SereneDB 26.09.1.3)`

## Full-text index vs. table scan

| Question | Method | Median ms | Result | Speed-up |
|---|---|---|---|---|
| term 'parity' (BGL, 4.7M lines) | FTS index  @@ | 1.7 | 121498 | baseline |
| term 'parity' (BGL, 4.7M lines) | ILIKE scan | 45.1 | 121512 | 27x slower |
| term 'parity' (BGL, 4.7M lines) | regex word scan | 51.4 | 121498 | 31x slower |
| phrase 'data TLB error interrupt' (BGL) | FTS index  @@ | 4.8 | 152734 | baseline |
| phrase 'data TLB error interrupt' (BGL) | ILIKE scan | 45.5 | 152734 | 9x slower |
| term + one month (BGL, Sep 2005) | FTS index  @@ | 1.8 | 485 | baseline |
| term + one month (BGL, Sep 2005) | ILIKE scan | 6.2 | 487 | 3x slower |
| rare term 'InvalidDiskInfo' (OpenStack, 208k lines) | FTS index  @@ | 1.7 | 16 | baseline |
| rare term 'InvalidDiskInfo' (OpenStack, 208k lines) | ILIKE scan | 27.8 | 16 | 17x slower |
| first 20 hits with total count (tool query) | FTS index  @@ | 4.7 | 20 rows of 121498 | baseline |
| first 20 hits with total count (tool query) | ILIKE scan | 156.0 | 20 rows | 33x slower |

ILIKE also matches inside longer tokens (e.g. `instances` for `instance`), so its counts are higher than the word-level full-text match.

## Recall: full-text index vs. exact whole-token scan

Ground truth is a regex scan for the term bounded by non-alphanumeric characters, which is the rule `log_dict` (`split_by_non_alpha`) tokenizes by.

| Dataset | Term | FTS index | Whole-token regex | ILIKE substring | Match |
|---|---|---|---|---|---|
| bgl | parity | 121498 | 121498 | 121512 | yes |
| bgl | interrupt | 481945 | 481945 | 489941 | yes |
| bgl | ciod | 350567 | 350567 | 350721 | yes |
| bgl | socket | 60333 | 60333 | 60674 | yes |
| bgl | kernel | 50535 | 50535 | 51576 | yes |
| bgl | error | 558042 | 558042 | 592259 | yes |
| openstack | InvalidDiskInfo | 16 | 16 | 16 | yes |
| openstack | libvirt | 40 | 40 | 40 | yes |
| openstack | qemu | 40 | 40 | 40 | yes |
| openstack | Traceback | 8 | 8 | 8 | yes |
| openstack | instance | 66137 | 66137 | 102006 | yes |
| openstack | error | 16 | 16 | 16 | yes |

## Tool queries (aggregations and context)

| Query | Scope | Median ms | Result |
|---|---|---|---|
| search_logs | bgl: full-text 'error' | 5.1 | 20 rows of 558042 |
| search_logs | bgl: full-text 'error', level = 'FATAL' | 5.9 | 20 rows of 345127 |
| corpus_overview | whole base corpus, no filters | 31.6 | 2 rows of None |
| count_by | bgl: alerts only | 4.4 | 4 rows of 4 |
| count_by | bgl: alerts only | 11.3 | 50 rows of 31375 |
| trend | bgl: alerts only, per week | 4.6 | 85 rows of None |
| recurring_patterns | bgl: alerts only | 66.5 | 20 rows of 2259 |
| recurring_patterns | bgl: whole dataset | 422.7 | 20 rows of 9863 |
| team_priorities | bgl: whole dataset, problem = alert lines, recent window 7d | 59.7 | 4 rows of None |
| latency_summary | openstack: HTTP access lines, slow > 1.0s | 10.5 | 30 rows of 33 |
| anomaly_comparison | openstack: nova.compute.manager 'Took N seconds' lines, anomalous = anomaly_labels.txt | 14.2 | 8 rows of None |
| nearby_events | bgl: lines 3313785..3313805 | 3.0 | 21 rows of None |
| nearby_events | bgl: node of line 3313795, +/- 3600s, 10 before / 10 after | 5.0 | 19 rows of None |
