"""SELECT-only OpenStack scenario reproduction, MCP evidence and warm benchmarks.

Requires an explicit SERENEDB_DSN; never loads .env, creates objects or loads data.
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
from log_insights.queries import SQL_DATASET_SPECS, build_search_query_plan

REQUEST_ID = "req-d6143bd4-9784-4e95-a0c8-83fba00530b6"
INSTANCE_ID = "544fd51c-4edc-4780-baae-ba1d80a0acfc"


def run_research_queries(connection):
    """Execute the six checked-in research SELECTs without modifying the DB."""
    source = Path(__file__).resolve().parents[1] / "sql/examples/openstack_tracing.sql"
    sql = "\n".join(line for line in source.read_text().splitlines() if not line.lstrip().startswith("--"))
    statements = [s.strip() for s in sql.split(";") if s.strip()]
    if len(statements) != 6 or any(not s.startswith(("SELECT ", "WITH ")) for s in statements):
        raise ValueError("Expected the six code-owned SELECT-only research queries")
    return {f"Q{number}": connection.execute(statement).fetchall() for number, statement in enumerate(statements, 1)}


def benchmark(connection, request, repetitions):
    plan = build_search_query_plan(**request, _dataset_specs=SQL_DATASET_SPECS)
    samples, previous = [], None
    for iteration in range(repetitions + 1):
        start = perf_counter()
        rows = connection.execute(plan.sql, plan.parameters).fetchall()
        records_end = perf_counter()
        total = connection.execute(plan.count_sql, plan.count_parameters).fetchone()["total_corpus_match_count"]
        end = perf_counter()
        summary = dict(total_corpus_match_count=total, returned_record_count=min(len(rows), plan.limit), truncated=len(rows) > plan.limit)
        if previous is not None and summary != previous:
            raise AssertionError("Corpus changed during benchmark")
        previous = summary
        if iteration:
            samples.append(dict(records=(records_end-start)*1000, total_count=(end-records_end)*1000, combined=(end-start)*1000))
    return dict(request=request, summary=summary, samples_ms=samples,
                median_ms={key: round(median(s[key] for s in samples), 3) for key in samples[0]},
                executed_sql=dict(records=plan.sql, total_count=plan.count_sql),
                parameters=dict(records=dict(plan.parameters), total_count=dict(plan.count_parameters)))


async def mcp_evidence(settings):
    async with Client(build_server(settings, SQL_DATASET_SPECS)) as client:
        tool = next(t for t in (await client.list_tools()).tools if t.name == "search_logs")
        examples = []
        for request in (
            dict(dataset="openstack", request_id=REQUEST_ID, limit=50),
            dict(dataset="openstack", instance_id=INSTANCE_ID, limit=2),
            dict(dataset="openstack", instance_id=INSTANCE_ID, query="41.81 seconds to spawn", match_mode="phrase", limit=50),
            dict(dataset="openstack", query=INSTANCE_ID, match_mode="phrase", limit=50),
        ):
            response = await client.call_tool("search_logs", request)
            assert not response.is_error
            examples.append(dict(request=request, response=response.structured_content))
        return dict(tool_schema=tool.input_schema, examples=examples)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 1 <= args.repetitions <= 20:
        parser.error("repetitions must be between 1 and 20")
    settings = Settings.from_env()
    settings.require_serenedb_dsn()
    cases = dict(
        request=dict(dataset="openstack", request_id=REQUEST_ID, limit=50),
        instance=dict(dataset="openstack", instance_id=INSTANCE_ID, limit=50),
        combined=dict(dataset="openstack", request_id=REQUEST_ID, instance_id=INSTANCE_ID, limit=50),
        instance_phrase=dict(dataset="openstack", instance_id=INSTANCE_ID, text="41.81 seconds to spawn", match_mode="phrase", limit=50),
        instance_time=dict(dataset="openstack", instance_id=INSTANCE_ID, start_time=datetime(2017, 5, 14, 21, 7), end_time=datetime(2017, 5, 14, 21, 9), limit=50),
        uuid_phrase=dict(dataset="openstack", text=INSTANCE_ID, match_mode="phrase", limit=50),
    )
    with connect(settings) as connection:
        version = connection.execute("SELECT version() AS version").fetchone()["version"]
        corpus = connection.execute("SELECT COUNT(*) AS n FROM openstack_logs").fetchone()["n"]
        backgrounds = connection.execute(
            "SELECT request_id, COUNT(*) AS n, COUNT(DISTINCT instance_id) AS distinct_vms FROM openstack_logs "
            "WHERE request_id LIKE %s OR request_id LIKE %s GROUP BY request_id ORDER BY n DESC",
            ("req-addc1839-%", "req-3ea4052c-%"),
        ).fetchall()
        for index, row in enumerate(backgrounds, 1):
            cases[f"background_{index}"] = dict(dataset="openstack", request_id=row["request_id"], limit=2)
        timings = {name: benchmark(connection, request, args.repetitions) for name, request in cases.items()}
        research = run_research_queries(connection)
        labels = connection.execute(
            "SELECT a.instance_id, COUNT(*) AS records, MIN(l.level) AS min_level, MAX(l.level) AS max_level, "
            "MIN(CASE WHEN l.message LIKE %s THEN regexp_extract(l.message, %s, 1)::DOUBLE ELSE NULL END) AS spawn_seconds "
            "FROM openstack_anomalous_instances a JOIN openstack_logs l ON l.instance_id = a.instance_id "
            "GROUP BY a.instance_id ORDER BY spawn_seconds",
            ("%seconds to spawn the instance on the hypervisor%", "Took ([0-9.]+) seconds to spawn"),
        ).fetchall()
    report = dict(recorded_at=datetime.now(timezone.utc).isoformat(), version=version, openstack_rows=corpus,
                  repetitions=args.repetitions,
                  timing_scope="Client wall time executing/fetching rows plus full count; one warm-up per case, warm cache; excludes connection setup and MCP overhead",
                  benchmarks=timings, background_contexts=backgrounds, research_results=research,
                  labelled_vm_structured_records=labels, **anyio.run(mcp_evidence, settings))
    encoded = json.dumps(report, indent=2, default=lambda v: v.isoformat() if isinstance(v, datetime) else str(v)) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
        print(json.dumps(dict(output=str(args.output), version=version, openstack_rows=corpus,
                              benchmarks={k: dict(**v["summary"], median_ms=v["median_ms"]) for k, v in timings.items()},
                              labelled_vm_structured_records=labels, spawn_statistics=research["Q3"]), indent=2, default=str))
    else:
        print(encoded, end="")


if __name__ == "__main__":
    main()
