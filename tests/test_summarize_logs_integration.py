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
from log_insights.ownership import OWNERSHIP_TABLE

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_SERENEDB_INTEGRATION_TESTS") != "1",
    reason="Set RUN_SERENEDB_INTEGRATION_TESTS=1 to test the loaded local SereneDB",
)


def _serialize_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            key: value.isoformat() if isinstance(value, datetime) else value
            for key, value in row.items()
        }
        for row in rows
    ]


def _direct_grouped_query(
    settings: Settings, sql: str, parameters: tuple[Any, ...], count_sql: str,
    count_parameters: tuple[Any, ...],
) -> tuple[list[dict[str, Any]], int]:
    with connect(settings) as connection:
        with connection.cursor() as cursor:
            cursor.execute(sql, parameters)
            groups = cursor.fetchall()
            cursor.execute(count_sql, count_parameters)
            total = int(cursor.fetchone()["total"])
    return _serialize_rows(groups), total


def test_mcp_grouped_summaries_match_independent_sql() -> None:
    load_dotenv()
    settings = Settings.from_env()

    cases = (
        {
            "arguments": {
                "dataset": "bgl",
                "group_by": "component",
                "severity": "ERROR",
                "limit": 3,
            },
            "sql": f"""SELECT component, COUNT(*) AS record_count
                FROM {BGL_TABLE}
                WHERE severity = %s
                GROUP BY component
                ORDER BY record_count DESC, component
                LIMIT %s""",
            "parameters": ("ERROR", 3),
            "count_sql": f"SELECT COUNT(*) AS total FROM {BGL_TABLE} WHERE severity = %s",
            "count_parameters": ("ERROR",),
        },
        {
            "arguments": {
                "dataset": "openstack",
                "group_by": "event_id",
                "component": "nova.compute.manager",
                "limit": 5,
            },
            "sql": f"""SELECT event_id, COUNT(*) AS record_count
                FROM {OPENSTACK_TABLE}
                WHERE component = %s
                GROUP BY event_id
                ORDER BY record_count DESC, event_id
                LIMIT %s""",
            "parameters": ("nova.compute.manager", 5),
            "count_sql": f"SELECT COUNT(*) AS total FROM {OPENSTACK_TABLE} WHERE component = %s",
            "count_parameters": ("nova.compute.manager",),
        },
        {
            "arguments": {
                "dataset": "bgl",
                "group_by": "severity",
                "limit": 10,
            },
            "sql": f"""SELECT severity, COUNT(*) AS record_count
                FROM {BGL_TABLE}
                GROUP BY severity
                ORDER BY record_count DESC, severity
                LIMIT %s""",
            "parameters": (10,),
            "count_sql": f"SELECT COUNT(*) AS total FROM {BGL_TABLE}",
            "count_parameters": (),
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
            health = await client.call_tool("health_check", {})
            assert health.structured_content["status"] == "ok"
            search = await client.call_tool(
                "search_logs", {"dataset": "bgl", "query": "core", "limit": 1}
            )
            assert not search.is_error

            for case in cases:
                response = await client.call_tool("summarize_logs", case["arguments"])
                assert not response.is_error
                actual = response.structured_content
                direct_groups, direct_total = _direct_grouped_query(
                    settings,
                    case["sql"],
                    case["parameters"],
                    case["count_sql"],
                    case["count_parameters"],
                )
                assert actual["groups"] == direct_groups
                assert actual["total_matching_record_count"] == direct_total
                assert actual["returned_group_count"] == len(direct_groups)
                assert actual["scope"]["complete_filtered_scope_aggregated"] is True
                assert actual["scope"]["development_sample"] is True

    anyio.run(call_tools)


def test_mcp_time_buckets_limits_and_empty_scope() -> None:
    load_dotenv()
    settings = Settings.from_env()

    async def call_tools() -> None:
        async with Client(build_server(settings, DATASET_SPECS)) as client:
            hourly = await client.call_tool(
                "summarize_logs",
                {
                    "dataset": "bgl",
                    "group_by": "time",
                    "interval": "hour",
                    "start_time": "2005-06-03T00:00:00",
                    "end_time": "2005-06-04T00:00:00",
                    "limit": 2,
                },
            )
            assert not hourly.is_error
            actual = hourly.structured_content
            direct_groups, direct_total = _direct_grouped_query(
                settings,
                f"""SELECT date_trunc('hour', event_time) AS bucket_start,
                    COUNT(*) AS record_count
                    FROM {BGL_TABLE}
                    WHERE event_time >= %s AND event_time < %s
                    GROUP BY date_trunc('hour', event_time)
                    ORDER BY bucket_start
                    LIMIT %s""",
                (datetime(2005, 6, 3), datetime(2005, 6, 4), 2),
                f"""SELECT COUNT(*) AS total FROM {BGL_TABLE}
                    WHERE event_time >= %s AND event_time < %s""",
                (datetime(2005, 6, 3), datetime(2005, 6, 4)),
            )
            assert actual["groups"] == direct_groups
            assert actual["total_matching_record_count"] == direct_total
            assert actual["truncated"] is True

            daily = await client.call_tool(
                "summarize_logs",
                {
                    "dataset": "openstack",
                    "group_by": "time",
                    "interval": "day",
                    "limit": 5,
                },
            )
            assert not daily.is_error
            assert daily.structured_content["groups"] == [
                {"bucket_start": "2017-05-16T00:00:00", "record_count": 2000}
            ]
            assert daily.structured_content["total_matching_record_count"] == 2000
            assert daily.structured_content["truncated"] is False

            empty = await client.call_tool(
                "summarize_logs",
                {
                    "dataset": "openstack",
                    "group_by": "severity",
                    "component": "component-that-does-not-exist",
                    "limit": 5,
                },
            )
            assert not empty.is_error
            assert empty.structured_content["groups"] == []
            assert empty.structured_content["returned_group_count"] == 0
            assert empty.structured_content["total_matching_record_count"] == 0
            assert empty.structured_content["truncated"] is False

    anyio.run(call_tools)


def test_mcp_team_summary_matches_left_join_and_reports_coverage() -> None:
    load_dotenv()
    settings = Settings.from_env()

    async def call_tools() -> None:
        async with Client(build_server(settings, DATASET_SPECS)) as client:
            response = await client.call_tool(
                "summarize_logs",
                {"dataset": "openstack", "group_by": "team", "limit": 100},
            )
            assert not response.is_error
            actual = response.structured_content

        direct_groups, direct_total = _direct_grouped_query(
            settings,
            f"""SELECT COALESCE(o.team, 'UNMAPPED') AS team,
                COUNT(*) AS record_count
                FROM {OPENSTACK_TABLE} AS l
                LEFT JOIN {OWNERSHIP_TABLE} AS o
                  ON o.dataset = %s AND o.component = l.component
                GROUP BY COALESCE(o.team, 'UNMAPPED')
                ORDER BY record_count DESC, team
                LIMIT %s""",
            ("openstack", 100),
            f"SELECT COUNT(*) AS total FROM {OPENSTACK_TABLE}",
            (),
        )
        assert actual["groups"] == direct_groups
        assert actual["total_matching_record_count"] == direct_total == 2_000
        assert actual["mapping_coverage"] == {
            "total_record_count": 2_000,
            "mapped_record_count": 2_000,
            "unmapped_record_count": 0,
            "mapped_record_percentage": 100.0,
            "total_component_count": 10,
            "mapped_component_count": 10,
            "unmapped_component_count": 0,
            "mapped_component_percentage": 100.0,
        }
        assert actual["truncated"] is False
        provenance = actual["provenance"]
        assert provenance["fictional_ownership"]["fictional"] is True
        assert provenance["fictional_ownership"]["authoritative"] is False
        assert provenance["fictional_ownership"]["join_type"] == "LEFT JOIN"
        assert provenance["fictional_ownership"]["unmapped_team_label"] == "UNMAPPED"
        assert set(provenance["executed_sql"]) == {
            "aggregation",
            "total_count",
            "mapping_coverage",
        }
        assert set(provenance["parameters"]) == {
            "aggregation",
            "total_count",
            "mapping_coverage",
        }

    anyio.run(call_tools)
