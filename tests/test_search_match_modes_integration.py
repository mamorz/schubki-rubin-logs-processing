"""Opt-in SELECT-only checks on an existing full SQL corpus; no DDL or fixture writes."""

from datetime import datetime, timedelta
import os

import anyio
from mcp import Client
import pytest

from log_insights.config import Settings
from log_insights.db import connect
from log_insights.mcp_server import build_server
from log_insights.queries import QueryService, SQL_DATASET_SPECS

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_SQL_BACKEND_INTEGRATION_TESTS") != "1",
    reason="Requires an explicit SERENEDB_DSN for a preloaded SQL database; SELECT only",
)


@pytest.fixture
def live_settings():
    settings = Settings.from_env()
    settings.require_serenedb_dsn()  # Deliberately never load .env.
    return settings


@pytest.fixture
def service(live_settings):
    return QueryService(lambda: connect(live_settings), SQL_DATASET_SPECS)


def test_full_bgl_phrase_order_adjacency_and_normalization(service):
    any_result = service.search_logs(dataset="bgl", text="generating core", limit=1)
    phrase = service.search_logs(dataset="bgl", text="generating core", match_mode="phrase", limit=1)
    normalized = service.search_logs(dataset="bgl", text="GENERATING   CORE", match_mode="phrase", limit=2)
    reverse = service.search_logs(dataset="bgl", text="core generating", match_mode="phrase", limit=1)
    assert any_result.total_corpus_match_count > phrase.total_corpus_match_count > 2
    assert normalized.total_corpus_match_count == phrase.total_corpus_match_count
    assert phrase.returned_record_count == 1 and phrase.truncated
    assert reverse.total_corpus_match_count == 0 and not reverse.truncated
    # 'core' is an intervening indexed token; all matches but phrase must not.
    separated = service.search_logs(dataset="bgl", text="generating 304", match_mode="phrase", limit=1)
    unordered = service.search_logs(dataset="bgl", text="304 generating", match_mode="all", limit=1)
    assert separated.total_corpus_match_count == 0
    assert unordered.total_corpus_match_count > 0
    with_punctuation = service.search_logs(dataset="bgl", text="generating core.304", match_mode="phrase", limit=1)
    without_punctuation = service.search_logs(dataset="bgl", text="generating core 304", match_mode="phrase", limit=1)
    assert with_punctuation.total_corpus_match_count == without_punctuation.total_corpus_match_count > 0


@pytest.mark.parametrize("dataset,term", [("bgl", "generating core"), ("openstack", "instance")])
def test_modes_preserve_every_filter_and_full_count(live_settings, service, dataset, term):
    seed = service.search_logs(dataset=dataset, text=term, match_mode="phrase", limit=1).rows[0]
    args = dict(dataset=dataset, text=seed["message"], component=seed["component"],
                severity=seed["severity"], start_time=seed["timestamp"],
                end_time=seed["timestamp"] + timedelta(days=1))
    spec = SQL_DATASET_SPECS[dataset]
    for mode, predicate in (("any", "message @@ %s"), ("phrase", "message @@ ts_phrase(%s)"),
                            ("exact", "message = %s"), ("all", "message @@ plainto_tsquery(%s)")):
        small = service.search_logs(**args, match_mode=mode, limit=1)
        larger = service.search_logs(**args, match_mode=mode, limit=3)
        with connect(live_settings) as connection, connection.cursor() as cursor:
            # A separate raw-table JOIN reference keeps time filters off the index.
            # Both SQL corpora use line_no; OpenStack additionally uses source_file.
            join_key = "raw.line_no = matched.line_no"
            if dataset == "openstack":
                join_key += " AND raw.source_file = matched.source_file"
            if mode == "exact":
                source = f"{spec.table} raw WHERE {predicate}"
            else:
                source = (f"{spec.table} raw JOIN (SELECT * FROM {spec.index_relation} "
                          f"WHERE {predicate}) matched ON {join_key} WHERE TRUE")
            cursor.execute(f"SELECT COUNT(*) AS n FROM {source} "
                           "AND raw.component = %s AND raw.level = %s AND raw.ts >= %t AND raw.ts < %t",
                           (args["text"] if mode == "exact" else args["text"].strip(),
                            args["component"], args["severity"], args["start_time"], args["end_time"]))
            expected = cursor.fetchone()["n"]
        assert small.total_corpus_match_count == larger.total_corpus_match_count == expected
        assert expected > 0 and small.returned_record_count == 1
        assert small.truncated is (expected > 1)
        assert larger.truncated is (expected > 3)
        for row in larger.rows:
            assert row["component"] == args["component"] and row["severity"] == args["severity"]
            assert args["start_time"] <= row["timestamp"] < args["end_time"]
            if mode == "exact":
                assert row["message"] == args["text"]


@pytest.mark.parametrize("mode", ["any", "phrase", "exact", "all"])
def test_time_filtered_counts_match_python_filtered_candidates(live_settings, service, mode):
    # Regresses 26.09's index range bug independently of any SQL time predicate.
    # For phrase on this corpus: June 3 <= ts < July 3 is 92, not the index's 147.
    text = "304" if mode == "any" else "generating core.304"
    predicate = {"any": "message @@ %s", "phrase": "message @@ ts_phrase(%s)",
                 "exact": "message = %s", "all": "message @@ plainto_tsquery(%s)"}[mode]
    relation = "bgl_logs" if mode == "exact" else "bgl_logs_idx"
    with connect(live_settings) as connection, connection.cursor() as cursor:
        cursor.execute(f"SELECT line_no, ts, component, level FROM {relation} WHERE {predicate}", (text,))
        candidates = cursor.fetchmany(10_000)
        assert 0 < len(candidates) < 10_000
    first_time = min(row["ts"] for row in candidates)
    for start, end in ((datetime(2005, 6, 3), datetime(2005, 7, 3)),
                       (datetime(2005, 7, 3), None), (None, datetime(2005, 7, 3)),
                       (first_time, first_time + timedelta(microseconds=1))):
        expected = {row["line_no"] for row in candidates
                    if (start is None or row["ts"] >= start) and (end is None or row["ts"] < end)
                    and row["component"] == "KERNEL" and row["level"] == "INFO"}
        result = service.search_logs(dataset="bgl", text=text, match_mode=mode,
                                     start_time=start, end_time=end, component="KERNEL", severity="INFO", limit=3)
        assert result.total_corpus_match_count == len(expected)
        assert {row["record_id"] for row in result.rows} <= expected
        assert result.truncated is (len(expected) > 3)


def test_exact_is_case_and_whitespace_sensitive_and_not_substring(service):
    original = service.search_logs(dataset="bgl", text="generating core.304", match_mode="exact", limit=1)
    assert original.total_corpus_match_count > 0
    for text in ("generating core", "GENERATING CORE.304", " generating core.304", "generating core.304 ",
                 "generating core.304' OR 1=1 --"):
        result = service.search_logs(dataset="bgl", text=text, match_mode="exact", limit=1)
        assert result.total_corpus_match_count == 0
        assert result.rows == [] and not result.truncated


def test_new_mcp_handlers_against_existing_database_without_deployment(live_settings):
    async def run():
        # This test owns an in-process MCP server, not the deployed HTTP service.
        async with Client(build_server(live_settings, SQL_DATASET_SPECS)) as client:
            for mode in (None, "any", "phrase", "exact", "all"):
                args = dict(dataset="bgl", query="generating core.304", limit=1)
                if mode is not None:
                    args["match_mode"] = mode
                result = await client.call_tool("search_logs", args)
                assert not result.is_error
                payload = result.structured_content
                assert payload["match_mode"] == (mode or "any")
                assert payload["returned_record_count"] == 1
                assert payload["total_corpus_match_count"] > 1
                assert payload["truncated"] is True
                assert payload["provenance"]["parameters"]["total_count"] == {"text": args["query"]}
            filtered = await client.call_tool("search_logs", dict(
                dataset="bgl", query="generating core.304", match_mode="phrase", limit=2,
                start_time="2005-06-03T00:00:00", end_time="2005-07-03T00:00:00",
                component="KERNEL", severity="INFO",
            ))
            assert not filtered.is_error
            assert filtered.structured_content["total_corpus_match_count"] == 92
            assert filtered.structured_content["truncated"] is True
            params = filtered.structured_content["provenance"]["parameters"]["total_count"]
            assert params["start_time"] == "2005-06-03T00:00:00"
    anyio.run(run)
