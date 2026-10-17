from __future__ import annotations

import os

import anyio
import pytest

from scripts.verify_mcp_http import EXPECTED_TOOLS, verify_endpoint

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_MCP_HTTP_INTEGRATION_TESTS") != "1",
    reason="Set RUN_MCP_HTTP_INTEGRATION_TESTS=1 for the running HTTP MCP server",
)


def test_streamable_http_endpoint_and_all_tools() -> None:
    url = os.getenv("MCP_TEST_URL", "http://127.0.0.1:8000/mcp")
    report = anyio.run(verify_endpoint, url)

    assert set(report["tools"]) == EXPECTED_TOOLS
    assert report["server"]["name"] == "RUBIN Log Insights"
    assert report["server"]["version"] == "1.0.0"
    assert report["search"]["returned_record_count"] > 0
    assert report["component_summary"]["total_matching_record_count"] == 2_000
    assert report["team_summary"]["mapping_coverage"][
        "mapped_record_percentage"
    ] == 100.0
    assert report["nearby_status"] == "ok"
    assert report["invalid_input_returned_error"] is True
    assert report["invalid_input_error_is_actionable"] is True
