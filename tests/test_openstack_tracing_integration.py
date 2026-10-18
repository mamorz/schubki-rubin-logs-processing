"""SELECT-only checks of structured tracing on the existing SQL corpus."""

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
from scripts.verify_openstack_tracing import INSTANCE_ID, REQUEST_ID, run_research_queries

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_SQL_BACKEND_INTEGRATION_TESTS") != "1",
    reason="Requires explicit SERENEDB_DSN and existing SQL corpora; SELECT only",
)


@pytest.fixture
def settings():
    settings = Settings.from_env()
    settings.require_serenedb_dsn()
    return settings


@pytest.fixture
def service(settings):
    return QueryService(lambda: connect(settings), SQL_DATASET_SPECS)


@pytest.mark.parametrize("filters,expected", [
    ({"request_id": REQUEST_ID}, 12), ({"instance_id": INSTANCE_ID}, 25),
    ({"request_id": REQUEST_ID, "instance_id": INSTANCE_ID}, 11),
])
def test_structured_rows_metadata_and_complete_counts(settings, service, filters, expected):
    # Independently read bounded scenario rows from the raw table. Full results
    # fit within 50; compare each metadata field and deterministic chronology.
    where = " AND ".join(f"{field} = %({field})s" for field in filters)
    with connect(settings) as connection:
        rows = connection.execute(
            "SELECT source_file, line_no, ts, component, level, message, request_id, "
            "instance_id, service, http_status, latency_s, is_anomaly "
            f"FROM openstack_logs WHERE {where} ORDER BY ts, source_file, line_no", filters,
        ).fetchall()
    assert len(rows) == expected
    for limit in (1, expected, 50):
        result = service.search_logs(dataset="openstack", **filters, limit=limit)
        assert result.total_corpus_match_count == expected
        assert result.returned_record_count == min(limit, expected)
        assert result.truncated is (expected > limit)
        for actual, raw in zip(result.rows, rows[:limit]):
            ordinal = {"openstack_abnormal": 1, "openstack_normal1": 2, "openstack_normal2": 3}[raw["source_file"]]
            assert actual["record_id"] == ordinal * 1_000_000_000 + raw["line_no"]
            assert actual["event_id"] is None
            assert actual["timestamp"] == raw["ts"]
            assert actual["severity"] == raw["level"]
            for field in ("component", "message", "request_id", "instance_id", "service", "http_status", "latency_s", "is_anomaly"):
                assert actual[field] == raw[field]
    if "instance_id" not in filters:
        api = next(r for r in rows if r["service"] == "nova-api")
        assert api["instance_id"] is None
        assert api["http_status"] == 202 and round(api["latency_s"], 3) == 0.479
        assert api["is_anomaly"] is False
    compute = next(r for r in rows if r["service"] == "nova-compute")
    assert compute["latency_s"] is None and compute["http_status"] is None


@pytest.mark.parametrize("mode", ["any", "all", "phrase", "exact"])
def test_text_and_identifiers_and_existing_filters(settings, service, mode):
    query = "Took 41.81 seconds to spawn the instance on the hypervisor."
    if mode == "exact":
        # The raw message includes its [instance: ...] prefix. Exact must copy
        # the stored value, not the shortened text in the research narrative.
        with connect(settings) as connection:
            query = connection.execute(
                "SELECT message FROM openstack_logs WHERE request_id = %s AND message LIKE %s",
                (REQUEST_ID, "%Took 41.81 seconds to spawn%"),
            ).fetchone()["message"]
    filters = dict(request_id=REQUEST_ID, instance_id=INSTANCE_ID, component="nova.compute.manager",
                   severity="INFO", start_time=datetime(2017, 5, 14, 21, 7), end_time=datetime(2017, 5, 14, 21, 9))
    predicate = {"any": "message @@ %(text)s", "all": "message @@ plainto_tsquery(%(text)s)",
                 "phrase": "message @@ ts_phrase(%(text)s)", "exact": "raw.message = %(text)s"}[mode]
    if mode == "exact":
        source, first = "openstack_logs raw", predicate
    else:
        source = (f"openstack_logs raw JOIN (SELECT source_file, line_no FROM openstack_logs_idx WHERE {predicate}) hits "
                  "ON raw.source_file = hits.source_file AND raw.line_no = hits.line_no")
        first = "TRUE"
    with connect(settings) as connection:
        expected = connection.execute(
            f"SELECT COUNT(*) AS n FROM {source} WHERE {first} "
            "AND raw.request_id = %(request_id)s AND raw.instance_id = %(instance_id)s "
            "AND raw.component = %(component)s AND raw.level = %(severity)s "
            "AND raw.ts >= %(start_time)t AND raw.ts < %(end_time)t", dict(filters, text=query),
        ).fetchone()["n"]
    small = service.search_logs(dataset="openstack", text=query, match_mode=mode, **filters, limit=1)
    large = service.search_logs(dataset="openstack", text=query, match_mode=mode, **filters, limit=50)
    assert small.total_corpus_match_count == large.total_corpus_match_count == expected > 0
    assert small.truncated is (expected > 1)
    assert not large.truncated
    assert len(large.rows) == expected


@pytest.mark.parametrize("filters", [{"request_id": REQUEST_ID}, {"instance_id": INSTANCE_ID}])
def test_timestamp_boundaries_against_python_filtered_candidates(service, filters):
    candidates = service.search_logs(dataset="openstack", **filters, limit=50)
    assert not candidates.truncated
    first, last = candidates.rows[0]["timestamp"], candidates.rows[-1]["timestamp"]
    for start, end in ((first, last), (first, first + timedelta(microseconds=1)),
                       (last, None), (None, first), (last + timedelta(microseconds=1), None)):
        expected = [r for r in candidates.rows if (start is None or r["timestamp"] >= start)
                    and (end is None or r["timestamp"] < end)]
        result = service.search_logs(dataset="openstack", **filters, start_time=start, end_time=end, limit=50)
        assert result.total_corpus_match_count == len(expected)
        assert result.rows == expected and not result.truncated


def test_empty_injection_and_disjoint_identifiers(service):
    for filters in ({"request_id": "missing' OR 1=1 --"}, {"instance_id": "missing' OR 1=1 --"},
                    {"request_id": REQUEST_ID, "instance_id": "missing"},
                    {"request_id": REQUEST_ID, "severity": "ERROR"}):
        result = service.search_logs(dataset="openstack", **filters, limit=1)
        assert result.total_corpus_match_count == result.returned_record_count == 0
        assert result.rows == [] and result.truncated is False


def test_background_contexts_are_bounded_literal_lookups(settings, service):
    with connect(settings) as connection:
        contexts = connection.execute(
            "SELECT request_id, COUNT(*) AS n, COUNT(DISTINCT instance_id) AS vms FROM openstack_logs "
            "WHERE request_id LIKE %s OR request_id LIKE %s GROUP BY request_id",
            ("req-addc1839-%", "req-3ea4052c-%"),
        ).fetchall()
    assert sorted(r["n"] for r in contexts) == [12_248, 52_349]
    for context in contexts:
        result = service.search_logs(dataset="openstack", request_id=context["request_id"], limit=2)
        assert result.total_corpus_match_count == context["n"]
        assert result.returned_record_count == 2 and result.truncated
        assert all(r["request_id"] == context["request_id"] for r in result.rows)


def test_sasha_scenario_and_sql_only_statistics(settings, service):
    vm = service.search_logs(dataset="openstack", instance_id=INSTANCE_ID, limit=50)
    mentions = service.search_logs(dataset="openstack", text=INSTANCE_ID, match_mode="phrase", limit=50)
    assert vm.total_corpus_match_count == 25
    assert mentions.total_corpus_match_count == 28 and not mentions.truncated
    assert sum(r["service"] == "nova-api" for r in mentions.rows) == 3
    assert sum(r["is_anomaly"] for r in mentions.rows) == 25
    assert all(r["severity"] == "INFO" for r in mentions.rows)
    assert any("Took 41.81 seconds to spawn" in r["message"] for r in vm.rows)
    with connect(settings) as connection:
        research = run_research_queries(connection)
        labels = connection.execute("SELECT instance_id FROM openstack_anomalous_instances").fetchall()
    assert len(research["Q1"]) == 12 and len(research["Q2"]) == 28
    normal, anomalous = research["Q3"]
    assert normal["spawns"] == 2062 and float(normal["p50_s"]) == pytest.approx(19.74)
    assert anomalous["spawns"] == 4 and float(anomalous["max_s"]) == pytest.approx(50.72)
    assert len(research["Q4"]) == 4
    spawn_seconds = []
    for label in labels:
        trace = service.search_logs(dataset="openstack", instance_id=label["instance_id"], limit=50)
        assert trace.total_corpus_match_count == 25 and not trace.truncated
        assert all(r["severity"] == "INFO" for r in trace.rows)
        mentions = service.search_logs(dataset="openstack", text=label["instance_id"], match_mode="phrase", limit=50)
        assert mentions.total_corpus_match_count == 28 and not mentions.truncated
        assert all(r["severity"] == "INFO" for r in mentions.rows)
        assert sum(r["is_anomaly"] for r in mentions.rows) == 25
        spawn = next(r for r in trace.rows if "seconds to spawn the instance on the hypervisor" in r["message"])
        spawn_seconds.append(float(spawn["message"].split("Took ")[1].split()[0]))
    assert min(spawn_seconds) == pytest.approx(29.46)
    assert max(spawn_seconds) == pytest.approx(50.72)


@pytest.mark.parametrize("mode", ["auto", "legacy"])
def test_tracing_over_real_http_without_service_restarts(settings, mode):
    app = build_server(settings, SQL_DATASET_SPECS).streamable_http_app(host="127.0.0.1")
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
                        await anyio.sleep(0.02)
                report = await verify_endpoint(f"http://127.0.0.1:{listener.getsockname()[1]}/mcp", mode=mode, tracing=True)
                assert report["openstack_tracing"]["instance_count"] == 25
            anyio.run(run)
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            assert not thread.is_alive()
