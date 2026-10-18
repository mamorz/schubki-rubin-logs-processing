"""Session-local deadlines and MCP recovery against the running SereneDB.

Workloads are SELECT-only: fixed half-second sleeps and bounded arithmetic.
No production query plan, data, global setting or database object is changed.
"""

from dataclasses import replace
import os
import time

import anyio
from mcp import Client
import psycopg
import pytest

from log_insights.config import Settings
from log_insights.db import connect, connection_factory
from log_insights.mcp_server import build_server
from log_insights.queries import QueryService

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_SQL_BACKEND_INTEGRATION_TESTS") != "1",
    reason="Requires explicit SereneDB DSN; SELECT-only execution deadline checks",
)


@pytest.mark.parametrize("query", [
    "SELECT pg_sleep(0.5)",
    "SELECT sum(sin(i)) FROM range(50000000) t(i)",
])
def test_deadline_cancels_sql_releases_resources_and_recovers(query):
    settings = replace(Settings.from_env(), sql_timeout_seconds=.05)
    factory = connection_factory(settings)
    with connect(settings) as observer:
        original = observer.execute("SHOW max_execution_time").fetchone()
        connection = factory()
        pid = connection.info.backend_pid
        started = time.monotonic()
        with pytest.raises(psycopg.errors.QueryCanceled) as error:
            with connection, connection.cursor() as cursor:
                cursor.execute(query)
        elapsed = time.monotonic() - started
        assert error.value.sqlstate == "57014"
        assert elapsed < 2, f"Bounded SELECT was not interrupted promptly: {elapsed:.3f}s"
        assert connection.closed and cursor.closed
        # Verify the setting did not affect an already-open independent session.
        assert observer.execute("SHOW max_execution_time").fetchone() == original
        deadline = time.monotonic() + 1
        while True:
            active = observer.execute("SELECT count(*) AS n FROM pg_stat_activity WHERE pid = %s", (pid,)).fetchone()["n"]
            if active == 0 or time.monotonic() >= deadline:
                break
            time.sleep(.01)
        assert active == 0, "Timed-out backend session remained after connection cleanup"
    with factory() as subsequent:
        assert subsequent.execute("SELECT 1 AS ok").fetchone()["ok"] == 1
    with connect(settings) as fresh:
        assert fresh.execute("SHOW max_execution_time").fetchone() == original


def test_native_setting_enforces_analytical_timeout_without_driver_fallback():
    # Exercise a plain psycopg connection: no MCP driver deadline can mask a
    # nonfunctional server setting. The SELECT is bounded even if it regresses.
    with connect(Settings.from_env()) as connection:
        connection.execute("SET SESSION max_execution_time = 20")
        connection.commit()
        with pytest.raises(psycopg.errors.QueryCanceled) as error:
            connection.execute("SELECT sum(sin(i)) FROM range(50000000) t(i)")
        assert error.value.sqlstate == "57014"


def test_connection_failure_then_successful_query_on_same_mcp_client(monkeypatch):
    settings = Settings.from_env()
    factory = connection_factory(settings)
    attempts = 0

    def flaky_factory():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise psycopg.OperationalError("simulated connection failure password=PRIVATE")
        return factory()

    monkeypatch.setattr("log_insights.mcp_server.connection_factory", lambda _: flaky_factory)

    async def run():
        async with Client(build_server(settings)) as client:
            args = dict(dataset="openstack", instance_id="no-such-instance", limit=1)
            failed = await client.call_tool("search_logs", args)
            assert failed.is_error
            assert "Database connection unavailable" in failed.content[0].text
            assert "PRIVATE" not in failed.content[0].text
            recovered = await client.call_tool("search_logs", args)
            assert not recovered.is_error
            assert recovered.structured_content["total_corpus_match_count"] == 0

    anyio.run(run)
    assert attempts == 2


def test_mcp_timeout_then_successful_query_on_same_client(monkeypatch):
    settings = replace(Settings.from_env(), sql_timeout_seconds=.05)
    factory = connection_factory(settings)
    connections, cursors = [], []
    original_search = QueryService.search_logs
    calls = 0

    def delayed_first_search(self, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            connection = factory()
            connections.append(connection)
            with connection, connection.cursor() as cursor:
                cursors.append(cursor)
                cursor.execute("SELECT pg_sleep(0.5)")
        return original_search(self, **kwargs)

    monkeypatch.setattr(QueryService, "search_logs", delayed_first_search)

    async def run():
        async with Client(build_server(settings), read_timeout_seconds=5) as client:
            failed = await client.call_tool("search_logs", dict(dataset="bgl", query="Ido packet timeout", match_mode="exact", limit=1))
            assert failed.is_error
            assert "SQL execution timed out" in failed.content[0].text
            assert connections[0].closed and cursors[0].closed
            recovered = await client.call_tool("search_logs", dict(dataset="openstack", instance_id="no-such-instance", limit=1))
            assert not recovered.is_error
            assert recovered.structured_content["total_corpus_match_count"] == 0
            assert not (await client.call_tool("health_check", {})).is_error

    anyio.run(run)


def test_all_six_tools_succeed_with_default_deadline():
    async def run():
        async with Client(build_server(Settings.from_env())) as client:
            assert len((await client.list_tools()).tools) == 6
            for name, arguments in (
                ("health_check", {}),
                ("search_logs", dict(dataset="bgl", query="Ido packet timeout", match_mode="exact", limit=1)),
                ("summarize_logs", dict(dataset="bgl", group_by="severity", limit=1)),
                ("get_nearby_events", dict(dataset="bgl", record_id=3395426, before=1, after=1)),
                ("analyze_vm_spawn_performance", dict(top_limit=1, bucket_limit=1)),
                ("detect_log_spikes", dict(component="MMCS", max_findings=1)),
            ):
                result = await client.call_tool(name, arguments)
                assert not result.is_error, (name, result)

    anyio.run(run)
