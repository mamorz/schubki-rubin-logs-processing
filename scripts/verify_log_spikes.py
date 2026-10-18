"""SELECT-only full BGL spike-query benchmarks and actual MCP schema/responses."""

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
from log_insights.spikes import build_spike_plan, serialize_spikes


def benchmark(connection, request, repetitions):
    plan = build_spike_plan(SQL_DATASET_SPECS["bgl"], **request)
    samples, previous = [], None
    for iteration in range(repetitions + 1):
        started = perf_counter()
        row = connection.execute(plan.sql, plan.parameters).fetchone()
        elapsed = (perf_counter()-started)*1000
        response = serialize_spikes(row, plan)
        stable = {k:response[k] for k in ("counts","baselines","findings","coverage")}
        if previous is not None and stable != previous:
            raise AssertionError("Spike population or deterministic output changed during benchmark")
        previous = stable
        if iteration:
            samples.append(round(elapsed,3))
    return dict(request=request,samples_ms=samples,median_ms=round(median(samples),3),response=response)


async def mcp_evidence(settings):
    async with Client(build_server(settings,SQL_DATASET_SPECS)) as client:
        tool = next(t for t in (await client.list_tools()).tools if t.name == "detect_log_spikes")
        examples = []
        for request in ({}, dict(component="MMCS",start_time="2005-09-20T12:00:00",end_time="2005-09-20T13:00:00"),
                        dict(interval="day",max_findings=1,patterns_per_finding=1)):
            started = perf_counter()
            result = await client.call_tool(tool.name,request)
            elapsed = (perf_counter()-started)*1000
            assert not result.is_error
            examples.append(dict(request=request,response=result.structured_content,mcp_wall_ms=round(elapsed,3)))
        return dict(tool_schema=tool.input_schema,tool_description=tool.description,examples=examples)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repetitions",type=int,default=5)
    parser.add_argument("--output",type=Path)
    args = parser.parse_args()
    if not 1 <= args.repetitions <= 20:
        parser.error("repetitions must be between 1 and 20")
    settings = Settings.from_env()
    settings.require_serenedb_dsn()
    cases = dict(default={},compact=dict(max_findings=1,patterns_per_finding=1),
        largest=dict(max_findings=20,patterns_per_finding=5,evidence_per_pattern=3),
        daily=dict(interval="day"),total_volume=dict(signal="total_volume"),mmcs=dict(component="MMCS"),
        mmcs_window=dict(component="MMCS",start_time=datetime(2005,9,20,12),end_time=datetime(2005,9,20,13)),
        reference=dict(component="MMCS",baseline_scope="reference",baseline_start_time=datetime(2005,6,1),
            baseline_end_time=datetime(2005,9,1),start_time=datetime(2005,9,20,12),
            end_time=datetime(2005,9,20,13),min_baseline_buckets=10))
    with connect(settings) as connection:
        version = connection.execute("SELECT version() AS version").fetchone()["version"]
        results = {name:benchmark(connection,request,args.repetitions) for name,request in cases.items()}
    default = results["default"]["response"]
    assert default["counts"]["corpus_record_count"] == 4747963
    assert default["counts"]["elevated_component_bucket_count"] == 24
    assert default["counts"]["spike_window_count"] == 16
    for result in results.values():
        for finding in result["response"]["findings"]:
            assert sum(p["record_count"] for p in finding["contributors"])+finding["omitted_pattern_record_count"] == finding["selected_record_count"]
    report = dict(recorded_at=datetime.now(timezone.utc).isoformat(),version=version,repetitions=args.repetitions,
        timing_scope="Warm-cache execution/fetch of complete SELECT including contributing patterns and evidence; one warm-up per case; excludes connection setup, Python formatting and MCP overhead",
        benchmarks=results,**anyio.run(mcp_evidence,settings))
    encoded = json.dumps(report,indent=2,default=lambda v:v.isoformat() if isinstance(v,datetime) else str(v))+"\n"
    if args.output:
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(encoded,encoding="utf-8")
        print(json.dumps(dict(output=str(args.output),version=version,
            benchmarks={k:dict(median_ms=v["median_ms"],counts=v["response"]["counts"]) for k,v in results.items()}),indent=2))
    else:
        print(encoded,end="")


if __name__ == "__main__":
    main()
