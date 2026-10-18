from __future__ import annotations

from datetime import datetime
import os
from typing import Any

import anyio
from dotenv import load_dotenv
from mcp import Client
import pytest

from log_insights.config import Settings
from log_insights.db import connect
from log_insights.ingestion import BGL_TABLE, OPENSTACK_TABLE
from log_insights.mcp_server import build_server
from log_insights.queries import DATASET_SPECS

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_SERENEDB_INTEGRATION_TESTS") != "1",
    reason="Set RUN_SERENEDB_INTEGRATION_TESTS=1 to test the loaded local SereneDB",
)


def _serialize_timeline(
    before_rows: list[dict[str, Any]],
    anchor: dict[str, Any],
    after_rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    timeline = [*before_rows, anchor, *after_rows]
    anchor_time = anchor["timestamp"]
    serialized: list[dict[str, Any]] = []
    previous_time: datetime | None = None
    for row in timeline:
        timestamp = row["timestamp"]
        serialized.append(
            {
                **row,
                "timestamp": timestamp.isoformat(),
                "seconds_from_anchor": (timestamp - anchor_time).total_seconds(),
                "gap_from_previous_seconds": (
                    None
                    if previous_time is None
                    else (timestamp - previous_time).total_seconds()
                ),
            }
        )
        previous_time = timestamp
    before_count = len(before_rows)
    return (
        serialized[:before_count],
        serialized[before_count],
        serialized[before_count + 1 :],
    )


def _direct_nearby(
    settings: Settings,
    *,
    table: str,
    record_id: int,
    before: int,
    after: int,
    same_component: bool,
) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    columns = """line_id AS record_id, event_id, event_time AS timestamp,
        component, severity, message"""
    with connect(settings) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT {columns} FROM {table} WHERE line_id = %s", (record_id,)
            )
            anchor = cursor.fetchone()
            assert anchor is not None
            component_clause = " AND component = %s" if same_component else ""
            component_parameters = (anchor["component"],) if same_component else ()
            cursor.execute(
                f"""SELECT {columns} FROM {table}
                WHERE (event_time < %s OR (event_time = %s AND line_id < %s))
                {component_clause}
                ORDER BY event_time DESC, line_id DESC LIMIT %s""",
                (
                    anchor["timestamp"],
                    anchor["timestamp"],
                    record_id,
                    *component_parameters,
                    before,
                ),
            )
            before_rows = list(reversed(cursor.fetchall()))
            cursor.execute(
                f"""SELECT {columns} FROM {table}
                WHERE (event_time > %s OR (event_time = %s AND line_id > %s))
                {component_clause}
                ORDER BY event_time, line_id LIMIT %s""",
                (
                    anchor["timestamp"],
                    anchor["timestamp"],
                    record_id,
                    *component_parameters,
                    after,
                ),
            )
            after_rows = cursor.fetchall()
    return _serialize_timeline(before_rows, anchor, after_rows)


def _boundary_record_id(settings: Settings, table: str, *, first: bool) -> int:
    direction = "ASC" if first else "DESC"
    with connect(settings) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT line_id FROM {table} ORDER BY event_time {direction}, "
                f"line_id {direction} LIMIT 1"
            )
            return int(cursor.fetchone()["line_id"])


def _equal_timestamp_anchor(settings: Settings) -> tuple[int, datetime]:
    with connect(settings) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""SELECT event_time, MAX(line_id) AS line_id
                FROM {OPENSTACK_TABLE}
                GROUP BY event_time
                HAVING COUNT(*) > 1
                ORDER BY event_time
                LIMIT 1"""
            )
            row = cursor.fetchone()
    assert row is not None
    return int(row["line_id"]), row["event_time"]


def test_mcp_nearby_events_matches_direct_sql_for_both_datasets() -> None:
    load_dotenv()
    settings = Settings.from_env()
    cases = (
        {
            "dataset": "bgl",
            "table": BGL_TABLE,
            "record_id": 12,
            "before": 3,
            "after": 3,
            "same_component": False,
        },
        {
            "dataset": "openstack",
            "table": OPENSTACK_TABLE,
            "record_id": 7,
            "before": 3,
            "after": 3,
            "same_component": True,
        },
    )

    async def call_tools() -> None:
        async with Client(build_server(settings, DATASET_SPECS)) as client:
            listed = await client.list_tools()
            assert {tool.name for tool in listed.tools} == {
                "get_nearby_events",
                "health_check",
                "search_logs",
                "summarize_logs",
                "analyze_vm_spawn_performance",
                "detect_log_spikes",
            }
            assert not (await client.call_tool("health_check", {})).is_error
            assert not (
                await client.call_tool(
                    "search_logs", {"dataset": "bgl", "query": "core", "limit": 1}
                )
            ).is_error
            assert not (
                await client.call_tool(
                    "summarize_logs",
                    {"dataset": "bgl", "group_by": "severity", "limit": 5},
                )
            ).is_error

            for case in cases:
                arguments = {key: value for key, value in case.items() if key != "table"}
                response = await client.call_tool("get_nearby_events", arguments)
                assert not response.is_error
                actual = response.structured_content
                direct_before, direct_anchor, direct_after = _direct_nearby(
                    settings,
                    table=case["table"],
                    record_id=case["record_id"],
                    before=case["before"],
                    after=case["after"],
                    same_component=case["same_component"],
                )
                assert actual["status"] == "ok"
                assert actual["before_events"] == direct_before
                assert actual["anchor"] == direct_anchor
                assert actual["after_events"] == direct_after
                if case["same_component"]:
                    component = actual["anchor"]["component"]
                    assert all(
                        event["component"] == component
                        for event in [*actual["before_events"], *actual["after_events"]]
                    )

    anyio.run(call_tools)


def test_boundaries_equal_timestamps_and_missing_anchor() -> None:
    load_dotenv()
    settings = Settings.from_env()
    first_id = _boundary_record_id(settings, BGL_TABLE, first=True)
    last_id = _boundary_record_id(settings, BGL_TABLE, first=False)
    equal_time_id, equal_time = _equal_timestamp_anchor(settings)

    async def call_tools() -> None:
        async with Client(build_server(settings, DATASET_SPECS)) as client:
            first = await client.call_tool(
                "get_nearby_events",
                {"dataset": "bgl", "record_id": first_id, "before": 5, "after": 1},
            )
            assert first.structured_content["before_events"] == []
            assert first.structured_content["counts"]["insufficient_before"] is True

            last = await client.call_tool(
                "get_nearby_events",
                {"dataset": "bgl", "record_id": last_id, "before": 1, "after": 5},
            )
            assert last.structured_content["after_events"] == []
            assert last.structured_content["counts"]["insufficient_after"] is True

            equal = await client.call_tool(
                "get_nearby_events",
                {
                    "dataset": "openstack",
                    "record_id": equal_time_id,
                    "before": 1,
                    "after": 1,
                },
            )
            actual = equal.structured_content
            assert actual["before_events"][-1]["timestamp"] == equal_time.isoformat()
            assert actual["anchor"]["timestamp"] == equal_time.isoformat()
            assert actual["anchor"]["gap_from_previous_seconds"] == 0.0
            assert actual["before_events"][-1]["record_id"] < actual["anchor"]["record_id"]

            missing = await client.call_tool(
                "get_nearby_events",
                {"dataset": "openstack", "record_id": 999999, "before": 2, "after": 2},
            )
            assert not missing.is_error
            content = missing.structured_content
            assert content["status"] == "anchor_not_found"
            assert content["anchor"] is None
            assert content["before_events"] == []
            assert content["after_events"] == []
            assert set(content["provenance"]["executed_sql"]) == {"anchor"}

    anyio.run(call_tools)
