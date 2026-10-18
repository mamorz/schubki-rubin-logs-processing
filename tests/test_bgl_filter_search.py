"""Additive BGL filter-only validation, parameterization and MCP contracts."""

from datetime import datetime, timezone

import anyio
from mcp import Client
import pytest

from log_insights.config import Settings
from log_insights.mcp_server import build_server
from log_insights.queries import DATASET_SPECS, SQL_DATASET_SPECS, QueryService, build_search_query_plan

START, END = datetime(2005, 9, 20, 12), datetime(2005, 9, 20, 13)
FILTERS = [dict(component="MMCS"), dict(severity="ERROR"), dict(component="MMCS", severity="ERROR")]


@pytest.mark.parametrize("specs", [DATASET_SPECS, SQL_DATASET_SPECS])
@pytest.mark.parametrize("filters", FILTERS)
@pytest.mark.parametrize("mode", ["any", "all", "phrase", "exact"])
def test_filter_only_scope_uses_base_table_and_exact_unlimited_count(specs, filters, mode):
    plan = build_search_query_plan(dataset="bgl", start_time=START, end_time=END,
                                   match_mode=mode, limit=2, _dataset_specs=specs, **filters)
    spec = specs["bgl"]
    for sql in (plan.sql, plan.count_sql):
        assert f"FROM {spec.table}\n" in sql
        assert spec.index_relation not in sql
        assert "message @@" not in sql and "message =" not in sql
        assert "%(text)" not in sql
        assert f"{spec.time_column} >= %(start_time)t" in sql
        assert f"{spec.time_column} < %(end_time)t" in sql
        for name in filters:
            column = spec.severity_column if name == "severity" else name
            assert f"{column} = %({name})s" in sql
    assert plan.count_parameters == dict(start_time=START, end_time=END, **filters)
    assert plan.parameters == dict(plan.count_parameters, fetch_limit=3)
    assert "LIMIT" not in plan.count_sql
    assert f"ORDER BY {spec.time_column}, {spec.id_expression}" in plan.sql
    assert plan.applied_filters["full_text_query"] is None
    assert plan.match_mode == plan.applied_filters["match_mode"] == mode


@pytest.mark.parametrize("options", [
    {}, dict(component="MMCS"), dict(severity="ERROR"), dict(component="MMCS", severity="ERROR"),
    dict(start_time=START, end_time=END),
    dict(component="MMCS", start_time=START), dict(component="MMCS", end_time=END),
    dict(severity="ERROR", start_time=START), dict(severity="ERROR", end_time=END),
    dict(component="MMCS", severity="ERROR", start_time=START),
])
def test_overly_broad_filter_only_search_never_connects(options):
    with pytest.raises(ValueError, match="query is required.*both start_time.*component or severity"):
        QueryService(lambda: pytest.fail("broad search connected"), SQL_DATASET_SPECS).search_logs(dataset="bgl", **options)


@pytest.mark.parametrize("overrides,expected", [
    (dict(component=" "), "component"), (dict(severity=""), "severity"),
    (dict(component="x\x00y"), "component"), (dict(severity="x\x00y"), "severity"),
    (dict(start_time=END), "earlier than"), (dict(end_time=START), "earlier than"),
    (dict(start_time=START.replace(tzinfo=timezone.utc)), "timezone"),
    (dict(start_time="2005-09-20"), "datetime"),
    (dict(limit=51), "limit"), (dict(limit=True), "limit"),
    (dict(match_mode="fuzzy"), "match_mode"),
    (dict(request_id="req-one"), "BGL and sample"),
])
def test_invalid_filter_only_parameters_never_connect(overrides, expected):
    options = dict(start_time=START, end_time=END, component="MMCS", severity="ERROR")
    options.update(overrides)
    with pytest.raises(ValueError, match=expected):
        QueryService(lambda: pytest.fail("invalid search connected"), SQL_DATASET_SPECS).search_logs(dataset="bgl", **options)


@pytest.mark.parametrize("mode", ["any", "all", "phrase"])
@pytest.mark.parametrize("text", ["", " ", "\t\n"])
def test_explicit_blank_query_never_becomes_filter_only(mode, text):
    with pytest.raises(ValueError, match="must not be empty"):
        build_search_query_plan(dataset="bgl", text=text, match_mode=mode,
                                start_time=START, end_time=END, component="MMCS")


def test_explicit_empty_exact_query_still_filters_empty_messages():
    plan = build_search_query_plan(dataset="bgl", text="", match_mode="exact",
                                   start_time=START, end_time=END, component="MMCS")
    assert "message = %(text)s" in plan.sql
    assert plan.count_parameters["text"] == ""


def test_filter_values_are_bound_and_never_become_sql():
    filters = dict(component="MMCS' OR TRUE --", severity="ERROR'; DROP TABLE bgl_logs; --")
    plan = build_search_query_plan(dataset="bgl", start_time=START, end_time=END,
                                   _dataset_specs=SQL_DATASET_SPECS, **filters)
    for key, value in filters.items():
        assert value not in plan.sql and value not in plan.count_sql
        assert plan.count_parameters[key] == value


class RecordingConnection:
    def __init__(self):
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def cursor(self):
        return self

    def execute(self, sql, params):
        self.calls.append((sql, params))

    def fetchall(self):
        return [dict(record_id=i, event_id=None, timestamp=START, component="MMCS", severity="ERROR", message="")
                for i in (1, 2, 3)]

    def fetchone(self):
        return dict(total_corpus_match_count=9)


def test_mcp_schema_unchanged_and_omitted_null_queries_equivalent(monkeypatch):
    connection = RecordingConnection()
    monkeypatch.setattr("log_insights.mcp_server.connection_factory", lambda _: lambda: connection)

    async def run():
        async with Client(build_server(Settings(None))) as client:
            catalog = (await client.list_tools()).tools
            assert len(catalog) == 6
            tool = next(t for t in catalog if t.name == "search_logs")
            assert tool.input_schema["required"] == ["dataset"]
            properties = tool.input_schema["properties"]
            assert set(properties) == {"dataset", "query", "start_time", "end_time", "component", "severity",
                                       "limit", "match_mode", "request_id", "instance_id"}
            assert properties["query"]["default"] is properties["severity"]["default"] is None
            assert properties["limit"]["default"] == 20
            assert properties["match_mode"]["default"] == "any"
            assert "BGL filter-only" in tool.description
            args = dict(dataset="bgl", component="MMCS", severity="ERROR",
                        start_time=START.isoformat(), end_time=END.isoformat(), limit=2)
            omitted = await client.call_tool("search_logs", args)
            explicit_null = await client.call_tool("search_logs", dict(args, query=None))
            assert not omitted.is_error and not explicit_null.is_error
            assert omitted.structured_content == explicit_null.structured_content
            data = omitted.structured_content
            assert set(data) == {"match_mode", "records", "returned_record_count", "total_corpus_match_count",
                                 "truncated", "provenance"}
            assert data["total_corpus_match_count"] == 9
            assert data["returned_record_count"] == 2 and data["truncated"]
            assert data["records"][0]["message"] == ""
            assert data["provenance"]["applied_filters"]["full_text_query"] is None
            before = len(connection.calls)
            for overrides in (dict(start_time=None), dict(end_time=None), dict(query=" "),
                              dict(start_time="2005-09-20T12:00:00Z")):
                result = await client.call_tool("search_logs", dict(args, **overrides))
                assert result.is_error
            assert len(connection.calls) == before

    anyio.run(run)
