"""SELECT-only BGL filter search and temporary HTTP interoperability checks."""

from datetime import datetime, timedelta
import os
import socket
import threading

import anyio
from mcp import Client
import pytest
import uvicorn

from log_insights.config import Settings
from log_insights.db import connection_factory
from log_insights.mcp_server import build_server
from log_insights.queries import QueryService, SQL_DATASET_SPECS

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_SQL_BACKEND_INTEGRATION_TESTS") != "1",
    reason="Requires explicitly configured SereneDB with preloaded BGL/OpenStack; SELECT only",
)

START, END = datetime(2005, 9, 20, 12), datetime(2005, 9, 20, 13)


@pytest.fixture
def settings():
    settings = Settings.from_env()
    settings.require_serenedb_dsn()
    return settings


@pytest.fixture
def service(settings):
    return QueryService(connection_factory(settings), SQL_DATASET_SPECS)


def direct_search(settings, filters, *, start=START, end=END, limit=50):
    """Independent base-table records/count oracle, transferring at most 50 rows."""
    predicates, params = ["ts >= %t", "ts < %t"], [start, end]
    for key, column in (("component", "component"), ("severity", "level")):
        if key in filters:
            predicates.append(f"{column} = %s")
            params.append(filters[key])
    where = " AND ".join(predicates)
    with connection_factory(settings)() as connection:
        total = connection.execute("SELECT count(*) AS n FROM bgl_logs WHERE " + where, params).fetchone()["n"]
        rows = connection.execute(
            "SELECT line_no AS record_id, CAST(NULL AS VARCHAR) AS event_id, ts AS timestamp, "
            "component, level AS severity, message FROM bgl_logs WHERE " + where +
            " ORDER BY ts, line_no LIMIT %s", [*params, limit],
        ).fetchall()
    return rows, total


@pytest.mark.parametrize("filters", [
    dict(component="MMCS", severity="ERROR"), dict(component="MMCS"), dict(severity="ERROR"),
    dict(component="KERNEL", severity="INFO"), dict(component="NO_SUCH_COMPONENT"),
    dict(component="MMCS' OR TRUE --"), dict(severity="ERROR' OR TRUE --"),
])
def test_filter_combinations_match_raw_sql_rows_and_counts(settings, service, filters):
    rows, total = direct_search(settings, filters)
    for limit in (1, 50):
        result = service.search_logs(dataset="bgl", start_time=START, end_time=END, limit=limit, **filters)
        assert result.rows == rows[:limit]
        assert result.total_corpus_match_count == total
        assert result.returned_record_count == min(total, limit)
        assert result.truncated is (total > limit)
        assert result.applied_filters["full_text_query"] is None
    if filters == dict(component="MMCS", severity="ERROR"):
        assert total == 66563  # Measured corpus expectation, never an implementation constant.


def test_microsecond_start_inclusion_end_exclusion_and_repeatable_order(settings, service):
    filters = dict(component="MMCS", severity="ERROR")
    initial = service.search_logs(dataset="bgl", start_time=START, end_time=END, limit=50, **filters)
    first = initial.rows[0]["timestamp"]
    for start, end in ((first, first + timedelta(microseconds=1)),
                       (first - timedelta(microseconds=1), first)):
        expected_rows, total = direct_search(settings, filters, start=start, end=end)
        result = service.search_logs(dataset="bgl", start_time=start, end_time=end, limit=50, **filters)
        assert result.rows == expected_rows
        assert result.total_corpus_match_count == total
        assert result.truncated is (total > 50)
        assert all(start <= r["timestamp"] < end for r in result.rows)
    again = service.search_logs(dataset="bgl", start_time=START, end_time=END, limit=50, **filters)
    assert initial.rows == again.rows
    keys = [(r["timestamp"], r["record_id"]) for r in initial.rows]
    assert keys == sorted(keys) and len(set(keys)) == len(keys)


@pytest.mark.parametrize("mode", ["auto", "legacy"])
def test_spike_filter_search_and_nearby_over_temporary_http(settings, mode):
    _, expected = direct_search(settings, dict(component="MMCS", severity="ERROR"))
    app = build_server(settings).streamable_http_app(host="127.0.0.1")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(16)
        server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            async def run():
                with anyio.fail_after(10):
                    while not server.started:
                        await anyio.sleep(.02)
                url = f"http://127.0.0.1:{listener.getsockname()[1]}/mcp"
                async with Client(url, mode=mode, read_timeout_seconds=10) as client:
                    assert len((await client.list_tools()).tools) == 6
                    spike_result = await client.call_tool("detect_log_spikes", dict(
                        component="MMCS", start_time=START.isoformat(), end_time=END.isoformat(),
                        max_findings=1, patterns_per_finding=5,
                    ))
                    assert not spike_result.is_error
                    finding = spike_result.structured_content["findings"][0]
                    severities = {p["severity"] for p in finding["contributors"]}
                    assert severities == {"ERROR"} and not finding["patterns_truncated"]
                    args = dict(dataset="bgl", component=finding["component"], severity=next(iter(severities)),
                                start_time=finding["start_time"], end_time=finding["end_time"], limit=50)
                    omitted = await client.call_tool("search_logs", args)
                    null = await client.call_tool("search_logs", dict(args, query=None))
                    assert not omitted.is_error and not null.is_error
                    data = omitted.structured_content
                    assert data == null.structured_content
                    assert data["total_corpus_match_count"] == finding["selected_record_count"] == expected == 66563
                    assert data["returned_record_count"] == 50 and data["truncated"]
                    filters = data["provenance"]["applied_filters"]
                    assert filters["start_time"] == finding["start_time"]
                    assert filters["end_time"] == finding["end_time"]
                    assert filters["severity"] == "ERROR" and filters["full_text_query"] is None
                    record = data["records"][0]
                    nearby = await client.call_tool("get_nearby_events", dict(
                        dataset="bgl", record_id=record["record_id"], before=1, after=1,
                    ))
                    assert not nearby.is_error
                    assert nearby.structured_content["anchor"]["record_id"] == record["record_id"]
                    assert nearby.structured_content["anchor"]["timestamp"] == record["timestamp"]
                    for match_mode in ("any", "all", "phrase", "exact"):
                        no_text = await client.call_tool("search_logs", dict(args, match_mode=match_mode, limit=1))
                        assert not no_text.is_error
                        assert no_text.structured_content["total_corpus_match_count"] == expected
                        text = await client.call_tool("search_logs", dict(args, match_mode=match_mode, query=record["message"], limit=1))
                        assert not text.is_error and text.structured_content["total_corpus_match_count"] > 0
                    for ids in (
                        dict(request_id="req-d6143bd4-9784-4e95-a0c8-83fba00530b6"),
                        dict(instance_id="544fd51c-4edc-4780-baae-ba1d80a0acfc"),
                    ):
                        traced = await client.call_tool("search_logs", dict(dataset="openstack", limit=1, **ids))
                        assert not traced.is_error and traced.structured_content["total_corpus_match_count"] > 0
                    invalid = await client.call_tool("search_logs", dict(dataset="bgl", start_time=START.isoformat(), end_time=END.isoformat()))
                    assert invalid.is_error and "component or severity" in invalid.content[0].text
                    assert not (await client.call_tool("health_check", {})).is_error
            anyio.run(run)
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            assert not thread.is_alive()
