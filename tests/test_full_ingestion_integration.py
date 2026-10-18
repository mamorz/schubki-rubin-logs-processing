from __future__ import annotations

import os

import anyio
from dotenv import load_dotenv
from mcp import Client
import pytest

from log_insights.config import Settings
from log_insights.db import connect
from log_insights.full_ingestion import (
    BGL_STAGING_SEARCH_INDEX,
    BGL_STAGING_TABLE,
    OPENSTACK_STAGING_SEARCH_INDEX,
    OPENSTACK_STAGING_TABLE,
)
from log_insights.ingestion import BGL_TABLE, OPENSTACK_TABLE
from log_insights.mcp_server import build_server
from log_insights.queries import DatasetSpec

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_SERENEDB_INTEGRATION_TESTS") != "1",
    reason="Set RUN_SERENEDB_INTEGRATION_TESTS=1 to test the loaded local SereneDB",
)

EXPECTED_STAGE_ROWS = {
    "bgl": 4_757,
    "openstack": 2_932,
}

STAGING_DATASET_SPECS = {
    "bgl": DatasetSpec(
        name="bgl",
        table=BGL_STAGING_TABLE,
        index_relation=BGL_STAGING_SEARCH_INDEX,
        scope_label="bounded original BGL staging subset",
    ),
    "openstack": DatasetSpec(
        name="openstack",
        table=OPENSTACK_STAGING_TABLE,
        index_relation=OPENSTACK_STAGING_SEARCH_INDEX,
        scope_label="bounded original OpenStack staging subset",
    ),
}


def test_staging_rows_fields_indexes_and_sample_isolation() -> None:
    load_dotenv()
    settings = Settings.from_env()
    with connect(settings) as connection:
        with connection.cursor() as cursor:
            for sample_table in (BGL_TABLE, OPENSTACK_TABLE):
                cursor.execute(f"SELECT COUNT(*) AS count FROM {sample_table}")
                assert cursor.fetchone()["count"] == 2_000

            for dataset, spec in STAGING_DATASET_SPECS.items():
                cursor.execute(
                    f"""SELECT COUNT(*) AS row_count,
                        COUNT(DISTINCT line_id) AS distinct_ids,
                        COUNT(*) FILTER (WHERE event_time IS NULL) AS missing_times,
                        COUNT(*) FILTER (WHERE component = '') AS empty_components,
                        COUNT(*) FILTER (WHERE severity = '') AS empty_severities,
                        COUNT(*) FILTER (WHERE event_id IS NOT NULL) AS event_ids,
                        COUNT(*) FILTER (WHERE event_template IS NOT NULL) AS templates,
                        MIN(event_time) AS earliest,
                        MAX(event_time) AS latest
                    FROM {spec.table}"""
                )
                row = cursor.fetchone()
                assert row["row_count"] == EXPECTED_STAGE_ROWS[dataset]
                assert row["distinct_ids"] == row["row_count"]
                assert row["missing_times"] == 0
                assert row["empty_components"] == 0
                assert row["empty_severities"] == 0
                assert row["event_ids"] == 0
                assert row["templates"] == 0
                assert row["earliest"] < row["latest"]

            cursor.execute(
                "SELECT relname FROM pg_catalog.pg_class "
                "WHERE relname IN (%s, %s)",
                (BGL_STAGING_SEARCH_INDEX, OPENSTACK_STAGING_SEARCH_INDEX),
            )
            assert {row["relname"] for row in cursor.fetchall()} == {
                BGL_STAGING_SEARCH_INDEX,
                OPENSTACK_STAGING_SEARCH_INDEX,
            }

            cursor.execute(
                f"SELECT COUNT(*) AS count FROM {BGL_STAGING_SEARCH_INDEX} "
                "WHERE message @@ %s",
                ("cache",),
            )
            assert cursor.fetchone()["count"] > 0
            cursor.execute(
                f"SELECT COUNT(*) AS count FROM {OPENSTACK_STAGING_SEARCH_INDEX} "
                "WHERE message @@ %s",
                ("instance",),
            )
            assert cursor.fetchone()["count"] > 0


def test_all_mcp_log_tools_work_against_staging_tables() -> None:
    load_dotenv()
    settings = Settings.from_env()

    async def call_tools() -> None:
        async with Client(build_server(settings, STAGING_DATASET_SPECS)) as client:
            listed = await client.list_tools()
            assert {tool.name for tool in listed.tools} == {
                "get_nearby_events",
                "health_check",
                "search_logs",
                "summarize_logs",
                "analyze_vm_spawn_performance",
            }
            assert not (await client.call_tool("health_check", {})).is_error

            for dataset, query in (("bgl", "cache"), ("openstack", "instance")):
                search = await client.call_tool(
                    "search_logs",
                    {"dataset": dataset, "query": query, "limit": 3},
                )
                assert not search.is_error
                assert search.structured_content["returned_record_count"] > 0
                assert all(
                    record["event_id"] is None
                    for record in search.structured_content["records"]
                )

                summary = await client.call_tool(
                    "summarize_logs",
                    {"dataset": dataset, "group_by": "component", "limit": 100},
                )
                assert not summary.is_error
                assert summary.structured_content["total_matching_record_count"] == (
                    EXPECTED_STAGE_ROWS[dataset]
                )

                anchor_id = search.structured_content["records"][0]["record_id"]
                nearby = await client.call_tool(
                    "get_nearby_events",
                    {
                        "dataset": dataset,
                        "record_id": anchor_id,
                        "before": 2,
                        "after": 2,
                    },
                )
                assert not nearby.is_error
                assert nearby.structured_content["status"] == "ok"

            teams = await client.call_tool(
                "summarize_logs",
                {"dataset": "openstack", "group_by": "team", "limit": 100},
            )
            assert not teams.is_error
            assert any(
                group["team"] == "UNMAPPED"
                for group in teams.structured_content["groups"]
            )
            assert teams.structured_content["mapping_coverage"][
                "unmapped_component_count"
            ] == 2

    anyio.run(call_tools)
