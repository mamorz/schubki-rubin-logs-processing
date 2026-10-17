#!/usr/bin/env python3
"""Measure query times on the full corpus: full-text index vs. scans, and the
aggregations behind the tools. Writes evidence/performance.md.

    .venv/bin/python scripts/benchmark.py
"""
from __future__ import annotations

import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from log_analysis import queries as q  # noqa: E402
from verify_queries import connect  # noqa: E402

RUNS = 5

# (group, label, sql, params). Same question answered with and without the index.
SEARCH = [
    ("term 'parity' (BGL, 4.7M lines)", [
        ("FTS index  @@", "SELECT count(*) FROM bgl_logs_idx WHERE message @@ websearch_to_tsquery(%s)", ("parity",)),
        ("ILIKE scan", "SELECT count(*) FROM bgl_logs WHERE message ILIKE %s", ("%parity%",)),
        ("regex word scan", "SELECT count(*) FROM bgl_logs WHERE regexp_matches(message, %s)", (r"(?i)\bparity\b",)),
    ]),
    ("phrase 'data TLB error interrupt' (BGL)", [
        ("FTS index  @@", "SELECT count(*) FROM bgl_logs_idx WHERE message @@ websearch_to_tsquery(%s)", ('"data TLB error interrupt"',)),
        ("ILIKE scan", "SELECT count(*) FROM bgl_logs WHERE message ILIKE %s", ("%data TLB error interrupt%",)),
    ]),
    ("term + one month (BGL, Sep 2005)", [
        ("FTS index  @@", "SELECT count(*) FROM bgl_logs_idx WHERE message @@ websearch_to_tsquery(%s) "
                          "AND ts >= '2005-09-01' AND ts < '2005-10-01'", ("parity",)),
        ("ILIKE scan", "SELECT count(*) FROM bgl_logs WHERE message ILIKE %s "
                       "AND ts >= '2005-09-01' AND ts < '2005-10-01'", ("%parity%",)),
    ]),
    ("rare term 'InvalidDiskInfo' (OpenStack, 208k lines)", [
        ("FTS index  @@", "SELECT count(*) FROM openstack_logs_idx WHERE message @@ websearch_to_tsquery(%s)", ("InvalidDiskInfo",)),
        ("ILIKE scan", "SELECT count(*) FROM openstack_logs WHERE message ILIKE %s", ("%InvalidDiskInfo%",)),
    ]),
    ("first 20 hits with total count (tool query)", [
        ("FTS index  @@", q.search_logs("bgl", "parity", limit=20), None),
        ("ILIKE scan", "SELECT line_no, ts, node, message, count(*) OVER () FROM bgl_logs "
                       "WHERE message ILIKE %s ORDER BY ts, line_no LIMIT 20", ("%parity%",)),
    ]),
]

# Recall check: the index must find exactly the lines where the term occurs as a
# whole alphanumeric token (case-insensitive), the same rule log_dict tokenizes by.
RECALL_TERMS = [("bgl", "parity"), ("bgl", "interrupt"), ("bgl", "ciod"), ("bgl", "socket"),
                ("bgl", "kernel"), ("bgl", "error"), ("openstack", "InvalidDiskInfo"),
                ("openstack", "libvirt"), ("openstack", "qemu"), ("openstack", "Traceback"),
                ("openstack", "instance"), ("openstack", "error")]

AGGREGATIONS = [
    q.search_logs("bgl", "error", limit=20),
    q.search_logs("bgl", "error", level="FATAL", limit=20),
    q.corpus_overview(),
    q.count_by("bgl", "team", alerts_only=True),
    q.count_by("bgl", "node", alerts_only=True),
    q.trend("bgl", "week", split_by="team", alerts_only=True),
    q.recurring_patterns("bgl", alerts_only=True),
    q.recurring_patterns("bgl"),
    q.team_priorities("bgl"),
    q.latency_summary("endpoint"),
    q.anomaly_comparison(),
    q.nearby_events("bgl", 3313795, before=10, after=10),
    q.nearby_events("bgl", 3313795, same_node=True, window_seconds=3600),
]


def timed(conn, sql, params) -> tuple[float, object]:
    """Median time of a statement, or of a whole Query via run_query (rows + count)."""
    if isinstance(sql, q.Query):
        query = sql
        q.run_query(conn, query)  # warm-up
        times, result = [], None
        for _ in range(RUNS):
            start = time.perf_counter()
            res = q.run_query(conn, query)
            times.append(1000 * (time.perf_counter() - start))
            result = f"{res['row_count']} rows of {res['total_available']}"
        return statistics.median(times), result
    conn.execute(sql, params).fetchall()  # warm-up
    times, result = [], None
    for _ in range(RUNS):
        start = time.perf_counter()
        rows = conn.execute(sql, params).fetchall()
        times.append(1000 * (time.perf_counter() - start))
        result = rows[0][0] if len(rows) == 1 and len(rows[0]) == 1 else f"{len(rows)} rows"
    return statistics.median(times), result


def main() -> int:
    out = ["# Performance on the full corpus\n",
           f"Median of {RUNS} warm runs per query, measured by `scripts/benchmark.py` through psycopg "
           "(includes client round-trip). BGL 4,747,963 lines, OpenStack 207,820 lines.\n",
           "## Full-text index vs. table scan\n",
           "| Question | Method | Median ms | Result | Speed-up |", "|---|---|---|---|---|"]
    with connect() as conn:
        out.insert(2, f"Server: `{conn.execute('SELECT version()').fetchone()[0]}`\n")
        for question, variants in SEARCH:
            measured = [(label, *timed(conn, sql, params)) for label, sql, params in variants]
            base = measured[0][1]
            for label, ms, result in measured:
                speed = "baseline" if label.startswith("FTS") else f"{ms / base:.0f}x slower"
                out.append(f"| {question} | {label} | {ms:.1f} | {result} | {speed} |")
                print(f"{question:48} {label:16} {ms:8.1f} ms  {result}")
        out += ["\nILIKE also matches inside longer tokens (e.g. `instances` for `instance`), so its counts "
                "are higher than the word-level full-text match.\n",
                "## Recall: full-text index vs. exact whole-token scan\n",
                "Ground truth is a regex scan for the term bounded by non-alphanumeric characters, "
                "which is the rule `log_dict` (`split_by_non_alpha`) tokenizes by.\n",
                "| Dataset | Term | FTS index | Whole-token regex | ILIKE substring | Match |", "|---|---|---|---|---|---|"]
        for ds, term in RECALL_TERMS:
            table = q.TABLE[ds]
            fts = conn.execute(f"SELECT count(*) FROM {q.INDEX[ds]} WHERE message @@ websearch_to_tsquery(%s)",
                               (term,)).fetchone()[0]
            exact = conn.execute(f"SELECT count(*) FROM {table} WHERE regexp_matches(message, %s)",
                                 (rf"(?i)(^|[^0-9a-z]){term}([^0-9a-z]|$)",)).fetchone()[0]
            ilike = conn.execute(f"SELECT count(*) FROM {table} WHERE message ILIKE %s",
                                 (f"%{term}%",)).fetchone()[0]
            out.append(f"| {ds} | {term} | {fts} | {exact} | {ilike} | {'yes' if fts == exact else 'NO'} |")
            print(f"recall {ds:10} {term:16} fts={fts} exact={exact} ilike={ilike}")
        out += ["", "## Tool queries (aggregations and context)\n",
                "| Query | Scope | Median ms | Result |", "|---|---|---|---|"]
        for query in AGGREGATIONS:
            ms, result = timed(conn, query, None)
            out.append(f"| {query.name} | {query.scope} | {ms:.1f} | {result} |")
            print(f"{query.name:28} {query.scope[:60]:60} {ms:8.1f} ms  {result}")
    (ROOT / "evidence").mkdir(exist_ok=True)
    (ROOT / "evidence" / "performance.md").write_text("\n".join(out) + "\n")
    print("-> evidence/performance.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
