#!/usr/bin/env python3
"""Run every query in log_analysis/queries.py against the loaded corpus and write
the executed SQL, parameters, timing and results to evidence/.

    .venv/bin/python scripts/verify_queries.py            # all cases
    .venv/bin/python scripts/verify_queries.py search     # cases whose name contains "search"

Connection: SERENEDB_HOST (127.0.0.1), SERENEDB_PORT (7890), SERENEDB_USER (postgres),
SERENEDB_PASSWORD (optional, the local Docker setup uses trust auth).
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from log_analysis import queries as q  # noqa: E402

# (case name, role, question it answers, Query)
CASES = [
    ("corpus_overview", "all", "How big is the base corpus and how many problem lines does each dataset have?",
     q.corpus_overview()),

    # Engineer
    ("search_bgl_parity", "engineer", "Where do cache parity errors occur in September 2005?",
     q.search_logs("bgl", "parity", "2005-09-01", "2005-10-01", limit=5)),
    ("search_bgl_phrase_alerts", "engineer", "Which alert lines report a data TLB error interrupt?",
     q.search_logs("bgl", '"data TLB error interrupt"', alerts_only=True, limit=5)),
    ("search_openstack_disk_error", "engineer", "Which nova-compute errors mention an invalid disk?",
     q.search_logs("openstack", "InvalidDiskInfo", level="ERROR", limit=10)),
    ("nearby_openstack_disk_error", "engineer", "What happened just before and after the first InvalidDiskInfo error?",
     None),  # anchored on the first hit of the previous case, built at run time
    ("nearby_bgl_same_node", "engineer", "What else happened on the node of the first September parity alert?",
     None),
    ("instance_timeline_anomalous", "engineer", "What is the full log history of anomalous instance 544fd51c-...?",
     q.instance_timeline("544fd51c-4edc-4780-baae-ba1d80a0acfc", limit=30)),

    # Team lead
    ("count_bgl_alerts_by_team", "team lead", "Which teams own the BGL alert lines (whole dataset)?",
     q.count_by("bgl", "team", alerts_only=True)),
    ("count_bgl_alerts_by_label", "team lead", "Which BGL alert categories recur most?",
     q.count_by("bgl", "alert_label", alerts_only=True, limit=15)),
    ("count_openstack_by_team_level", "team lead", "How are OpenStack WARNING lines spread across teams?",
     q.count_by("openstack", "team", level="WARNING")),
    ("trend_bgl_alerts_weekly_by_team", "team lead", "How do BGL alert lines develop week by week, per team?",
     q.trend("bgl", "week", split_by="team", alerts_only=True)),
    ("trend_bgl_kerndtlb_daily", "team lead", "On which days did KERNDTLB alerts occur?",
     q.trend("bgl", "day", alert_label="KERNDTLB", limit=60)),
    ("patterns_bgl_alerts", "team lead", "Which BGL alert message patterns recur most?",
     q.recurring_patterns("bgl", alerts_only=True, limit=10)),
    ("patterns_openstack_problems_warning", "team lead", "Which OpenStack WARNING patterns recur?",
     q.recurring_patterns("openstack", level="WARNING", limit=10)),
    ("patterns_openstack_problems_error", "team lead", "Which OpenStack ERROR patterns recur?",
     q.recurring_patterns("openstack", level="ERROR", limit=10)),

    # Manager
    ("priorities_bgl", "manager", "Which BGL teams have the most problem lines, and is it getting better or worse?",
     q.team_priorities("bgl")),
    ("priorities_openstack", "manager", "Which OpenStack teams have the most problem lines?",
     q.team_priorities("openstack", recent_days=1)),

    # OpenStack analytics
    ("latency_by_endpoint", "team lead", "Which OpenStack API endpoints are slowest (p95/p99)?",
     q.latency_summary("endpoint", limit=15)),
    ("latency_by_status", "team lead", "Does latency differ by HTTP status?",
     q.latency_summary("status")),
    ("latency_trend_hourly_create", "team lead", "How does server-create latency evolve per hour?",
     q.latency_trend("hour", endpoint="POST /v2/{tenant}/servers", limit=48)),
    ("error_rates_by_endpoint", "team lead", "Which endpoints return 4xx/5xx most often?",
     q.error_rates("endpoint", limit=10)),
    ("error_rates_by_component", "team lead", "Which OpenStack components log the most warnings/errors?",
     q.error_rates("component")),
    ("anomaly_lifecycle_comparison", "manager", "Do the labelled anomalous instances behave differently from normal ones?",
     q.anomaly_comparison()),
    ("slowest_spawns", "engineer", "Which instances took longest to spawn, and are they the labelled anomalies?",
     q.slowest_instances(limit=10)),
]


def connect():
    return psycopg.connect(
        host=os.getenv("SERENEDB_HOST", "127.0.0.1"),
        port=int(os.getenv("SERENEDB_PORT", "7890")),
        user=os.getenv("SERENEDB_USER", "postgres"),
        password=os.getenv("SERENEDB_PASSWORD") or None,
        dbname=os.getenv("SERENEDB_DATABASE", "postgres"),
        autocommit=True,
    )


def jsonable(v):
    if isinstance(v, (datetime, date)):
        return v.isoformat(sep=" ") if isinstance(v, datetime) else v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    return v


def md_table(rows: list[dict], max_rows: int = 15, max_cell: int = 90) -> str:
    if not rows:
        return "_no rows_\n"
    cols = list(rows[0])
    def cell(v):
        s = str(jsonable(v)) if v is not None else ""
        s = s.replace("|", "\\|").replace("\n", " ")
        return s if len(s) <= max_cell else s[: max_cell - 1] + "…"
    out = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    out += ["| " + " | ".join(cell(r[c]) for c in cols) + " |" for r in rows[:max_rows]]
    if len(rows) > max_rows:
        out.append(f"\n_{len(rows) - max_rows} more rows in results.json_")
    return "\n".join(out) + "\n"


def explain_uses_index(conn, query: q.Query) -> bool:
    plan = "\n".join(r[-1] for r in conn.execute("EXPLAIN " + query.sql, query.params).fetchall())
    return "IRESEARCH_SCAN" in plan


def main(argv: list[str]) -> int:
    selected = [c for c in CASES if not argv[1:] or any(a in c[0] for a in argv[1:])]
    out_dir = ROOT / "evidence"
    out_dir.mkdir(exist_ok=True)
    results, failures = [], 0
    previous: dict[str, dict] = {}
    with connect() as conn:
        version = conn.execute("SELECT version()").fetchone()[0]
        for name, role, question, query in selected:
            if name == "nearby_openstack_disk_error":
                hit = previous.get("search_openstack_disk_error", {}).get("rows", [{}])[0]
                query = q.nearby_events("openstack", hit["line_no"], source_file=hit["source_file"],
                                        before=8, after=8) if hit else None
            elif name == "nearby_bgl_same_node":
                hit = previous.get("search_bgl_parity", {}).get("rows", [{}])[0]
                query = q.nearby_events("bgl", hit["line_no"], same_node=True, before=5, after=5,
                                        window_seconds=3600) if hit else None
            if query is None:
                print(f"SKIP {name}: prerequisite case not run")
                continue
            try:
                res = q.run_query(conn, query)
                res["uses_fulltext_index"] = explain_uses_index(conn, query)
            except Exception as e:  # report and keep going
                failures += 1
                print(f"FAIL {name}: {type(e).__name__}: {str(e).splitlines()[0]}")
                continue
            res.update(case=name, role=role, question=question)
            previous[name] = res
            results.append(res)
            flag = " TRUNCATED" if res["truncated"] else ""
            print(f"ok   {name:38} {res['elapsed_ms']:8.1f} ms  rows={res['row_count']}"
                  f" total={res['total_available']}{flag}")

    (out_dir / "results.json").write_text(json.dumps(
        {"server": version, "cases": results}, default=jsonable, indent=2, ensure_ascii=False))

    lines = [f"# Query evidence\n",
             f"Generated by `scripts/verify_queries.py` against `{version}` on the full base corpus "
             f"(BGL 4,747,963 lines, OpenStack 207,820 lines). Counts are log lines, not incidents.\n",
             "| Case | Role | Rows | Total | Truncated | Time (ms) | FTS index |", "|---|---|---|---|---|---|---|"]
    lines += [f"| [{r['case']}](#{r['case'].replace('_', '-')}) | {r['role']} | {r['row_count']} | "
              f"{r['total_available'] if r['total_available'] is not None else '-'} | "
              f"{'yes' if r['truncated'] else 'no'} | {r['elapsed_ms']} | {'yes' if r['uses_fulltext_index'] else '-'} |"
              for r in results]
    for r in results:
        lines += [f"\n## {r['case']}\n", f"**Role:** {r['role']}  ", f"**Question:** {r['question']}  ",
                  f"**Scope:** {r['scope']}  ",
                  f"**Result:** {r['row_count']} rows"
                  + (f" of {r['total_available']} available" if r['total_available'] is not None else "")
                  + (" (truncated)" if r["truncated"] else "") + f", {r['elapsed_ms']} ms\n",
                  "```sql", r["sql"], "```", f"Parameters: `{json.dumps(r['params'], default=jsonable)}`\n",
                  md_table(r["rows"])]
    (out_dir / "queries_report.md").write_text("\n".join(lines))
    print(f"\n{len(results)} cases ok, {failures} failed -> evidence/queries_report.md, evidence/results.json")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
