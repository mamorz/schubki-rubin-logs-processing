"""Execute the SQL adapter on disposable in-memory fixtures, never SereneDB.

SQLite exercises IDs, filtering, joins, coverage and chronological behavior.
The fixture translates only psycopg placeholders and full-text @@ to LIKE;
it does NOT certify SereneDB indexing/tokenization or the SQL loader syntax.
"""

from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
import os
import re
import sqlite3
import socket
import threading

import anyio
from mcp import Client
import pytest
import uvicorn

from log_insights.config import Settings
from log_insights.mcp_server import build_server
from log_insights.queries import (
    DATASET_SPECS, SQL_DATASET_SPECS, QueryService,
    build_aggregation_query_plan, build_nearby_query_plan, build_search_query_plan,
)
from scripts.check_sql_backend import check_sql_backend
from scripts.verify_mcp_http import verify_endpoint


class Cursor:
    def __init__(self, connection):
        self.raw = connection.cursor()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.raw.close()

    def execute(self, sql, parameters=None):
        sql = re.sub(r"%\((\w+)\)s", r":\1", sql)
        sql = sql.replace("message @@ :text", "message LIKE '%' || :text || '%'")
        sql = sql.replace("message @@ %s", "message LIKE '%' || ? || '%'")
        if isinstance(parameters, dict):
            parameters = {
                key: value.isoformat(sep=" ") if isinstance(value, datetime) else value
                for key, value in parameters.items()
            }
        self.raw.execute(sql, parameters or {})

    def _row(self, row):
        if row is None:
            return None
        result = dict(row)
        if "timestamp" in result:
            result["timestamp"] = datetime.fromisoformat(result["timestamp"])
        return result

    def fetchone(self):
        return self._row(self.raw.fetchone())

    def fetchall(self):
        return [self._row(row) for row in self.raw.fetchall()]


@pytest.fixture
def sql_fixture():
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    # Use the actual teammate table DDL, excluding Serene-specific dictionary SQL.
    schema = (Path(__file__).resolve().parents[1] / "sql/01_schema.sql").read_text()
    schema = re.sub(r"--[^\n]*", "", schema)
    for ddl in re.findall(r"CREATE TABLE \w+\s*\(.*?\);", schema, re.S):
        db.execute(ddl)
    db.executemany(
        "INSERT INTO bgl_logs (line_no, ts, epoch, is_alert, type, component, level, message) "
        "VALUES (?, ?, 1, 0, 'RAS', ?, ?, ?)",
        [(1, "2005-06-03 12:00:00", "KERNEL", "INFO", "core ready"),
         (2, "2005-06-03 12:00:01", "UNKNOWN", "ERROR", "core failed")],
    )
    db.executemany(
        "INSERT INTO openstack_logs (source_file, line_no, log_file, service, ts, "
        "level, component, is_anomaly, message) "
        "VALUES (?, 1, 'nova-compute.log', 'nova-compute', ?, 'INFO', ?, 0, 'instance ready')",
        [(name, "2017-05-16 00:00:00", component) for name, component in (
            ("openstack_abnormal", "nova.compute.manager"),
            ("openstack_normal1", "nova.compute.manager"),
            ("openstack_normal2", "unknown.component"),
        )],
    )
    db.executemany("INSERT INTO ownership VALUES (?, ?, ?, 'fixture')", [
        ("bgl", "KERNEL", "Compute Kernel"),
        ("openstack", "nova.compute.manager", "Virtualization"),
    ])
    for dataset in ("bgl", "openstack"):
        db.execute(f"CREATE VIEW {dataset}_logs_idx AS SELECT * FROM {dataset}_logs")

    class Connection:
        def cursor(self):
            return Cursor(db)

    @contextmanager
    def factory():
        yield Connection()

    yield db, factory
    db.close()


def test_native_search_columns_filters_and_parameterization():
    plan = build_search_query_plan(
        dataset="openstack", text="instance' OR 1=1 --", severity="INFO",
        start_time=datetime(2017, 5, 16), _dataset_specs=SQL_DATASET_SPECS,
    )
    assert "FROM openstack_logs_idx" in plan.sql
    assert "ts >= %(start_time)s" in plan.sql
    assert "level = %(severity)s" in plan.sql
    assert "CAST(NULL AS VARCHAR) AS event_id" in plan.sql
    assert "instance' OR" not in plan.sql
    assert plan.parameters["text"] == "instance' OR 1=1 --"


def test_search_and_nearby_disambiguate_three_files(sql_fixture):
    _, factory = sql_fixture
    service = QueryService(factory, SQL_DATASET_SPECS)
    search = service.search_logs(dataset="openstack", text="instance", limit=2)
    assert [row["record_id"] for row in search.rows] == [1000000001, 2000000001]
    assert search.total_corpus_match_count == 3 and search.truncated
    assert all(row["event_id"] is None for row in search.rows)
    nearby = service.get_nearby_events(dataset="openstack", record_id=2000000001, before=1, after=1)
    assert nearby.before_events[0]["record_id"] == 1000000001
    assert nearby.after_events[0]["record_id"] == 3000000001
    assert nearby.development_sample is False
    assert nearby.anchor["seconds_from_anchor"] == 0
    restricted = service.get_nearby_events(dataset="openstack", record_id=2000000001, same_component=True)
    assert not restricted.after_events


@pytest.mark.parametrize("dataset,total,unmapped", [("bgl", 2, 1), ("openstack", 3, 1)])
def test_native_team_coverage_and_missing_event_ids(sql_fixture, dataset, total, unmapped):
    _, factory = sql_fixture
    service = QueryService(factory, SQL_DATASET_SPECS)
    teams = service.summarize_logs(dataset=dataset, group_by="team")
    assert teams.total_matching_record_count == total
    assert teams.mapping_coverage["unmapped_record_count"] == unmapped
    assert any(group["team"] == "UNMAPPED" for group in teams.groups)
    assert teams.ownership_provenance["mapping_table"] == "ownership"
    assert teams.ownership_provenance["mapping_file"] == "data/ownership.csv"
    assert teams.development_sample is False
    events = service.summarize_logs(dataset=dataset, group_by="event_id")
    assert list(events.groups) == [{"event_id": None, "record_count": total}]
    levels = service.summarize_logs(dataset=dataset, group_by="severity", severity="INFO")
    assert levels.groups[0]["severity"] == "INFO"


def test_native_time_and_team_predicates_use_main_names():
    for group in ("time", "team"):
        plan = build_aggregation_query_plan(
            dataset="bgl", group_by=group, interval="hour" if group == "time" else None,
            severity="ERROR", start_time=datetime(2005, 6, 3),
            _dataset_specs=SQL_DATASET_SPECS,
        )
        assert "event_time" not in plan.sql
        if group == "team":
            assert "LEFT JOIN ownership AS o" in plan.sql
            assert "l.ts >= %(start_time)s" in plan.sql
            assert "l.level = %(severity)s" in plan.sql
        else:
            assert "date_trunc('hour', ts)" in plan.sql


def test_preflight_rejects_duplicate_ownership_and_unknown_sources(sql_fixture):
    db, factory = sql_fixture
    with factory() as connection:
        assert check_sql_backend(connection)["datasets"]["openstack"]["rows"] == 3
        db.execute("INSERT INTO ownership VALUES ('bgl', 'KERNEL', 'Other', 'duplicate')")
        with pytest.raises(ValueError, match="duplicate"):
            check_sql_backend(connection)
        db.execute("DELETE FROM ownership WHERE team = 'Other'")
        db.execute("UPDATE openstack_logs SET source_file = 'unknown' WHERE source_file = 'openstack_normal2'")
        with pytest.raises(ValueError, match="identities"):
            check_sql_backend(connection)


def test_default_server_routes_all_tools_to_sql_fixture(monkeypatch, sql_fixture):
    _, factory = sql_fixture
    monkeypatch.setattr("log_insights.mcp_server.connection_factory", lambda _: factory)

    async def run():
        async with Client(build_server(Settings(None))) as client:
            assert len((await client.list_tools()).tools) == 4
            assert not (await client.call_tool("health_check", {})).is_error
            for dataset, term in (("bgl", "core"), ("openstack", "instance")):
                search = await client.call_tool("search_logs", {"dataset": dataset, "query": term})
                assert not search.is_error
                assert all(row["event_id"] is None for row in search.structured_content["records"])
                summary = await client.call_tool("summarize_logs", {"dataset": dataset, "group_by": "team"})
                assert not summary.is_error
                assert summary.structured_content["scope"]["development_sample"] is False
                nearby = await client.call_tool("get_nearby_events", {
                    "dataset": dataset, "record_id": search.structured_content["records"][0]["record_id"],
                })
                assert not nearby.is_error
                assert nearby.structured_content["scope"]["development_sample"] is False

    anyio.run(run)


def test_backend_selection_is_explicit(monkeypatch):
    monkeypatch.delenv("MCP_DATASET_BACKEND", raising=False)
    assert Settings.from_env().dataset_backend == "sql"
    monkeypatch.setenv("MCP_DATASET_BACKEND", "sample")
    assert Settings.from_env().dataset_backend == "sample"
    legacy = build_nearby_query_plan(dataset="bgl", record_id=1, _dataset_specs=DATASET_SPECS)
    assert "FROM loghub_bgl_sample_v1" in legacy.anchor_sql
    monkeypatch.setenv("MCP_DATASET_BACKEND", "unrecognized")
    with pytest.raises(ValueError, match="MCP_DATASET_BACKEND"):
        Settings.from_env()


@pytest.mark.skipif(
    os.getenv("RUN_LOCAL_HTTP_FIXTURE_TESTS") != "1",
    reason="Opt in to loopback sockets; uses only disposable in-memory fixtures",
)
@pytest.mark.parametrize("mode", ["auto", "legacy"])
def test_real_http_with_sql_adapter_and_ephemeral_fixture(monkeypatch, sql_fixture, mode):
    _, factory = sql_fixture
    monkeypatch.setattr("log_insights.mcp_server.connection_factory", lambda _: factory)
    app = build_server(Settings(None)).streamable_http_app(host="127.0.0.1")
    # Bind a random local port ourselves, avoiding other worktrees' MCP servers.
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(16)
        url = f"http://127.0.0.1:{listener.getsockname()[1]}/mcp"
        server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            async def run():
                with anyio.fail_after(10):
                    while not server.started:
                        await anyio.sleep(0.02)
                report = await verify_endpoint(url, mode=mode)
                assert report["component_summary"]["total_matching_record_count"] == 2
                assert report["team_summary"]["mapping_coverage"]["unmapped_record_count"] == 1
                assert report["nearby_status"] == "ok"
                assert report["invalid_input_error_is_actionable"]
            anyio.run(run)
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            assert not thread.is_alive()
