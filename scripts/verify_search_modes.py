"""SELECT-only search benchmark; requires an explicit SERENEDB_DSN (no .env load)."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from statistics import median
from time import perf_counter

from log_insights.config import Settings
from log_insights.db import connect
from log_insights.queries import SQL_DATASET_SPECS, build_search_query_plan

MODES = ("any", "phrase", "exact", "all")


def benchmark_search(
    connection, *, dataset: str, text: str, repetitions: int = 5, limit: int = 20,
    start_time: datetime | None = None, end_time: datetime | None = None,
    component: str | None = None, severity: str | None = None,
) -> dict:
    """Time records plus independent full-scope count; do not time connection setup."""
    if not 1 <= repetitions <= 20:
        raise ValueError("repetitions must be between 1 and 20")
    plans = {mode: build_search_query_plan(
        dataset=dataset, text=text, match_mode=mode, limit=limit, _dataset_specs=SQL_DATASET_SPECS,
        start_time=start_time, end_time=end_time, component=component, severity=severity,
    ) for mode in MODES}
    spec = SQL_DATASET_SPECS[plans["any"].dataset]
    with connection.cursor() as cursor:
        cursor.execute("SELECT version() AS version")
        version = cursor.fetchone()["version"]
        cursor.execute(f"SELECT COUNT(*) AS n FROM {spec.table}")
        corpus_rows = cursor.fetchone()["n"]
        results = {mode: {"samples_ms": []} for mode in MODES}
        # One untimed warm-up per mode, then interleaved measured iterations.
        for iteration in range(repetitions + 1):
            for mode, plan in plans.items():
                start = perf_counter()
                cursor.execute(plan.sql, plan.parameters)
                rows = cursor.fetchall()
                records_end = perf_counter()
                cursor.execute(plan.count_sql, plan.count_parameters)
                total = int(cursor.fetchone()["total_corpus_match_count"])
                end = perf_counter()
                result = results[mode]
                summary = dict(total_corpus_match_count=total,
                               returned_record_count=min(len(rows), limit), truncated=len(rows) > limit)
                if iteration:
                    if result["summary"] != summary:
                        raise AssertionError("Corpus changed during benchmark")
                    result["samples_ms"].append({
                        "records": (records_end - start) * 1000,
                        "total_count": (end - records_end) * 1000,
                        "combined": (end - start) * 1000,
                    })
                result["summary"] = summary
                if mode == "exact" and any(row["message"] != text for row in rows):
                    raise AssertionError("Exact search returned a different raw message")
        for mode, result in results.items():
            plan = plans[mode]
            result["median_ms"] = {key: round(median(sample[key] for sample in result["samples_ms"]), 3)
                                   for key in ("records", "total_count", "combined")}
            result["executed_sql"] = {"records": plan.sql, "total_count": plan.count_sql}
            result["parameters"] = {"records": dict(plan.parameters), "total_count": dict(plan.count_parameters)}
    return dict(version=version, dataset=spec.name, corpus_rows=corpus_rows, query=text,
                repetitions=repetitions, limit=limit,
                filters=dict(start_time=start_time, end_time=end_time, component=component, severity=severity),
                timing_scope="Client wall time: SQL execution + fetching; warm cache, no connection setup or MCP overhead",
                results=results)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", choices=tuple(SQL_DATASET_SPECS), default="bgl")
    parser.add_argument("--query", default="generating core.304")
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--start-time", type=datetime.fromisoformat)
    parser.add_argument("--end-time", type=datetime.fromisoformat)
    parser.add_argument("--component")
    parser.add_argument("--severity")
    args = parser.parse_args()
    settings = Settings.from_env()
    settings.require_serenedb_dsn()
    with connect(settings) as connection:
        report = benchmark_search(connection, dataset=args.dataset, text=args.query,
                                  repetitions=args.repetitions, limit=args.limit,
                                  start_time=args.start_time, end_time=args.end_time,
                                  component=args.component, severity=args.severity)
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
