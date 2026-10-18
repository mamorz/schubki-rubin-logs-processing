from __future__ import annotations

from datetime import datetime
import os

import anyio
from dotenv import load_dotenv
from mcp import Client
import pytest

from log_insights.config import Settings
from log_insights.db import connect
from log_insights.ingestion import BGL_SEARCH_INDEX, BGL_TABLE, OPENSTACK_SEARCH_INDEX, OPENSTACK_TABLE
from log_insights.mcp_server import build_server
from log_insights.queries import DATASET_SPECS

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_SERENEDB_INTEGRATION_TESTS") != "1",
    reason="Set RUN_SERENEDB_INTEGRATION_TESTS=1 to test the loaded local SereneDB",
)


def _iso(value: datetime) -> str:
    return value.isoformat()


def _direct_search(
    settings: Settings,
    *,
    index: str,
    text: str,
    start_time: datetime,
    end_time: datetime,
    component: str,
    severity: str,
    limit: int,
) -> tuple[list[dict[str, object]], int]:
    table = {BGL_SEARCH_INDEX: BGL_TABLE, OPENSTACK_SEARCH_INDEX: OPENSTACK_TABLE}[index]
    with connect(settings) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""SELECT
                    line_id AS record_id,
                    event_id,
                    event_time AS timestamp,
                    component,
                    severity,
                    message
                FROM {table}
                WHERE line_id IN (SELECT line_id FROM {index} WHERE message @@ %s)
                  AND event_time >= %t
                  AND event_time < %t
                  AND component = %s
                  AND severity = %s
                ORDER BY event_time, line_id
                LIMIT %s""",
                (text, start_time, end_time, component, severity, limit),
            )
            rows = cursor.fetchall()
            cursor.execute(
                f"""SELECT COUNT(*) AS count
                FROM {table}
                WHERE line_id IN (SELECT line_id FROM {index} WHERE message @@ %s)
                  AND event_time >= %t
                  AND event_time < %t
                  AND component = %s
                  AND severity = %s""",
                (text, start_time, end_time, component, severity),
            )
            total = int(cursor.fetchone()["count"])

    serialized = [
        {
            **row,
            "timestamp": _iso(row["timestamp"]),
        }
        for row in rows
    ]
    return serialized, total


def test_mcp_search_logs_matches_direct_sql_for_both_datasets() -> None:
    load_dotenv()
    settings = Settings.from_env()

    cases = (
        {
            "dataset": "bgl",
            "index": BGL_SEARCH_INDEX,
            "query": "core",
            "start_time": "2005-06-05T00:00:00",
            "end_time": "2005-06-06T00:00:00",
            "component": "KERNEL",
            "severity": "INFO",
            "limit": 3,
        },
        {
            "dataset": "openstack",
            "index": OPENSTACK_SEARCH_INDEX,
            "query": "instance",
            "start_time": "2017-05-16T00:00:00",
            "end_time": "2017-05-16T00:01:00",
            "component": "nova.compute.manager",
            "severity": "INFO",
            "limit": 3,
        },
    )

    async def call_tools() -> None:
        server = build_server(settings, DATASET_SPECS)
        async with Client(server) as client:
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

            empty = await client.call_tool(
                "search_logs",
                {
                    "dataset": "bgl",
                    "query": "zzzzzznoexistingtokenzzzz",
                    "limit": 5,
                },
            )
            assert not empty.is_error
            assert empty.structured_content["records"] == []
            assert empty.structured_content["returned_record_count"] == 0
            assert empty.structured_content["total_corpus_match_count"] == 0
            assert empty.structured_content["truncated"] is False

            for case in cases:
                tool_arguments = {
                    key: value for key, value in case.items() if key != "index"
                }
                response = await client.call_tool("search_logs", tool_arguments)
                assert not response.is_error
                actual = response.structured_content
                assert actual is not None

                direct_rows, direct_total = _direct_search(
                    settings,
                    index=case["index"],
                    text=case["query"],
                    start_time=datetime.fromisoformat(case["start_time"]),
                    end_time=datetime.fromisoformat(case["end_time"]),
                    component=case["component"],
                    severity=case["severity"],
                    limit=case["limit"],
                )
                assert actual["records"] == direct_rows
                assert actual["returned_record_count"] == len(direct_rows)
                assert actual["total_corpus_match_count"] == direct_total
                assert actual["truncated"] is (direct_total > case["limit"])
                assert actual["provenance"]["dataset"] == case["dataset"]

    anyio.run(call_tools)
