"""Structured OpenStack filter validation, query scopes and MCP contracts."""

from datetime import datetime

import anyio
from mcp import Client
import pytest

from log_insights.config import Settings
from log_insights.mcp_server import build_server
from log_insights.queries import DATASET_SPECS, SQL_DATASET_SPECS, QueryService, build_search_query_plan

METADATA = {"request_id", "instance_id", "service", "http_status", "latency_s", "is_anomaly"}


@pytest.mark.parametrize("identifiers", [
    {"request_id": "req-' OR 1=1 --"}, {"instance_id": "vm-' OR 1=1 --"},
    {"request_id": "req-one", "instance_id": "vm-one"},
])
@pytest.mark.parametrize("mode", ["any", "all", "phrase", "exact"])
@pytest.mark.parametrize("with_text", [False, True])
def test_structured_filters_and_message_share_unlimited_scope(identifiers, mode, with_text):
    start, end = datetime(2017, 5, 14), datetime(2017, 5, 15)
    query = "  Took 41.81 seconds  " if with_text else None
    plan = build_search_query_plan(
        dataset="openstack", text=query, match_mode=mode, **identifiers,
        component="nova.compute.manager", severity="INFO", start_time=start, end_time=end,
        limit=2, _dataset_specs=SQL_DATASET_SPECS,
    )
    for sql in (plan.sql, plan.count_sql):
        assert "FROM openstack_logs\n" in sql
        for field, value in identifiers.items():
            assert f"{field} = %({field})s" in sql
            assert value not in sql
        assert "ts >= %(start_time)t" in sql and "ts < %(end_time)t" in sql
        assert "component = %(component)s" in sql and "level = %(severity)s" in sql
        if not with_text or mode == "exact":
            assert "openstack_logs_idx" not in sql
        else:
            assert "IN (SELECT" in sql
            assert "openstack_logs_idx WHERE message @@" in sql
            assert "%(text)s) AND" in sql or "%(text)s)) AND" in sql
    expected = dict(identifiers, start_time=start, end_time=end, component="nova.compute.manager", severity="INFO")
    if with_text:
        expected["text"] = query if mode == "exact" else query.strip()
    assert plan.count_parameters == expected
    assert plan.parameters == {**expected, "fetch_limit": 3}
    assert "LIMIT" not in plan.count_sql
    assert "fetch_limit" not in plan.count_parameters
    assert "ORDER BY ts," in plan.sql
    for field in METADATA:
        assert field in plan.sql
    assert plan.applied_filters["full_text_query"] == (expected.get("text"))
    for field, value in identifiers.items():
        assert plan.applied_filters[field] == value


@pytest.mark.parametrize("specs,dataset", [
    (SQL_DATASET_SPECS, "bgl"), (DATASET_SPECS, "bgl"), (DATASET_SPECS, "openstack"),
])
@pytest.mark.parametrize("field", ["request_id", "instance_id"])
def test_unsupported_backend_or_dataset_has_actionable_error(specs, dataset, field):
    with pytest.raises(ValueError, match="SQL backend.*BGL and sample"):
        build_search_query_plan(dataset=dataset, text="instance", **{field: "id"}, _dataset_specs=specs)


@pytest.mark.parametrize("field", ["request_id", "instance_id"])
@pytest.mark.parametrize("value", ["", " \t", 9, True, [], "id\x00injection"])
def test_invalid_identifiers_rejected_before_connection(field, value):
    def never_connect():
        pytest.fail("invalid filter opened a connection")
    with pytest.raises(ValueError, match=field):
        QueryService(never_connect, SQL_DATASET_SPECS).search_logs(dataset="openstack", **{field: value})


@pytest.mark.parametrize("dataset", ["bgl", "openstack"])
def test_missing_query_without_ids_rejected(dataset):
    with pytest.raises(ValueError, match="query is required"):
        build_search_query_plan(dataset=dataset, component="anything", _dataset_specs=SQL_DATASET_SPECS)


def test_explicit_blank_token_query_not_silently_ignored_when_id_supplied():
    with pytest.raises(ValueError, match="must not be empty"):
        build_search_query_plan(dataset="openstack", request_id="req-one", text=" ", _dataset_specs=SQL_DATASET_SPECS)
    exact = build_search_query_plan(dataset="openstack", request_id="req-one", text="", match_mode="exact", _dataset_specs=SQL_DATASET_SPECS)
    assert exact.count_parameters == {"text": "", "request_id": "req-one"}


@pytest.mark.parametrize("specs,dataset", [(DATASET_SPECS, "bgl"), (DATASET_SPECS, "openstack"), (SQL_DATASET_SPECS, "bgl")])
def test_non_tracing_projection_and_default_parameters_unchanged(specs, dataset):
    plan = build_search_query_plan(dataset=dataset, text="core", _dataset_specs=specs)
    assert f"SELECT\n    {specs[dataset].record_columns}\nFROM" in plan.sql
    assert plan.count_parameters == {"text": "core"}
    assert "request_id" not in plan.applied_filters
    assert "instance_id" not in plan.applied_filters


def test_tracing_metadata_projection_does_not_change_nearby_columns():
    spec = SQL_DATASET_SPECS["openstack"]
    plan = build_search_query_plan(dataset="openstack", text="instance", _dataset_specs=SQL_DATASET_SPECS)
    assert "FROM openstack_logs\n" in plan.sql
    assert "http_status" in plan.sql and "http_status" not in spec.record_columns


class Connection:
    def __init__(self, total=25, rows=3):
        self.total, self.calls = total, []
        self.rows = [dict(record_id=n, event_id=None, timestamp=datetime(2017, 5, 14),
                          component="nova.compute.manager", severity="INFO", message="Took 41.81 seconds",
                          request_id="req-one", instance_id="vm-one", service="nova-compute",
                          http_status=None, latency_s=None, is_anomaly=True) for n in range(rows)]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def cursor(self):
        return self

    def execute(self, sql, params):
        assert sql.startswith("SELECT")
        self.calls.append((sql, params))

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return {"total_corpus_match_count": self.total}


@pytest.mark.parametrize("total,rows,truncated", [(25, 3, True), (2, 2, False), (0, 0, False)])
def test_structured_service_full_count_limit_and_metadata(total, rows, truncated):
    connection = Connection(total, rows)
    result = QueryService(lambda: connection, SQL_DATASET_SPECS).search_logs(dataset="openstack", instance_id="vm-one", limit=2)
    assert result.total_corpus_match_count == total
    assert result.returned_record_count == min(rows, 2)
    assert result.truncated is truncated
    assert result.rows == connection.rows[:2]
    assert len(connection.calls) == 2
    assert "LIMIT" not in connection.calls[1][0]
    assert connection.calls[1][1] == {"instance_id": "vm-one"}


def test_optional_mcp_schema_validation_and_null_metadata(monkeypatch):
    connection = Connection()
    monkeypatch.setattr("log_insights.mcp_server.connection_factory", lambda _: lambda: connection)

    async def run():
        async with Client(build_server(Settings(None))) as client:
            tool = next(t for t in (await client.list_tools()).tools if t.name == "search_logs")
            assert set(tool.input_schema["required"]) == {"dataset"}
            for field in ("query", "request_id", "instance_id"):
                assert tool.input_schema["properties"][field]["default"] is None
            assert "Background request contexts" in tool.description
            for filters in ({"request_id": "req-one"}, {"instance_id": "vm-one"},
                            {"request_id": "req-one", "instance_id": "vm-one", "query": "Took"}):
                response = await client.call_tool("search_logs", dict(dataset="openstack", limit=2, **filters))
                assert not response.is_error
                data = response.structured_content
                assert data["total_corpus_match_count"] == 25 and data["truncated"] is True
                row = data["records"][0]
                assert METADATA <= row.keys()
                assert row["http_status"] is None and row["latency_s"] is None
                assert row["is_anomaly"] is True
                assert "LIMIT" not in data["provenance"]["executed_sql"]["total_count"]
            before = len(connection.calls)
            for args in ({"dataset": "bgl"}, {"dataset": "openstack"},
                         {"dataset": "bgl", "query": "core", "request_id": "req-one"},
                         {"dataset": "openstack", "request_id": ""}):
                assert (await client.call_tool("search_logs", args)).is_error
            assert len(connection.calls) == before
    anyio.run(run)
