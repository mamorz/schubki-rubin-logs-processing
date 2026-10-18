"""Search mode SQL, parameterization, counts, and MCP schema/response contracts."""

from datetime import datetime

import anyio
from mcp import Client
import pytest

from log_insights.config import Settings
from log_insights.mcp_server import build_server
from log_insights.queries import (
    DATASET_SPECS, SQL_DATASET_SPECS, QueryService, build_search_query_plan,
)


@pytest.mark.parametrize("specs", [DATASET_SPECS, SQL_DATASET_SPECS])
@pytest.mark.parametrize("dataset", ["bgl", "openstack"])
@pytest.mark.parametrize("mode,predicate", [
    ("any", "message @@ %(text)s"),
    ("phrase", "message @@ ts_phrase(%(text)s)"),
    ("exact", "message = %(text)s"),
    ("all", "message @@ plainto_tsquery(%(text)s)"),
])
def test_modes_keep_filters_parameterized_and_count_unlimited(specs, dataset, mode, predicate):
    text = "  core'; SELECT 1; -- %_  "
    component = "KERNEL' OR 1=1 --"
    severity = "ERROR' OR 1=1 --"
    start, end = datetime(2005, 6, 3), datetime(2005, 6, 4)
    plan = build_search_query_plan(
        dataset=dataset, text=text, match_mode=mode, component=component,
        severity=severity, start_time=start, end_time=end, limit=2, _dataset_specs=specs,
    )
    spec = specs[dataset]
    relation = spec.table  # Time ranges are applied to raw candidate rows.
    assert plan.match_mode == plan.applied_filters["match_mode"] == mode
    for sql in (plan.sql, plan.count_sql):
        assert f"FROM {relation}\n" in sql
        assert predicate in sql
        assert f"{spec.time_column} >= %(start_time)t" in sql
        assert f"{spec.time_column} < %(end_time)t" in sql
        assert "component = %(component)s" in sql
        assert f"{spec.severity_column} = %(severity)s" in sql
        assert text.strip() not in sql and component not in sql and severity not in sql
        if mode != "exact":
            assert f"IN (SELECT {spec.id_expression} FROM {spec.index_relation} WHERE {predicate})" in sql
    assert plan.count_parameters == {
        "text": text if mode == "exact" else text.strip(), "component": component,
        "severity": severity, "start_time": start, "end_time": end,
    }
    assert plan.parameters == {**plan.count_parameters, "fetch_limit": 3}
    assert "LIMIT" not in plan.count_sql
    assert "fetch_limit" not in plan.count_parameters
    assert f"ORDER BY {spec.time_column}, {spec.id_expression}" in plan.sql


def test_omitted_mode_preserves_any_sql_and_text_normalization():
    args = {"dataset": "bgl", "text": "  generating core  "}
    assert build_search_query_plan(**args) == build_search_query_plan(**args, match_mode="any")
    assert build_search_query_plan(**args).parameters["text"] == "generating core"


@pytest.mark.parametrize("mode", ["", "approximate", "phrase'; SELECT 1; --", None, True, []])
def test_invalid_mode_rejected_before_database_access(mode):
    def never_connect():
        pytest.fail("invalid match mode reached the database")
    with pytest.raises(ValueError, match="match_mode"):
        QueryService(never_connect).search_logs(dataset="bgl", text="core", match_mode=mode)


@pytest.mark.parametrize("text", ["", " ", "\t\n", "  Core.304\t  ", "a' OR 1=1 --"])
def test_exact_preserves_every_character_including_empty_message(text):
    plan = build_search_query_plan(dataset="bgl", text=text, match_mode="exact")
    assert plan.parameters["text"] == plan.count_parameters["text"] == text
    assert plan.applied_filters["full_text_query"] == text


@pytest.mark.parametrize("mode", ["any", "phrase", "exact", "all"])
@pytest.mark.parametrize("text", [None, 7, "a\x00b"])
def test_all_modes_reject_non_strings_and_nul(mode, text):
    with pytest.raises(ValueError):
        build_search_query_plan(dataset="bgl", text=text, match_mode=mode)


class RecordingConnection:
    def __init__(self, total=123, rows=3):
        self.calls = []
        self.total = total
        self.rows = [dict(record_id=n, event_id=None, timestamp=datetime(2005, 6, 3),
                          component="KERNEL", severity="INFO", message="generating core.304")
                     for n in range(rows)]

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
        return self.rows

    def fetchone(self):
        return {"total_corpus_match_count": self.total}


@pytest.mark.parametrize("mode", ["any", "phrase", "exact", "all"])
@pytest.mark.parametrize("total,rows,truncated", [(123, 3, True), (2, 2, False), (0, 0, False)])
def test_service_counts_full_scope_and_reports_mode(mode, total, rows, truncated):
    connection = RecordingConnection(total=total, rows=rows)
    service = QueryService(lambda: connection, SQL_DATASET_SPECS)
    result = service.search_logs(dataset="bgl", text="generating core.304", match_mode=mode, limit=2)
    assert result.match_mode == mode
    assert result.total_corpus_match_count == total
    assert result.returned_record_count == min(rows, 2)
    assert result.truncated is truncated
    assert len(connection.calls) == 2
    assert "LIMIT" not in connection.calls[1][0]
    assert "fetch_limit" not in connection.calls[1][1]


def test_mcp_optional_enum_description_and_mode_response(monkeypatch):
    connection = RecordingConnection()
    monkeypatch.setattr("log_insights.mcp_server.connection_factory", lambda _: lambda: connection)

    async def run():
        async with Client(build_server(Settings(None))) as client:
            search = next(t for t in (await client.list_tools()).tools if t.name == "search_logs")
            assert set(search.input_schema["required"]) == {"dataset"}
            schema = search.input_schema["properties"]["match_mode"]
            assert schema["default"] == "any"
            assert set(schema["enum"]) == {"any", "phrase", "exact", "all"}
            assert "consecutive tokens" in search.description
            assert "ENTIRE stored raw message" in search.description
            for mode in (None, "any", "phrase", "exact", "all"):
                args = dict(dataset="bgl", query="generating core.304", limit=2)
                if mode is not None:
                    args["match_mode"] = mode
                response = await client.call_tool("search_logs", args)
                assert not response.is_error
                data = response.structured_content
                assert data["match_mode"] == (mode or "any")
                assert data["returned_record_count"] == 2
                assert data["total_corpus_match_count"] == 123
                assert data["truncated"] is True
                provenance = data["provenance"]
                assert provenance["applied_filters"]["match_mode"] == (mode or "any")
                assert provenance["parameters"]["total_count"] == {"text": "generating core.304"}
                assert "LIMIT" not in provenance["executed_sql"]["total_count"]
            before = len(connection.calls)
            invalid = await client.call_tool("search_logs", dict(dataset="bgl", query="core", match_mode="fuzzy"))
            assert invalid.is_error
            assert len(connection.calls) == before

    anyio.run(run)
