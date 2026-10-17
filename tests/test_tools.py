from datetime import datetime

import anyio
from mcp import Client
import pytest

from log_insights.config import Settings
from log_insights.mcp_server import build_server
from log_insights.tools import PLANNED_TOOLS, _parse_timestamp


def test_core_tool_names_are_declared() -> None:
    assert {tool.name for tool in PLANNED_TOOLS} == {
        "search_logs",
        "get_nearby_events",
        "summarize_logs",
    }


def test_health_tool_is_registered_and_callable() -> None:
    async def call_health_check() -> dict[str, str]:
        server = build_server(Settings(serenedb_dsn=None))
        async with Client(server) as client:
            listed = await client.list_tools()
            assert {tool.name for tool in listed.tools} == {
                "get_nearby_events",
                "health_check",
                "search_logs",
                "summarize_logs",
            }

            result = await client.call_tool("health_check", {})
            assert result.structured_content is not None
            return result.structured_content

    assert anyio.run(call_health_check) == {
        "status": "ok",
        "database": "not checked",
        "log_analysis_tools": "search_logs,summarize_logs,get_nearby_events",
    }


def test_search_tool_reports_invalid_parameters_without_opening_database() -> None:
    async def call_invalid_search() -> bool:
        server = build_server(Settings(serenedb_dsn=None))
        async with Client(server) as client:
            result = await client.call_tool(
                "search_logs", {"dataset": "not-a-dataset", "query": "core"}
            )
            return bool(result.is_error)

    assert anyio.run(call_invalid_search)


def test_summary_tool_reports_invalid_grouping_without_opening_database() -> None:
    async def call_invalid_summary() -> bool:
        server = build_server(Settings(serenedb_dsn=None))
        async with Client(server) as client:
            result = await client.call_tool(
                "summarize_logs",
                {"dataset": "bgl", "group_by": "message"},
            )
            return bool(result.is_error)

    assert anyio.run(call_invalid_summary)


def test_nearby_tool_reports_invalid_record_id_without_opening_database() -> None:
    async def call_invalid_nearby() -> bool:
        server = build_server(Settings(serenedb_dsn=None))
        async with Client(server) as client:
            result = await client.call_tool(
                "get_nearby_events",
                {"dataset": "bgl", "record_id": 0},
            )
            return bool(result.is_error)

    assert anyio.run(call_invalid_nearby)


def test_mcp_timestamp_parser_accepts_naive_iso_8601() -> None:
    assert _parse_timestamp("start_time", "2017-05-16T00:00:00.008") == datetime(
        2017, 5, 16, 0, 0, 0, 8000
    )


@pytest.mark.parametrize("value", ["", "not-a-time", "2017-05-16T00:00:00Z"])
def test_mcp_timestamp_parser_rejects_invalid_or_zoned_values(value: str) -> None:
    with pytest.raises(ValueError):
        _parse_timestamp("start_time", value)
