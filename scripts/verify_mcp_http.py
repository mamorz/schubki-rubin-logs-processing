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


async def verify_endpoint(url: str) -> dict[str, Any]:
    """Verify initialization, schemas, read-only tools, and error handling."""

    async with Client(url, read_timeout_seconds=30) as client:
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

        return {
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


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify a running Streamable HTTP MCP endpoint."
    )
    parser.add_argument(
        "--url",
        default=os.getenv("MCP_TEST_URL", "http://127.0.0.1:8000/mcp"),
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    print(json.dumps(anyio.run(verify_endpoint, args.url), indent=2, default=str))


if __name__ == "__main__":
    main()
