"""SELECT-only spawn-performance benchmarks and actual MCP response/schema evidence."""

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
from log_insights.queries import SQL_DATASET_SPECS
from log_insights.vm_performance import build_vm_spawn_plan, serialize_vm_spawn_result


def benchmark(connection, request, repetitions):
    plan = build_vm_spawn_plan(SQL_DATASET_SPECS["openstack"], **request)
    samples, previous = [], None
    for iteration in range(repetitions + 1):
        started = perf_counter()
        row = connection.execute(plan.sql, plan.parameters).fetchone()
        elapsed = (perf_counter() - started) * 1000
        result = serialize_vm_spawn_result(row, plan)
        stable = {k: result[k] for k in ("counts", "statistics", "baseline", "detection")}
        if previous is not None and stable != previous:
            raise AssertionError("Population changed during benchmark")
        previous = stable
        if iteration:
            samples.append(elapsed)
    return dict(request=request, samples_ms=samples, median_ms=round(median(samples), 3), response=result)


async def mcp_evidence(settings):
    async with Client(build_server(settings, SQL_DATASET_SPECS)) as client:
        tool = next(t for t in (await client.list_tools()).tools if t.name == "analyze_vm_spawn_performance")
        examples = []
        for request in ({}, dict(slow_threshold_seconds=25.0, top_limit=5, include_anomaly_labels=True),
                        dict(start_time="2017-05-16T00:00:00", end_time="2017-05-17T00:00:00",
                             interval="hour", bucket_limit=2)):
            started = perf_counter()
            response = await client.call_tool(tool.name, request)
            elapsed = (perf_counter() - started) * 1000
            assert not response.is_error
            examples.append(dict(request=request, response=response.structured_content, mcp_wall_ms=elapsed))
        return dict(tool_schema=tool.input_schema, tool_description=tool.description, examples=examples)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 1 <= args.repetitions <= 20:
        parser.error("repetitions must be between 1 and 20")
    settings = Settings.from_env()  # Explicit DSN only; never read .env or initialize data.
    settings.require_serenedb_dsn()
    cases = dict(
        default={}, absolute_25=dict(slow_threshold_seconds=25.0), absolute_30=dict(slow_threshold_seconds=30.0),
        limited=dict(top_limit=1, bucket_limit=1), labelled=dict(include_anomaly_labels=True),
        hourly=dict(interval="hour"), no_buckets=dict(interval=None),
        time_corpus=dict(start_time=datetime(2017,5,14),end_time=datetime(2017,5,15)),
        time_analysis=dict(start_time=datetime(2017,5,14),end_time=datetime(2017,5,15),baseline_scope="analysis"),
    )
    with connect(settings) as connection:
        version = connection.execute("SELECT version() AS version").fetchone()["version"]
        corpus = connection.execute("SELECT COUNT(*) AS n FROM openstack_logs").fetchone()["n"]
        results = {name: benchmark(connection, request, args.repetitions) for name, request in cases.items()}
    report = dict(recorded_at=datetime.now(timezone.utc).isoformat(), version=version, openstack_rows=corpus,
                  repetitions=args.repetitions,
                  timing_scope="Client execution/fetch of the single complete SELECT; one warm-up per case; warm cache; excludes connection setup and MCP overhead",
                  benchmarks=results, **anyio.run(mcp_evidence, settings))
    encoded = json.dumps(report, indent=2, default=lambda v: v.isoformat() if isinstance(v, datetime) else str(v)) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
        print(json.dumps(dict(output=str(args.output), version=version, openstack_rows=corpus,
                             benchmarks={k: dict(median_ms=v["median_ms"], counts=v["response"]["counts"],
                                                 detection=v["response"]["detection"]) for k,v in results.items()}), indent=2))
    else:
        print(encoded, end="")


if __name__ == "__main__":
    main()
