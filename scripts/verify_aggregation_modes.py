"""SELECT-only full-corpus aggregation benchmark and MCP example evidence.

Requires an explicit SERENEDB_DSN; never loads .env, creates objects, or loads data.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
from statistics import median
from time import perf_counter

import anyio
from mcp import Client

from log_insights.config import Settings
from log_insights.db import connect
from log_insights.mcp_server import build_server
from log_insights.queries import SQL_DATASET_SPECS, build_aggregation_query_plan


def benchmark_aggregation(connection, request: dict, repetitions: int) -> dict:
    """Time all statements, including full count and team mapping coverage."""
    plan = build_aggregation_query_plan(**request, _dataset_specs=SQL_DATASET_SPECS)
    statements = {"aggregation": (plan.sql, plan.parameters),
                  "total_count": (plan.count_sql, plan.count_parameters)}
    if plan.coverage_sql:
        statements["mapping_coverage"] = (plan.coverage_sql, plan.coverage_parameters)
    samples = []
    previous = None
    with connection.cursor() as cursor:
        for iteration in range(repetitions + 1):
            timings, rows = {}, {}
            for name, (sql, parameters) in statements.items():
                start = perf_counter()
                cursor.execute(sql, parameters)
                rows[name] = cursor.fetchall()
                timings[name] = (perf_counter() - start) * 1000
            timings["combined"] = sum(timings.values())
            summary = dict(total_matching_record_count=rows["total_count"][0]["total_matching_record_count"],
                           returned_group_count=min(len(rows["aggregation"]), plan.limit),
                           truncated=len(rows["aggregation"]) > plan.limit,
                           groups=rows["aggregation"][:plan.limit])
            if "mapping_coverage" in rows:
                summary["mapping_coverage_counts"] = rows["mapping_coverage"][0]
            if not summary["truncated"]:
                assert sum(g["record_count"] for g in summary["groups"]) == summary["total_matching_record_count"]
            if previous is not None and previous != summary:
                raise AssertionError("Corpus changed during benchmark")
            previous = summary
            if iteration:
                samples.append(timings)
    return dict(request=request, summary=summary, samples_ms=samples,
                median_ms={k: round(median(s[k] for s in samples), 3) for k in samples[0]},
                executed_sql={k: sql for k, (sql, _) in statements.items()},
                parameters={k: dict(parameters) for k, (_, parameters) in statements.items()})


async def mcp_evidence(settings):
    async with Client(build_server(settings, SQL_DATASET_SPECS)) as client:
        tool = next(t for t in (await client.list_tools()).tools if t.name == "summarize_logs")
        examples = []
        for request in (
            dict(dataset="bgl", query="data storage interrupt", match_mode="phrase",
                 group_by="time", interval="month", sort_direction="desc", limit=3),
            dict(dataset="bgl", query="data storage interrupt", match_mode="phrase", group_by="team", limit=100),
        ):
            result = await client.call_tool("summarize_logs", request)
            assert not result.is_error
            examples.append(dict(request=request, response=result.structured_content))
        return dict(tool_schema=tool.input_schema, examples=examples)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--output", type=Path, help="Optional local JSON evidence file")
    args = parser.parse_args()
    if not 1 <= args.repetitions <= 20:
        parser.error("repetitions must be between 1 and 20")
    settings = Settings.from_env()
    settings.require_serenedb_dsn()
    cases = {f"bgl_component_{mode}": dict(dataset="bgl", query="generating core.304", match_mode=mode,
                                           group_by="component", limit=20)
             for mode in ("any", "all", "phrase", "exact")}
    cases.update(
        bgl_phrase_day=dict(dataset="bgl", query="data storage interrupt", match_mode="phrase",
                            group_by="time", interval="day", limit=100),
        bgl_phrase_month_latest=dict(dataset="bgl", query="data storage interrupt", match_mode="phrase",
                                     group_by="time", interval="month", sort_direction="desc", limit=3),
        bgl_phrase_team=dict(dataset="bgl", query="data storage interrupt", match_mode="phrase", group_by="team"),
        bgl_phrase_time_regression=dict(dataset="bgl", query="generating core.304", match_mode="phrase",
                                       group_by="team", component="KERNEL", severity="INFO",
                                       start_time=datetime(2005, 6, 3), end_time=datetime(2005, 7, 3)),
        openstack_phrase_day=dict(dataset="openstack", query="instance", match_mode="phrase",
                                  group_by="time", interval="day", limit=100),
        openstack_phrase_team=dict(dataset="openstack", query="instance", match_mode="phrase", group_by="team"),
    )
    with connect(settings) as connection:
        version = connection.execute("SELECT version() AS version").fetchone()["version"]
        corpus = {dataset: connection.execute(f"SELECT COUNT(*) AS n FROM {spec.table}").fetchone()["n"]
                  for dataset, spec in SQL_DATASET_SPECS.items()}
        benchmarks = {name: benchmark_aggregation(connection, request, args.repetitions)
                      for name, request in cases.items()}
    report = dict(recorded_at=datetime.now(timezone.utc).isoformat(), version=version, corpus_rows=corpus,
                  repetitions=args.repetitions,
                  timing_scope="Client wall time executing/fetching SELECTs; one warm-up per case, warm cache; excludes connection setup and MCP overhead",
                  benchmarks=benchmarks, **anyio.run(mcp_evidence, settings))
    encoded = json.dumps(report, indent=2, default=lambda v: v.isoformat() if isinstance(v, datetime) else str(v)) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
        print(json.dumps({"output": str(args.output), "version": version, "corpus_rows": corpus,
                          "benchmarks": {name: dict(median_ms=r["median_ms"], **r["summary"]) for name, r in benchmarks.items()}},
                         indent=2, default=str))
    else:
        print(encoded, end="")


if __name__ == "__main__":
    main()
