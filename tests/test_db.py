"""MCP connection setup, failure cleanup and safe database error contracts."""

import logging

import anyio
from mcp import Client
import psycopg
import pytest

from log_insights.config import Settings
from log_insights.db import _QueryConnection, connection_factory
from log_insights.mcp_server import build_server


class SetupConnection:
    def __init__(self, *, fail_at=None, applied="15000"):
        self.fail_at = fail_at
        self.applied = applied
        self.closed = self.cursor_closed = self.committed = False
        self.statements = []

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.cursor_closed = True

    def execute(self, statement):
        self.statements.append(statement if isinstance(statement, str) else statement.as_string())
        if self.fail_at == len(self.statements):
            raise psycopg.OperationalError("setup failed")

    def fetchone(self):
        return {"max_execution_time": self.applied}

    def commit(self):
        if self.fail_at == "commit":
            raise psycopg.OperationalError("setup commit failed")
        self.committed = True

    def close(self):
        self.closed = True


def test_query_factory_is_lazy_and_configures_every_new_session(monkeypatch):
    opened = []

    def connect(settings, *, timeout_seconds):
        assert timeout_seconds == .1251
        connection = SetupConnection(applied="126")
        opened.append(connection)
        return connection

    monkeypatch.setattr("log_insights.db.connect", connect)
    factory = connection_factory(Settings(None, sql_timeout_seconds=.1251))
    assert not opened
    assert factory() is not factory()
    assert len(opened) == 2
    for connection in opened:
        assert connection.statements == ["SET SESSION max_execution_time = 126", "SHOW max_execution_time"]
        assert connection.committed and connection.cursor_closed and not connection.closed


@pytest.mark.parametrize("fail_at", [1, 2, "commit"])
def test_timeout_setup_failure_closes_connection_and_cursor(monkeypatch, fail_at):
    connection = SetupConnection(fail_at=fail_at)
    monkeypatch.setattr("log_insights.db.connect", lambda _, **kwargs: connection)
    with pytest.raises(psycopg.OperationalError):
        connection_factory(Settings(None))()
    assert connection.closed and connection.cursor_closed


def test_ignored_timeout_setting_refuses_query_connection(monkeypatch):
    connection = SetupConnection(applied="0")
    monkeypatch.setattr("log_insights.db.connect", lambda _, **kwargs: connection)
    with pytest.raises(psycopg.NotSupportedError):
        connection_factory(Settings(None))()
    assert connection.closed and connection.cursor_closed


@pytest.mark.parametrize("outcome", ["acknowledged", "cancel_failed", "drain_timed_out", "raced_success"])
def test_driver_deadline_bounds_cancellation_and_always_discards_session(monkeypatch, outcome):
    waits, cancellations, closed = [], [], []

    class StubConnection(_QueryConnection):
        def __init__(self):
            self.execution_timeout_seconds = .1

        def cancel_safe(self, *, timeout):
            cancellations.append(timeout)
            if outcome == "cancel_failed":
                raise psycopg.errors.CancellationTimeout("cancel connection unavailable")

        def close(self):
            closed.append(True)

        def __del__(self):
            pass  # This test double owns no libpq connection.

    def wait(self, gen, *args, timeout, **kwargs):
        waits.append(timeout)
        if len(waits) == 1 or outcome == "drain_timed_out":
            raise psycopg.errors._WaitTimeout("deadline")
        if outcome == "acknowledged":
            raise psycopg.errors.QueryCanceled("server acknowledgement")
        return "late success"

    monkeypatch.setattr(psycopg.Connection, "wait", wait)
    with pytest.raises(psycopg.errors.QueryCanceled, match="MCP SQL execution deadline exceeded"):
        StubConnection().wait(iter(()), timeout=.05)
    assert waits[0] == .05  # An explicitly shorter driver timeout is preserved.
    assert all(t <= 1 for t in waits + cancellations)
    assert cancellations == [1]
    assert closed == [True]


QUERY_TOOLS = [
    ("search_logs", dict(dataset="bgl", query="core")),
    ("summarize_logs", dict(dataset="bgl", group_by="component")),
    ("get_nearby_events", dict(dataset="bgl", record_id=1)),
    ("analyze_vm_spawn_performance", {}),
    ("detect_log_spikes", {}),
]


@pytest.mark.parametrize("name,arguments", QUERY_TOOLS)
@pytest.mark.parametrize("error_type,expected", [
    (psycopg.OperationalError, "Database connection unavailable"),
    (psycopg.InterfaceError, "Database connection unavailable"),
    (psycopg.errors.InvalidPassword, "Database connection unavailable"),
    (psycopg.errors.AdminShutdown, "Database connection unavailable"),
    (psycopg.errors.QueryCanceled, "SQL execution timed out"),
    (psycopg.errors.UndefinedTable, "Database query failed"),
    (psycopg.errors.OutOfMemory, "Database query failed"),
    (psycopg.errors.SerializationFailure, "Database query failed"),
])
def test_database_errors_are_safe_actionable_and_logged(monkeypatch, caplog, name, arguments, error_type, expected):
    private = "password=SECRET host=PRIVATE SELECT * FROM internal_table"

    def failed_connection():
        raise error_type(private)

    monkeypatch.setattr("log_insights.mcp_server.connection_factory", lambda _: failed_connection)

    async def run():
        async with Client(build_server(Settings(None))) as client:
            assert len((await client.list_tools()).tools) == 6
            result = await client.call_tool(name, arguments)
            assert result.is_error
            text = " ".join(block.text for block in result.content if hasattr(block, "text"))
            assert expected in text
            for secret in ("SECRET", "PRIVATE", "SELECT", "internal_table", error_type.__name__):
                assert secret not in text
            assert not (await client.call_tool("health_check", {})).is_error

    with caplog.at_level(logging.WARNING, logger="log_insights.tools"):
        anyio.run(run)
    diagnostics = [r for r in caplog.records if r.name == "log_insights.tools"]
    assert len(diagnostics) == 1
    assert name in diagnostics[0].getMessage()
    assert diagnostics[0].exc_info[0] is error_type


def test_input_validation_remains_actionable_without_connecting(monkeypatch):
    monkeypatch.setattr("log_insights.mcp_server.connection_factory", lambda _: lambda: pytest.fail("invalid input connected"))

    async def run():
        async with Client(build_server(Settings(None))) as client:
            for name, args, message in (
                ("search_logs", dict(dataset="bgl", query="core", limit=51), "limit"),
                ("summarize_logs", dict(dataset="bgl", group_by="time"), "interval is required"),
                ("get_nearby_events", dict(dataset="bgl", record_id=0), "record_id"),
                ("analyze_vm_spawn_performance", dict(start_time="bad"), "start_time"),
                ("detect_log_spikes", dict(severities=["CRITICAL"]), "severities"),
            ):
                response = await client.call_tool(name, args)
                assert response.is_error
                assert message in " ".join(b.text for b in response.content if hasattr(b, "text"))
            assert not (await client.call_tool("health_check", {})).is_error

    anyio.run(run)
