"""Opt-in SELECT-only pattern aggregation checks against existing full corpora."""

from datetime import datetime, timedelta
import os
import socket
import threading

import anyio
import pytest
import uvicorn

from log_insights.config import Settings
from log_insights.db import connect
from log_insights.mcp_server import build_server
from log_insights.queries import QueryService, SQL_DATASET_SPECS
from scripts.verify_mcp_http import verify_endpoint

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_SQL_BACKEND_INTEGRATION_TESTS") != "1",
    reason="Requires explicit SERENEDB_DSN and preloaded SQL corpora; SELECT only",
)


@pytest.fixture
def settings():
    settings = Settings.from_env()  # Never load .env or run database setup.
    settings.require_serenedb_dsn()
    return settings


@pytest.fixture
def service(settings):
    return QueryService(lambda: connect(settings), SQL_DATASET_SPECS)


def reference_scope(spec, dataset, query, mode, filters):
    """Independent raw-table JOIN reference, not the production IN plan."""
    predicate = {
        "any": "message @@ %s", "all": "message @@ plainto_tsquery(%s)",
        "phrase": "message @@ ts_phrase(%s)", "exact": "raw.message = %s",
    }[mode]
    parameters = [query if mode == "exact" else query.strip()]
    if mode == "exact":
        source = f"{spec.table} raw WHERE {predicate}"
    else:
        key = "raw.line_no = hits.line_no"
        if dataset == "openstack":
            key += " AND raw.source_file = hits.source_file"
        source = (f"{spec.table} raw JOIN (SELECT * FROM {spec.index_relation} "
                  f"WHERE {predicate}) hits ON {key} WHERE TRUE")
    for field, column, operator in (
        ("component", "component", "="), ("severity", "level", "="),
        ("start_time", "ts", ">="), ("end_time", "ts", "<"),
    ):
        if filters.get(field) is not None:
            fmt = "%t" if field.endswith("time") else "%s"
            source += f" AND raw.{column} {operator} {fmt}"
            parameters.append(filters[field])
    return source, parameters


@pytest.mark.parametrize("dataset,term", [("bgl", "generating core.304"), ("openstack", "instance")])
@pytest.mark.parametrize("mode", ["any", "all", "phrase", "exact"])
def test_all_modes_filters_groups_and_totals_match_independent_reference(settings, service, dataset, term, mode):
    seed = service.search_logs(dataset=dataset, text=term, match_mode="phrase", limit=1).rows[0]
    query = seed["message"] if mode == "exact" else term
    filters = dict(component=seed["component"], severity=seed["severity"],
                   start_time=seed["timestamp"], end_time=seed["timestamp"] + timedelta(days=1))
    source, parameters = reference_scope(SQL_DATASET_SPECS[dataset], dataset, query, mode, filters)
    with connect(settings) as connection:
        expected = connection.execute(f"SELECT COUNT(*) AS n FROM {source}", parameters).fetchone()["n"]
        expected_groups = connection.execute(
            f"SELECT raw.component AS component, COUNT(*) AS record_count FROM {source} "
            "GROUP BY raw.component ORDER BY record_count DESC, component", parameters,
        ).fetchall()
    assert expected > 0
    for group in ("component", "severity", "event_id", "time", "team"):
        args = dict(dataset=dataset, query=query, match_mode=mode, group_by=group,
                    interval="day" if group == "time" else None, **filters)
        small = service.summarize_logs(**args, limit=1)
        complete = service.summarize_logs(**args, limit=100)
        assert small.total_matching_record_count == complete.total_matching_record_count == expected
        assert sum(g["record_count"] for g in complete.groups) == expected
        assert not complete.truncated
        assert small.groups == complete.groups[:1]
        assert small.truncated is (len(complete.groups) > 1)
        if group == "component":
            assert complete.groups == expected_groups
        if group == "team":
            assert complete.mapping_coverage["total_record_count"] == expected
            assert complete.mapping_coverage["mapped_record_count"] + complete.mapping_coverage["unmapped_record_count"] == expected
            assert complete.ownership_provenance["fictional"] is True
            assert complete.ownership_provenance["authoritative"] is False


@pytest.mark.parametrize("mode", ["any", "all", "phrase", "exact"])
def test_timestamp_regression_against_candidates_filtered_in_python(settings, service, mode):
    # No SQL timestamp predicate in the oracle: catches optimizer/index range
    # errors even if both production count and grouped query repeat the bug.
    query = "304" if mode == "any" else "generating core.304"
    predicate = {"any": "message @@ %s", "all": "message @@ plainto_tsquery(%s)",
                 "phrase": "message @@ ts_phrase(%s)", "exact": "message = %s"}[mode]
    relation = "bgl_logs" if mode == "exact" else "bgl_logs_idx"
    with connect(settings) as connection:
        cursor = connection.execute(f"SELECT line_no, ts, component, level FROM {relation} WHERE {predicate}", (query,))
        candidates = cursor.fetchmany(10_000)
        assert 0 < len(candidates) < 10_000
    first = min(r["ts"] for r in candidates)
    for start, end in ((datetime(2005, 6, 3), datetime(2005, 7, 3)),
                       (datetime(2005, 7, 3), None), (None, datetime(2005, 7, 3)),
                       (first, first + timedelta(microseconds=1))):
        expected = sum(1 for r in candidates if (start is None or r["ts"] >= start)
                       and (end is None or r["ts"] < end)
                       and r["component"] == "KERNEL" and r["level"] == "INFO")
        for group in ("time", "team"):
            result = service.summarize_logs(
                dataset="bgl", query=query, match_mode=mode, group_by=group,
                interval="day" if group == "time" else None, start_time=start, end_time=end,
                component="KERNEL", severity="INFO", limit=100,
            )
            assert result.total_matching_record_count == expected
            assert sum(g["record_count"] for g in result.groups) == expected
            assert not result.truncated
            if result.mapping_coverage:
                assert result.mapping_coverage["total_record_count"] == expected
            if mode == "phrase" and start == datetime(2005, 6, 3) and end == datetime(2005, 7, 3):
                assert expected == 92  # Incorrect index-only plan returned 147.


@pytest.mark.parametrize("dataset,term", [("bgl", "data storage interrupt"), ("openstack", "instance")])
def test_full_pattern_scope_reconciles_and_limit_reports_omitted_groups(service, dataset, term):
    expected = service.search_logs(dataset=dataset, text=term, match_mode="phrase", limit=1).total_corpus_match_count
    assert expected > 1
    for group, interval in (("component", None), ("severity", None), ("event_id", None),
                            ("team", None), ("time", "day")):
        result = service.summarize_logs(dataset=dataset, query=term, match_mode="phrase",
                                        group_by=group, interval=interval, limit=100)
        small = service.summarize_logs(dataset=dataset, query=term, match_mode="phrase",
                                       group_by=group, interval=interval, limit=1)
        assert small.total_matching_record_count == result.total_matching_record_count == expected
        assert not result.truncated
        assert sum(g["record_count"] for g in result.groups) == expected
        assert small.truncated is (len(result.groups) > 1)
        assert small.groups == result.groups[:1]


@pytest.mark.parametrize("dataset", ["bgl", "openstack"])
def test_omitted_query_and_empty_scopes(service, dataset):
    old = service.summarize_logs(dataset=dataset, group_by="severity", limit=100)
    omitted = service.summarize_logs(dataset=dataset, group_by="severity", query=None, limit=100)
    assert old == omitted
    assert sum(g["record_count"] for g in old.groups) == old.total_matching_record_count
    for mode in ("any", "all", "phrase", "exact"):
        for group in ("component", "team"):
            missing = service.summarize_logs(dataset=dataset, group_by=group,
                                              query="rubinnomatchsentinelzqx991827", match_mode=mode)
            assert missing.total_matching_record_count == missing.returned_group_count == 0
            assert missing.groups == [] and not missing.truncated
            if group == "team":
                assert missing.mapping_coverage["total_record_count"] == 0


def test_exact_equality_and_injection_resistance(settings, service):
    exact = service.summarize_logs(dataset="bgl", group_by="component", query="generating core.304", match_mode="exact")
    assert exact.total_matching_record_count == 164
    for query in ("generating core", "GENERATING CORE.304", " generating core.304", "generating core.304 ",
                  "generating core.304' OR 1=1 --"):
        empty = service.summarize_logs(dataset="bgl", group_by="component", query=query, match_mode="exact")
        assert empty.total_matching_record_count == 0 and empty.groups == []
    for query in ("", " "):
        with connect(settings) as connection:
            expected = connection.execute("SELECT COUNT(*) AS n FROM bgl_logs WHERE message = %s", (query,)).fetchone()["n"]
        result = service.summarize_logs(dataset="bgl", group_by="component", query=query, match_mode="exact", limit=100)
        assert result.total_matching_record_count == expected
        assert sum(g["record_count"] for g in result.groups) == expected
    for field in ("component", "severity"):
        empty = service.summarize_logs(dataset="bgl", group_by="team", query="generating core.304",
                                       match_mode="phrase", **{field: "' OR 1=1 --"})
        assert empty.total_matching_record_count == 0


def test_phrase_order_adjacency_and_dictionary_normalization(service):
    def count(query, mode="phrase"):
        return service.summarize_logs(dataset="bgl", group_by="component", query=query,
                                      match_mode=mode).total_matching_record_count
    assert count("generating core.304") == count("GENERATING   CORE 304") == 164
    assert count("core generating") == count("generating 304") == 0
    assert count("304 generating", "all") > 0
    assert count("generating core", "any") > count("generating core") > 0


@pytest.mark.parametrize("mode", ["auto", "legacy"])
def test_new_code_over_real_http_without_redeploying(settings, mode):
    # Temporary local Python server running current code; existing containers
    # stay untouched. All database activity is SELECT-only.
    app = build_server(settings, SQL_DATASET_SPECS).streamable_http_app(host="127.0.0.1")
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
                report = await verify_endpoint(url, mode=mode, patterns=True)
                assert report["pattern_aggregation"]["timestamp_regression_count"] == 92
            anyio.run(run)
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            assert not thread.is_alive()


@pytest.mark.parametrize("interval,value,expected", [
    ("week", datetime(2021, 1, 1, 23, 59, 59), datetime(2020, 12, 28)),
    ("week", datetime(2021, 1, 3, 23, 59, 59, 999999), datetime(2020, 12, 28)),
    ("week", datetime(2021, 1, 4), datetime(2021, 1, 4)),
    ("month", datetime(2024, 2, 29, 23, 59, 59, 999999), datetime(2024, 2, 1)),
    ("month", datetime(2024, 3, 1), datetime(2024, 3, 1)),
    ("month", datetime(2025, 1, 1), datetime(2025, 1, 1)),
])
def test_calendar_boundaries_on_serenedb_without_fixture_writes(settings, interval, value, expected):
    with connect(settings) as connection:
        # interval is a test-owned constant; timestamp remains parameterized.
        row = connection.execute(f"SELECT date_trunc('{interval}', CAST(%(value)t AS TIMESTAMP)) AS bucket", {"value": value}).fetchone()
    assert row["bucket"] == expected


@pytest.mark.parametrize("dataset,term", [("bgl", "data storage interrupt"), ("openstack", "instance")])
@pytest.mark.parametrize("interval", ["week", "month"])
def test_newest_period_retained_when_limit_truncates(settings, service, dataset, term, interval):
    args = dict(dataset=dataset, query=term, match_mode="phrase", group_by="time", interval=interval)
    complete = service.summarize_logs(**args, limit=100)
    latest = service.summarize_logs(**args, limit=1, sort_direction="desc")
    earliest = service.summarize_logs(**args, limit=1)
    reverse = service.summarize_logs(**args, limit=100, sort_direction="desc")
    assert not complete.truncated and not reverse.truncated
    if dataset == "bgl":
        assert len(complete.groups) > 1
    assert reverse.groups == list(reversed(complete.groups))
    assert latest.groups == complete.groups[-1:]
    assert earliest.groups == complete.groups[:1]
    assert latest.truncated is (len(complete.groups) > 1)
    assert earliest.truncated is (len(complete.groups) > 1)
    assert sum(g["record_count"] for g in complete.groups) == complete.total_matching_record_count
    assert latest.total_matching_record_count == earliest.total_matching_record_count == complete.total_matching_record_count
    # Independently find the last matching timestamp through the index (no
    # timestamp predicate). This proves the descending limit keeps newest data.
    spec = SQL_DATASET_SPECS[dataset]
    with connect(settings) as connection:
        row = connection.execute(
            f"SELECT date_trunc('{interval}', MAX(ts)) AS newest FROM {spec.index_relation} "
            "WHERE message @@ ts_phrase(%s)", (term,),
        ).fetchone()
    assert latest.groups[0]["bucket_start"] == row["newest"]


@pytest.mark.parametrize("dataset", ["bgl", "openstack"])
@pytest.mark.parametrize("interval", ["week", "month"])
def test_calendar_groups_without_query_keep_complete_corpus(service, dataset, interval):
    result = service.summarize_logs(dataset=dataset, group_by="time", interval=interval, limit=100)
    assert not result.truncated
    assert result.total_matching_record_count == (4_747_963 if dataset == "bgl" else 207_820)
    assert sum(g["record_count"] for g in result.groups) == result.total_matching_record_count
    assert [g["bucket_start"] for g in result.groups] == sorted(g["bucket_start"] for g in result.groups)
