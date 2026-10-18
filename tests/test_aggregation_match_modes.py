"""Pattern aggregation SQL, complete counts, validation and MCP contracts."""

from datetime import datetime

import anyio
from mcp import Client
import pytest

from log_insights.config import Settings
from log_insights.mcp_server import build_server
from log_insights.queries import (
    DATASET_SPECS, SQL_DATASET_SPECS, SEARCH_PREDICATES, QueryService,
    build_aggregation_query_plan,
)


@pytest.mark.parametrize("specs", [DATASET_SPECS, SQL_DATASET_SPECS])
@pytest.mark.parametrize("dataset", ["bgl", "openstack"])
@pytest.mark.parametrize("mode", ["any", "all", "phrase", "exact"])
@pytest.mark.parametrize("group", ["component", "severity", "event_id", "time", "team"])
def test_patterns_and_all_filters_share_complete_scope(specs, dataset, mode, group):
    query = "  core'; SELECT 1; -- %_  "
    component, severity = "KERNEL' OR 1=1 --", "INFO' OR 1=1 --"
    start, end = datetime(2005, 6, 3), datetime(2005, 7, 3)
    plan = build_aggregation_query_plan(
        dataset=dataset, group_by=group, interval="day" if group == "time" else None,
        query=query, match_mode=mode, start_time=start, end_time=end,
        component=component, severity=severity, limit=1, _dataset_specs=specs,
    )
    spec = specs[dataset]
    statements = [plan.sql, plan.count_sql]
    if group == "team":
        statements.append(plan.coverage_sql)
        assert plan.coverage_parameters == {**plan.count_parameters, "ownership_dataset": dataset}
        assert "LIMIT %(fetch_limit)t" in plan.sql
    for sql in statements:
        assert f"FROM {spec.table}\n" in sql
        assert SEARCH_PREDICATES[mode] in sql
        assert f"{spec.time_column} >= %(start_time)t" in sql
        assert f"{spec.time_column} < %(end_time)t" in sql
        assert "component = %(component)s" in sql
        assert f"{spec.severity_column} = %(severity)s" in sql
        assert query.strip() not in sql and component not in sql and severity not in sql
        if mode == "exact":
            assert spec.index_relation not in sql
        else:
            assert (f"{spec.id_expression} IN (SELECT {spec.id_expression} "
                    f"FROM {spec.index_relation} WHERE {SEARCH_PREDICATES[mode]})") in sql
            # No timestamp predicate may appear inside the index subquery.
            assert f"{SEARCH_PREDICATES[mode]}) AND\n" in sql
    assert plan.count_parameters == dict(
        text=query if mode == "exact" else query.strip(), component=component,
        severity=severity, start_time=start, end_time=end,
    )
    assert plan.parameters["fetch_limit"] == 2
    assert "LIMIT" not in plan.count_sql
    assert "fetch_limit" not in plan.count_parameters
    if plan.coverage_sql:
        assert "LIMIT" not in plan.coverage_sql
        assert "fetch_limit" not in plan.coverage_parameters
    assert plan.applied_filters["full_text_query"] == plan.count_parameters["text"]
    assert plan.applied_filters["match_mode"] == mode


@pytest.mark.parametrize("mode", ["any", "all", "phrase", "exact"])
@pytest.mark.parametrize("group", ["component", "time", "team"])
def test_omitted_query_preserves_existing_sql_parameters_and_filters(mode, group):
    args = dict(dataset="bgl", group_by=group, interval="hour" if group == "time" else None,
                start_time=datetime(2005, 6, 3), severity="INFO")
    old = build_aggregation_query_plan(**args)
    assert old == build_aggregation_query_plan(**args, query=None, match_mode=mode)
    assert "text" not in old.count_parameters
    assert "full_text_query" not in old.applied_filters
    assert "match_mode" not in old.applied_filters
    assert "_idx" not in old.sql
    assert "%(start_time)s" in old.sql


@pytest.mark.parametrize("query", ["", " ", "\t\n", "  Core.'%_\t  "])
def test_exact_preserves_all_message_characters(query):
    plan = build_aggregation_query_plan(dataset="bgl", group_by="team", query=query, match_mode="exact")
    assert plan.count_parameters["text"] == query
    assert plan.coverage_parameters["text"] == query


@pytest.mark.parametrize("mode", ["any", "all", "phrase"])
@pytest.mark.parametrize("query", ["", "  ", "\t\n"])
def test_blank_token_query_rejected(mode, query):
    with pytest.raises(ValueError):
        build_aggregation_query_plan(dataset="bgl", group_by="component", query=query, match_mode=mode)


@pytest.mark.parametrize("args", [
    {"match_mode": "fuzzy"}, {"match_mode": "any'; SELECT 1; --"},
    {"match_mode": None}, {"match_mode": True}, {"match_mode": []},
    {"query": 4}, {"query": True}, {"query": "a\x00b"},
])
def test_invalid_pattern_rejected_without_database_access(args):
    def never_connect():
        pytest.fail("invalid input reached database")
    with pytest.raises(ValueError):
        QueryService(never_connect).summarize_logs(dataset="bgl", group_by="component", **args)


class AggregationConnection:
    def __init__(self, groups, total):
        self.groups, self.total, self.calls = groups, total, []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def cursor(self):
        return self

    def execute(self, sql, parameters):
        assert sql.startswith("SELECT")
        self.calls.append((sql, parameters))

    def fetchall(self):
        return self.groups

    def fetchone(self):
        return {"total_matching_record_count": self.total}


@pytest.mark.parametrize("mode", ["any", "all", "phrase", "exact"])
@pytest.mark.parametrize("groups,total,truncated", [
    ([{"component": "A", "record_count": 80}, {"component": "B", "record_count": 43}], 123, True),
    ([{"component": "A", "record_count": 80}], 80, False), ([], 0, False),
])
def test_limit_only_bounds_groups_and_never_full_count(mode, groups, total, truncated):
    connection = AggregationConnection(groups, total)
    result = QueryService(lambda: connection).summarize_logs(
        dataset="bgl", group_by="component", query="core", match_mode=mode, limit=1,
    )
    assert result.total_matching_record_count == total
    assert result.groups == groups[:1]
    assert result.returned_group_count == min(len(groups), 1)
    assert result.truncated is truncated
    assert len(connection.calls) == 2
    assert "LIMIT" not in connection.calls[1][0]
    assert "fetch_limit" not in connection.calls[1][1]


def test_mcp_optional_schema_and_functionality(monkeypatch):
    connection = AggregationConnection([{"component": "KERNEL", "record_count": 123}], 123)
    monkeypatch.setattr("log_insights.mcp_server.connection_factory", lambda _: lambda: connection)

    async def run():
        async with Client(build_server(Settings(None))) as client:
            tool = next(t for t in (await client.list_tools()).tools if t.name == "summarize_logs")
            assert set(tool.input_schema["required"]) == {"dataset", "group_by"}
            properties = tool.input_schema["properties"]
            assert properties["query"]["default"] is None
            assert properties["match_mode"]["default"] == "any"
            assert set(properties["match_mode"]["enum"]) == {"any", "all", "phrase", "exact"}
            assert properties["sort_direction"]["default"] == "asc"
            assert set(properties["sort_direction"]["enum"]) == {"asc", "desc"}
            assert "ENTIRE stored raw message" in tool.description
            for mode in (None, "any", "all", "phrase", "exact"):
                args = dict(dataset="bgl", group_by="component", query="core", limit=1)
                if mode is not None:
                    args["match_mode"] = mode
                response = await client.call_tool("summarize_logs", args)
                assert not response.is_error
                data = response.structured_content
                assert data["total_matching_record_count"] == 123
                assert data["groups"] == connection.groups
                assert data["returned_group_count"] == 1
                assert data["applied_filters"]["match_mode"] == (mode or "any")
                assert data["provenance"]["parameters"]["total_count"] == {"text": "core"}
            old = await client.call_tool("summarize_logs", dict(dataset="bgl", group_by="component"))
            assert not old.is_error
            assert "full_text_query" not in old.structured_content["applied_filters"]
            before = len(connection.calls)
            for args in ({"match_mode": "fuzzy"}, {"query": " "}):
                invalid = await client.call_tool("summarize_logs", dict(dataset="bgl", group_by="component", **args))
                assert invalid.is_error
            assert len(connection.calls) == before

    anyio.run(run)


@pytest.mark.parametrize("interval", ["hour", "day", "week", "month"])
def test_time_direction_is_allowlisted_and_default_sql_is_preserved(interval):
    args = dict(dataset="bgl", group_by="time", interval=interval, query="core", match_mode="phrase")
    default = build_aggregation_query_plan(**args)
    assert default == build_aggregation_query_plan(**args, sort_direction="asc")
    recent = build_aggregation_query_plan(**args, sort_direction="desc")
    assert "ORDER BY bucket_start DESC" in recent.sql
    assert recent.applied_filters["sort_direction"] == "desc"
    assert default.count_sql == recent.count_sql
    assert default.count_parameters == recent.count_parameters


@pytest.mark.parametrize("direction", [None, True, "", "latest", "desc; SELECT 1; --"])
def test_invalid_direction_is_rejected(direction):
    with pytest.raises(ValueError, match="sort_direction"):
        build_aggregation_query_plan(dataset="bgl", group_by="time", interval="day", sort_direction=direction)


def test_descending_direction_rejected_for_non_time_groups():
    with pytest.raises(ValueError, match="only valid"):
        build_aggregation_query_plan(dataset="bgl", group_by="team", sort_direction="desc")
