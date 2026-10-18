"""Exercise the running MCP server through its Streamable HTTP endpoint."""

from __future__ import annotations

import argparse
import json
import os
from typing import Any

import anyio
from mcp import Client

EXPECTED_TOOLS = {
    "get_nearby_events",
    "health_check",
    "search_logs",
    "summarize_logs",
}
REQUIRED_INPUTS = {
    "get_nearby_events": {"dataset", "record_id"},
    "health_check": set(),
    "search_logs": {"dataset", "query"},
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


async def verify_endpoint(url: str, *, mode: str = "auto", patterns: bool = False) -> dict[str, Any]:
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
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    async def run():
        return await verify_endpoint(args.url, patterns=args.patterns)
    print(json.dumps(anyio.run(run), indent=2, default=str))


if __name__ == "__main__":
    main()
