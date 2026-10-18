"""Exercise the running MCP server through its Streamable HTTP endpoint."""

from __future__ import annotations

import argparse
import json
import os
from typing import Any

import anyio
from mcp import Client

EXPECTED_TOOLS = {
    "detect_log_spikes",
    "analyze_vm_spawn_performance",
    "get_nearby_events",
    "health_check",
    "search_logs",
    "summarize_logs",
}
REQUIRED_INPUTS = {
    "detect_log_spikes": set(),
    "analyze_vm_spawn_performance": set(),
    "get_nearby_events": {"dataset", "record_id"},
    "health_check": set(),
    "search_logs": {"dataset"},
    "summarize_logs": {"dataset", "group_by"},
}


def _require_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AssertionError(f"{label} must be a JSON object")
    return value


async def verify_pattern_aggregations(client: Client) -> dict[str, Any]:
    """Smoke-test the current aggregation schema and full SQL-corpus behavior."""

    tool = next(t for t in (await client.list_tools()).tools if t.name == "summarize_logs")
    properties = tool.input_schema["properties"]
    assert set(tool.input_schema["required"]) == {"dataset", "group_by"}
    assert properties["query"]["default"] is None
    assert properties["match_mode"]["default"] == "any"
    assert set(properties["match_mode"]["enum"]) == {"any", "all", "phrase", "exact"}
    assert properties["sort_direction"]["default"] == "asc"
    assert set(properties["sort_direction"]["enum"]) == {"asc", "desc"}
    totals = {}
    for dataset, term in (("bgl", "generating core.304"), ("openstack", "instance")):
        seed = await client.call_tool("search_logs", dict(dataset=dataset, query=term, limit=1))
        assert not seed.is_error
        raw_message = seed.structured_content["records"][0]["message"]
        totals[dataset] = {}
        for match_mode in ("any", "all", "phrase", "exact"):
            query = raw_message if match_mode == "exact" else term
            search = await client.call_tool("search_logs", dict(dataset=dataset, query=query, match_mode=match_mode, limit=1))
            assert not search.is_error
            expected = search.structured_content["total_corpus_match_count"]
            for group in ("component", "team", "time"):
                args = dict(dataset=dataset, query=query, match_mode=match_mode, group_by=group, limit=100)
                if group == "time":
                    args["interval"] = "day"
                response = await client.call_tool("summarize_logs", args)
                assert not response.is_error
                data = response.structured_content
                assert data["total_matching_record_count"] == expected > 0
                assert data["applied_filters"]["match_mode"] == match_mode
                assert "LIMIT" not in data["provenance"]["executed_sql"]["total_count"]
                assert data["scope"]["complete_filtered_scope_aggregated"] is True
                if not data["truncated"]:
                    assert sum(g["record_count"] for g in data["groups"]) == expected
                if group == "team":
                    assert data["mapping_coverage"]["total_record_count"] == expected
                    assert data["provenance"]["fictional_ownership"]["fictional"] is True
            totals[dataset][match_mode] = expected
    filtered = await client.call_tool("summarize_logs", dict(
        dataset="bgl", group_by="time", interval="day", query="generating core.304",
        match_mode="phrase", start_time="2005-06-03T00:00:00", end_time="2005-07-03T00:00:00",
        component="KERNEL", severity="INFO", limit=1,
    ))
    assert not filtered.is_error
    assert filtered.structured_content["total_matching_record_count"] == 92
    assert filtered.structured_content["returned_group_count"] == 1
    assert filtered.structured_content["truncated"] is True
    for interval in ("week", "month"):
        response = await client.call_tool("summarize_logs", dict(
            dataset="bgl", group_by="time", interval=interval, sort_direction="desc",
            query="data storage interrupt", match_mode="phrase", limit=1,
        ))
        assert not response.is_error
        assert response.structured_content["applied_filters"]["sort_direction"] == "desc"
        assert response.structured_content["total_matching_record_count"] == 63_493
        assert response.structured_content["truncated"] is True
    empty = await client.call_tool("summarize_logs", dict(
        dataset="openstack", group_by="team", query="rubinnomatchsentinelzqx991827", match_mode="phrase",
    ))
    assert not empty.is_error
    assert empty.structured_content["total_matching_record_count"] == 0
    assert empty.structured_content["groups"] == []
    assert empty.structured_content["truncated"] is False
    invalid = await client.call_tool("summarize_logs", dict(dataset="bgl", group_by="component", match_mode="fuzzy"))
    assert invalid.is_error
    return {"totals": totals, "timestamp_regression_count": 92}


async def verify_openstack_tracing(client: Client) -> dict[str, Any]:
    """Verify literal tracing and metadata on the existing full SQL corpus."""
    request_id = "req-d6143bd4-9784-4e95-a0c8-83fba00530b6"
    instance_id = "544fd51c-4edc-4780-baae-ba1d80a0acfc"
    tool = next(t for t in (await client.list_tools()).tools if t.name == "search_logs")
    assert set(tool.input_schema["required"]) == {"dataset"}
    for field in ("query", "request_id", "instance_id"):
        assert tool.input_schema["properties"][field]["default"] is None
    async def search(**args):
        response = await client.call_tool("search_logs", dict(dataset="openstack", **args))
        assert not response.is_error
        data = response.structured_content
        assert "LIMIT" not in data["provenance"]["executed_sql"]["total_count"]
        for row in data["records"]:
            assert {"request_id", "instance_id", "service", "http_status", "latency_s", "is_anomaly"} <= row.keys()
        return data
    request = await search(request_id=request_id, limit=50)
    assert request["total_corpus_match_count"] == request["returned_record_count"] == 12
    assert request["truncated"] is False
    api = next(r for r in request["records"] if r["service"] == "nova-api")
    assert api["instance_id"] is None and api["is_anomaly"] is False
    assert api["http_status"] == 202 and round(api["latency_s"], 3) == 0.479
    instance = await search(instance_id=instance_id, limit=2)
    assert instance["total_corpus_match_count"] == 25 and instance["returned_record_count"] == 2
    assert instance["truncated"] is True
    assert all(r["instance_id"] == instance_id and r["latency_s"] is None for r in instance["records"])
    spawn = await search(request_id=request_id, instance_id=instance_id, query="41.81 seconds to spawn",
                         match_mode="phrase", start_time="2017-05-14T21:07:00", end_time="2017-05-14T21:09:00",
                         component="nova.compute.manager", severity="INFO", limit=50)
    assert spawn["total_corpus_match_count"] == 1 and not spawn["truncated"]
    assert "41.81" in spawn["records"][0]["message"]
    mentions = await search(query=instance_id, match_mode="phrase", limit=50)
    assert mentions["total_corpus_match_count"] == 28 and not mentions["truncated"]
    assert sum(r["service"] == "nova-api" for r in mentions["records"]) == 3
    empty = await search(instance_id="missing' OR 1=1 --", limit=1)
    assert empty["total_corpus_match_count"] == 0 and empty["records"] == [] and not empty["truncated"]
    for args in ({"dataset": "bgl", "request_id": request_id}, {"dataset": "openstack"}):
        assert (await client.call_tool("search_logs", args)).is_error
    return dict(request_count=12, instance_count=25, message_mention_count=28, spawn_count=1)


async def verify_vm_spawn_performance(client: Client) -> dict[str, Any]:
    """Validate full-corpus baselines, strict thresholds and bounded outputs."""
    tool = next(t for t in (await client.list_tools()).tools if t.name == "analyze_vm_spawn_performance")
    assert not tool.input_schema.get("required")
    assert tool.input_schema["properties"]["baseline_multiplier"]["default"] == 1.25
    assert tool.input_schema["properties"]["baseline_scope"]["default"] == "corpus"
    async def analyze(**args):
        response = await client.call_tool(tool.name, args)
        assert not response.is_error
        data = response.structured_content
        assert data["provenance"]["statement_count"] == 1
        assert data["scope"]["complete_population_aggregated"] is True
        return data
    small = await analyze(top_limit=1, bucket_limit=1)
    complete = await analyze(top_limit=50, bucket_limit=100, include_anomaly_labels=True)
    assert small["counts"]["spawn_event_count"] == 2066
    assert small["counts"]["invalid_duration_record_count"] == 0
    for key in ("counts", "statistics", "baseline", "detection"):
        assert small[key] == complete[key]
    stats = complete["statistics"]
    assert abs(stats["median_seconds"] - 19.74) < 1e-10
    assert abs(stats["p95_seconds"] - 21.0075) < 1e-10
    assert abs(stats["p99_seconds"] - 21.6635) < 1e-10
    assert stats["min_seconds"] == 10.12 and stats["max_seconds"] == 50.72
    assert small["detection"]["slow_event_count"] == 5
    assert small["ranking"]["truncated"] and small["ranking"]["slow_events_truncated"]
    assert small["time_series"]["truncated"] and not complete["time_series"]["truncated"]
    assert sum(b["spawn_event_count"] for b in complete["time_series"]["buckets"]) == 2066
    assert sum(b["slow_event_count"] for b in complete["time_series"]["buckets"]) == 5
    vm = next(r for r in complete["slowest_spawn_events"] if r["instance_id"] == "544fd51c-4edc-4780-baae-ba1d80a0acfc")
    assert vm["duration_seconds"] == 41.81
    assert "is_anomaly" not in small["slowest_spawn_events"][0]
    for threshold, expected in ((25.0, 5), (30.0, 3), (50.72, 0)):
        data = await analyze(slow_threshold_seconds=threshold)
        assert data["detection"]["slow_event_count"] == expected
    invalid = await client.call_tool(tool.name, {"slow_threshold_seconds": -1})
    assert invalid.is_error
    return dict(spawn_event_count=2066, statistics=stats, default_slow_event_count=5,
                effective_threshold_seconds=small["detection"]["effective_threshold_seconds"])


async def verify_log_spikes(client: Client) -> dict[str, Any]:
    """Verify measured full-corpus spikes and independent contributor counts."""
    name = "detect_log_spikes"
    tool = next(t for t in (await client.list_tools()).tools if t.name == name)
    assert not tool.input_schema.get("required")
    assert tool.input_schema["properties"]["baseline_quantile"]["default"] == .99

    async def detect(**args):
        result = await client.call_tool(name, args)
        assert not result.is_error
        data = result.structured_content
        assert data["provenance"]["statement_count"] == 1
        assert not data["method"]["labels_used_for_detection"]
        return data

    full = await detect(max_findings=20, patterns_per_finding=5)
    small = await detect(max_findings=1, patterns_per_finding=1, evidence_per_pattern=3)
    assert full["counts"]["corpus_record_count"] == 4747963
    assert full["counts"]["elevated_component_bucket_count"] == 24
    assert full["counts"]["spike_window_count"] == 16
    for key in ("baselines", "coverage", "method"):
        assert full[key] == small[key]
    assert {k:v for k,v in full["counts"].items() if k != "returned_spike_window_count"} == {
        k:v for k,v in small["counts"].items() if k != "returned_spike_window_count"}
    assert not full["truncation"]["findings_truncated"]
    assert small["truncation"]["findings_truncated"]
    for finding in full["findings"] + small["findings"]:
        assert sum(p["record_count"] for p in finding["contributors"]) + finding["omitted_pattern_record_count"] == finding["selected_record_count"]
        for pattern in finding["contributors"]:
            assert pattern["evidence"] and pattern["evidence"][0]["record_id"] > 0
    mmcs = next(f for f in full["findings"] if f["component"] == "MMCS" and f["start_time"] == "2005-09-20T12:00:00")
    assert mmcs["selected_record_count"] == 66563
    assert [p["record_count"] for p in mmcs["contributors"]] == [63468, 3095]
    assert mmcs["omitted_pattern_record_count"] == 0
    daily = await detect(interval="day")
    assert daily["counts"]["spike_window_count"] == 2
    empty = await detect(component="NO_SUCH_COMPONENT")
    assert not empty["findings"]
    invalid = await client.call_tool(name, {"severities": ["CRITICAL"]})
    assert invalid.is_error
    return dict(corpus_record_count=4747963, elevated_component_bucket_count=24, spike_window_count=16,
                mmcs_selected_record_count=66563, mmcs_contributor_counts=[63468,3095])


async def verify_endpoint(url: str, *, mode: str = "auto", patterns: bool = False, tracing: bool = False,
                          performance: bool = False, spikes: bool = False) -> dict[str, Any]:
    """Verify initialization, schemas, read-only tools, and error handling."""

    async with Client(url, read_timeout_seconds=30, mode=mode) as client:
        server_info = client.server_info
        if server_info is None:
            raise AssertionError("MCP initialization did not return server metadata")

        listed = await client.list_tools()
        tools = {tool.name: tool for tool in listed.tools}
        if set(tools) != EXPECTED_TOOLS:
            raise AssertionError(
                f"unexpected tools: expected={sorted(EXPECTED_TOOLS)}, "
                f"actual={sorted(tools)}"
            )
        for name, tool in tools.items():
            if not tool.description:
                raise AssertionError(f"{name} is missing an agent-facing description")
            schema = tool.input_schema
            if schema.get("type") != "object":
                raise AssertionError(f"{name} input schema is not an object")
            required = set(schema.get("required", ()))
            if required != REQUIRED_INPUTS[name]:
                raise AssertionError(
                    f"{name} required inputs differ: {sorted(required)}"
                )

        health_result = await client.call_tool("health_check", {})
        if health_result.is_error:
            raise AssertionError("health_check returned an MCP error")
        health = _require_mapping(health_result.structured_content, "health_check")
        if health.get("status") != "ok":
            raise AssertionError("health_check status is not ok")

        search_result = await client.call_tool(
            "search_logs",
            {"dataset": "bgl", "query": "core", "limit": 2},
        )
        if search_result.is_error:
            raise AssertionError("search_logs returned an MCP error")
        search = _require_mapping(search_result.structured_content, "search_logs")
        records = search.get("records")
        if not isinstance(records, list) or not records:
            raise AssertionError("search_logs returned no representative records")
        search_provenance = _require_mapping(search.get("provenance"), "search provenance")
        for key in ("applied_filters", "executed_sql", "parameters"):
            if key not in search_provenance:
                raise AssertionError(f"search provenance is missing {key}")
        for key in (
            "returned_record_count",
            "total_corpus_match_count",
            "truncated",
        ):
            if key not in search:
                raise AssertionError(f"search response is missing {key}")

        component_summary_result = await client.call_tool(
            "summarize_logs",
            {"dataset": "bgl", "group_by": "component", "limit": 20},
        )
        if component_summary_result.is_error:
            raise AssertionError("component summarize_logs returned an MCP error")
        component_summary = _require_mapping(
            component_summary_result.structured_content,
            "component summarize_logs",
        )

        team_summary_result = await client.call_tool(
            "summarize_logs",
            {"dataset": "openstack", "group_by": "team", "limit": 20},
        )
        if team_summary_result.is_error:
            raise AssertionError("team summarize_logs returned an MCP error")
        team_summary = _require_mapping(
            team_summary_result.structured_content,
            "team summarize_logs",
        )
        team_provenance = _require_mapping(
            team_summary.get("provenance"), "team-summary provenance"
        )
        for key in ("fictional_ownership", "executed_sql", "parameters"):
            if key not in team_provenance:
                raise AssertionError(f"team-summary provenance is missing {key}")
        if "mapping_coverage" not in team_summary:
            raise AssertionError("team summary is missing mapping coverage")

        anchor_id = records[0].get("record_id")
        nearby_result = await client.call_tool(
            "get_nearby_events",
            {
                "dataset": "bgl",
                "record_id": anchor_id,
                "before": 1,
                "after": 1,
            },
        )
        if nearby_result.is_error:
            raise AssertionError("get_nearby_events returned an MCP error")
        nearby = _require_mapping(
            nearby_result.structured_content, "get_nearby_events"
        )
        if nearby.get("status") != "ok" or "provenance" not in nearby:
            raise AssertionError("nearby response is missing status or provenance")

        invalid_result = await client.call_tool(
            "search_logs",
            {"dataset": "not-a-dataset", "query": "core"},
        )
        if not invalid_result.is_error:
            raise AssertionError("invalid search input did not return an MCP error")
        invalid_text = " ".join(
            getattr(content, "text", "") for content in invalid_result.content
        )
        if "dataset must be one of" not in invalid_text:
            raise AssertionError("invalid search error is not actionable for an agent")

        report = {
            "endpoint": url,
            "server": server_info.model_dump(),
            "protocol_version": client.protocol_version,
            "tools": sorted(tools),
            "search": {
                "returned_record_count": search["returned_record_count"],
                "total_corpus_match_count": search["total_corpus_match_count"],
                "truncated": search["truncated"],
            },
            "component_summary": {
                "returned_group_count": component_summary["returned_group_count"],
                "total_matching_record_count": component_summary[
                    "total_matching_record_count"
                ],
            },
            "team_summary": {
                "returned_group_count": team_summary["returned_group_count"],
                "mapping_coverage": team_summary["mapping_coverage"],
            },
            "nearby_status": nearby["status"],
            "invalid_input_returned_error": True,
            "invalid_input_error_is_actionable": True,
        }
        if patterns:
            report["pattern_aggregation"] = await verify_pattern_aggregations(client)
        if tracing:
            report["openstack_tracing"] = await verify_openstack_tracing(client)
        if performance:
            report["vm_spawn_performance"] = await verify_vm_spawn_performance(client)
        if spikes:
            report["log_spikes"] = await verify_log_spikes(client)
        return report


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify a running Streamable HTTP MCP endpoint."
    )
    parser.add_argument(
        "--url",
        default=os.getenv("MCP_TEST_URL", "http://127.0.0.1:18000/mcp"),
    )
    parser.add_argument("--patterns", action="store_true", help="Also verify pattern aggregation on the full SQL corpora")
    parser.add_argument("--tracing", action="store_true", help="Also verify OpenStack structured tracing on the full SQL corpus")
    parser.add_argument("--performance", action="store_true", help="Also verify exact OpenStack spawn baselines and slow-event detection")
    parser.add_argument("--spikes", action="store_true", help="Also verify complete BGL spike windows and contributing messages")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    async def run():
        return await verify_endpoint(args.url, patterns=args.patterns, tracing=args.tracing,
                                     performance=args.performance, spikes=args.spikes)
    print(json.dumps(anyio.run(run), indent=2, default=str))


if __name__ == "__main__":
    main()
