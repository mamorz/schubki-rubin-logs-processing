#!/usr/bin/env python3
"""Run the OpenTelemetry (huge dataset) queries and write evidence/huge_datasets.md
with the executed SQL, data scope, time and bytes actually downloaded from S3.

    .venv/bin/python scripts/load_otel.py            # once: catalog + local index
    .venv/bin/python scripts/verify_otel.py

Bytes are measured from the SereneDB container's network counter (docker exec);
without Docker access the column shows "n/a". Each remote case uses a different
time window, so numbers are cold reads, not cache hits.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from log_analysis import otel_queries as oq  # noqa: E402
from log_analysis import queries as q  # noqa: E402
from verify_queries import connect, jsonable, md_table  # noqa: E402

CONTAINER = "schubki-rubin-logs-processing-serenedb-1"


def rx_bytes() -> int | None:
    try:
        out = subprocess.run(["docker", "exec", CONTAINER, "cat", "/proc/net/dev"],
                             capture_output=True, text=True, timeout=10).stdout
        return sum(int(l.split(":")[1].split()[0]) for l in out.splitlines()[2:]
                   if ":" in l and not l.strip().startswith("lo"))
    except Exception:
        return None


def cases(conn):
    """(name, role, question, builder). Builders run lazily: plan_window reads the catalog."""
    return [
        ("overview", "all", "How much OpenTelemetry data is reachable, and how much is indexed locally?",
         lambda: oq.otel_overview()),
        ("volume_whole_day", "manager", "How many log rows per hour does the whole day contain (50 files)?",
         lambda: oq.otel_volume_trend(bucket="hour")),
        ("error_rates_5min", "manager", "Which services have the highest error share, 04:00-04:05?",
         lambda: oq.otel_error_rates(conn, "2025-09-23 04:00:00+00", "2025-09-23 04:05:00+00", limit=10)),
        ("count_errors_by_service_8min", "team lead", "Where do error rows come from, 07:00-07:08?",
         lambda: oq.otel_count_by(conn, "service", "2025-09-23 07:00:00+00", "2025-09-23 07:08:00+00",
                                  severity="error", limit=10)),
        ("window_guard", "all", "Does a too-large remote window get refused before reading anything?",
         lambda: oq.otel_count_by(conn, "service", "2025-09-23 14:00:00+00", "2025-09-23 15:00:00+00")),
        ("trend_payment_errors_per_minute", "team lead", "How do payment errors develop per minute, 22:00-22:10?",
         lambda: oq.otel_trend(conn, "2025-09-23 22:00:00+00", "2025-09-23 22:10:00+00", service="payment")),
        ("trend_across_files", "team lead", "Error trend across the boundary of two files, 11:58-12:01",
         lambda: oq.otel_trend(conn, "2025-09-23 11:58:00+00", "2025-09-23 12:01:00+00")),
        ("top_error_messages_30s", "engineer", "Which error messages dominate, 23:00:00-23:00:30?",
         lambda: oq.otel_top_messages(conn, "2025-09-23 23:00:00+00", "2025-09-23 23:00:30+00", limit=10)),
        ("index_search_charge_card", "engineer", "Find 'failed to charge card' errors in the indexed slice",
         lambda: oq.otel_search('"failed to charge card"', severity="error", limit=5)),
        ("index_services_for_timeout", "engineer", "Which services log 'timeout' in the indexed slice?",
         lambda: oq.otel_index_count_by("service", text="timeout")),
        ("index_service_severity", "team lead", "Service/severity mix of the indexed slice (from the index only)",
         lambda: oq.otel_index_count_by("service_severity", limit=15)),
        ("context_first_error", "engineer", "What happened just before/after the first indexed charge-card error?",
         lambda: oq.otel_context(oq.INDEX_PART, 1, before=3, after=6)),
        ("trace_first_error", "engineer", "Every log row of that error's trace",
         lambda: oq.otel_trace(oq.INDEX_PART, "0ce994d55ca67ef4890f49805d0f379a", 1, window_rows=300_000)),
    ]


def index_vs_scan(conn) -> list[str]:
    """Same question with the local index and with a remote scan of the same rows."""
    s = conn.execute("SELECT end_row, log_rows, row_groups, build_seconds FROM otel_index_slice").fetchone()
    end_row, log_rows, groups, build = s
    phrase = "failed to charge card"
    idx = q.run_query(conn, oq.otel_search(f'"{phrase}"', limit=1))
    rx0 = rx_bytes()
    scan = q.run_query(conn, q.Query(
        "remote_scan", f"SELECT count(*) FILTER (WHERE Body ILIKE %(p)s) AS total_matches "
                       f"FROM read_parquet('{oq.part_url(oq.INDEX_PART)}', file_row_number = true) "
                       f"WHERE file_row_number < %(end)s", {"p": f"%{phrase}%", "end": end_row}))
    rx1 = rx_bytes()
    mb = f"{(rx1 - rx0) / 1e6:,.0f}" if rx0 is not None and rx1 is not None else "n/a"
    idx_s, scan_s = idx["elapsed_ms"] / 1000, scan["elapsed_ms"] / 1000
    breakeven = build / max(scan_s - idx_s, 1e-9)
    return [
        "## Index vs. remote scan (same rows, same question)\n",
        f"Question: how many rows of the indexed slice ({log_rows:,} rows, {groups} row groups) contain "
        f"\"{phrase}\"?\n",
        "| Method | Time | Downloaded | Matches |", "|---|---|---|---|",
        f"| Local inverted index (`@@` phrase) | {idx_s:.2f} s | 0 MB | {idx['total_available']:,} |",
        f"| Remote scan of Body (`ILIKE`) | {scan_s:.1f} s | {mb} MB | {scan['total_available']:,} |",
        f"| One-off index build (`load_otel.py`) | {build:.1f} s | Body of {groups} row groups | — |",
        f"\nThe index pays for itself after about **{breakeven:.1f}** repeated questions on this slice "
        f"(build time / time saved per question). Counts can differ slightly: `ILIKE` matches substrings, "
        f"the index matches whole tokens in order.\n",
    ]


def main() -> int:
    out_dir = ROOT / "evidence"
    out_dir.mkdir(exist_ok=True)
    results, failures = [], 0
    with connect() as conn:
        version = conn.execute("SELECT version()").fetchone()[0]
        for name, role, question, build in cases(conn):
            rx0 = rx_bytes()
            try:
                res = q.run_query(conn, build())
            except ValueError as e:
                if name != "window_guard":
                    raise
                # Expected: the guard refuses the window before any S3 read.
                res = {"query": "otel_count_by", "scope": "otel remote: 14:00-15:00 (refused)", "sql": "-",
                       "params": {}, "row_count": 0, "total_available": None, "truncated": False,
                       "elapsed_ms": 0.0, "rows": [{"refused": str(e)}]}
            except Exception as e:
                failures += 1
                print(f"FAIL {name}: {type(e).__name__}: {str(e).splitlines()[0][:200]}", flush=True)
                continue
            rx1 = rx_bytes()
            res.update(case=name, role=role, question=question,
                       downloaded_mb=round((rx1 - rx0) / 1e6, 1) if rx0 is not None and rx1 is not None else None)
            results.append(res)
            print(f"ok   {name:34} {res['elapsed_ms'] / 1000:8.2f} s  {res['downloaded_mb']!s:>8} MB  "
                  f"rows={res['row_count']} total={res['total_available']}", flush=True)
        comparison = index_vs_scan(conn)
        overview = conn.execute("SELECT count(DISTINCT part), sum(num_rows)::BIGINT, sum(total_bytes)::DOUBLE "
                                "FROM otel_row_groups").fetchone()

    (out_dir / "huge_datasets.json").write_text(json.dumps(
        {"server": version, "cases": results}, default=jsonable, indent=2, ensure_ascii=False))
    lines = [
        "# Huge dataset evidence: OpenTelemetry logs on S3\n",
        f"Generated by `scripts/verify_otel.py` against `{version}`. Source: 50 public Parquet files "
        f"(`text_bench/part_000..049.parquet`), queried in place over HTTPS; nothing is downloaded up front.\n",
        f"- **Reachable:** {overview[0]} files, **{overview[1]:,} log rows**, {overview[2] / 1e9:,.0f} GB compressed "
        f"(one day, 2025-09-23).",
        "- **Remote queries** read only the row groups of the requested window and only the columns they use; "
        "the *Downloaded* column is the measured network traffic of the database container.",
        "- **Indexed locally:** see `overview`; full-text search and counts on that slice never touch S3.\n",
        "| Case | Role | Data scope | Time | Downloaded | Rows | Total |", "|---|---|---|---|---|---|---|",
    ]
    lines += [f"| [{r['case']}](#{r['case'].replace('_', '-')}) | {r['role']} | {r['scope'][:110]}"
              f"{'…' if len(r['scope']) > 110 else ''} | {r['elapsed_ms'] / 1000:.2f} s | "
              f"{r['downloaded_mb'] if r['downloaded_mb'] is not None else 'n/a'} MB | {r['row_count']} | "
              f"{r['total_available'] if r['total_available'] is not None else '-'} |" for r in results]
    lines += [""] + comparison
    for r in results:
        lines += [f"\n## {r['case']}\n", f"**Role:** {r['role']}  ", f"**Question:** {r['question']}  ",
                  f"**Scope:** {r['scope']}  ",
                  f"**Result:** {r['row_count']} rows"
                  + (f" of {r['total_available']}" if r['total_available'] is not None else "")
                  + (" (truncated)" if r["truncated"] else "")
                  + f", {r['elapsed_ms'] / 1000:.2f} s, downloaded {r['downloaded_mb']} MB\n",
                  "```sql", r["sql"], "```", f"Parameters: `{json.dumps(r['params'], default=jsonable)}`\n",
                  md_table(r["rows"])]
    (out_dir / "huge_datasets.md").write_text("\n".join(lines))
    print(f"\n{len(results)} cases ok, {failures} failed -> evidence/huge_datasets.md")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
