"""SELECT-only full-corpus and synthetic VALUES tests; never DDL or data writes."""

from dataclasses import replace
from datetime import datetime, timedelta
import os
import re
import socket
import statistics
import threading

import anyio
import pytest
import uvicorn

from log_insights.config import Settings
from log_insights.db import connect
from log_insights.mcp_server import build_server
from log_insights.queries import QueryService, SQL_DATASET_SPECS
from log_insights.vm_performance import build_vm_spawn_plan, execute_vm_spawn_plan
from scripts.verify_mcp_http import verify_endpoint

pytestmark = pytest.mark.skipif(os.getenv("RUN_SQL_BACKEND_INTEGRATION_TESTS") != "1",
                               reason="Requires explicit DSN and preloaded SQL corpus; SELECT-only spawn analytics")


@pytest.fixture
def settings():
    settings = Settings.from_env()
    settings.require_serenedb_dsn()
    return settings


@pytest.fixture
def service(settings):
    return QueryService(lambda: connect(settings), SQL_DATASET_SPECS)


def test_complete_corpus_matches_independent_raw_scan_and_python_oracle(settings, service):
    # Different selection/parsing from production; bounded by this fixed corpus,
    # with no LIMIT or sampling before the independent population calculations.
    with connect(settings) as connection:
        raw = connection.execute(
            "SELECT source_file,line_no,instance_id,message FROM openstack_logs "
            "WHERE component = %s AND message LIKE %s",
            ("nova.compute.manager", "%seconds to spawn the instance on the hypervisor%"),
        ).fetchall()
        indexed = connection.execute(
            "SELECT source_file,line_no FROM openstack_logs_idx WHERE message @@ ts_phrase(%s) AND component = %s",
            ("spawn the instance on the hypervisor", "nova.compute.manager"),
        ).fetchall()
    assert {(r["source_file"],r["line_no"]) for r in raw} == {(r["source_file"],r["line_no"]) for r in indexed}
    values = [float(re.search(r"Took (\d+(?:\.\d+)?) seconds to spawn", r["message"]).group(1)) for r in raw]
    assert len(values) == 2066
    result = service.analyze_vm_spawn_performance(include_anomaly_labels=True)
    assert result["counts"] == dict(candidate_record_count=2066, spawn_event_count=2066,
                                    invalid_duration_record_count=0, unique_instance_count=2066,
                                    missing_instance_id_event_count=0)
    q = statistics.quantiles(values, n=100, method="inclusive")
    assert result["statistics"] == pytest.approx(dict(mean_seconds=statistics.mean(values), median_seconds=19.74,
        p95_seconds=21.0075, p99_seconds=21.6635, min_seconds=10.12, max_seconds=50.72))
    assert result["statistics"]["median_seconds"] == statistics.median(values)
    assert result["statistics"]["p95_seconds"] == pytest.approx(q[94])
    assert result["statistics"]["p99_seconds"] == pytest.approx(q[98])
    assert result["detection"]["effective_threshold_seconds"] == pytest.approx(q[94]*1.25)
    assert result["detection"]["slow_event_count"] == 5
    assert result["detection"]["slow_event_percentage"] == pytest.approx(100*5/2066)
    vm = next(r for r in result["slowest_spawn_events"] if r["instance_id"] == "544fd51c-4edc-4780-baae-ba1d80a0acfc")
    assert vm["duration_seconds"] == 41.81 and vm["record_id"] == 1000011980
    assert vm["is_anomaly"] is True


@pytest.mark.parametrize("threshold,expected", [(0,2066),(25,5),(30,3),(50.72,0),(41.81,1)])
def test_absolute_strict_thresholds(service, threshold, expected):
    result = service.analyze_vm_spawn_performance(slow_threshold_seconds=threshold)
    assert result["detection"]["slow_event_count"] == expected
    assert result["detection"]["method"] == "absolute"
    assert result["detection"]["slow_event_percentage"] == pytest.approx(100*expected/2066)


@pytest.mark.parametrize("scope", ["corpus","analysis"])
@pytest.mark.parametrize("top_limit,bucket_limit", [(1,100),(50,1),(1,1)])
def test_statistics_do_not_depend_on_output_limits_or_label_metadata(service, scope, top_limit, bucket_limit):
    args = dict(baseline_scope=scope)
    small = service.analyze_vm_spawn_performance(**args, top_limit=top_limit, bucket_limit=bucket_limit)
    large = service.analyze_vm_spawn_performance(**args, top_limit=50, bucket_limit=100, include_anomaly_labels=True)
    for key in ("counts","statistics","baseline","detection"):
        assert small[key] == large[key]
    assert small["ranking"]["returned_event_count"] == top_limit and small["ranking"]["truncated"]
    assert small["ranking"]["slow_events_truncated"] is (top_limit < 5)
    assert small["time_series"]["returned_bucket_count"] == min(3,bucket_limit)
    assert small["time_series"]["truncated"] is (bucket_limit < 3)
    assert large["time_series"]["total_bucket_count"] == 3
    assert not large["time_series"]["truncated"]
    assert sum(b["spawn_event_count"] for b in large["time_series"]["buckets"]) == 2066
    assert sum(b["slow_event_count"] for b in large["time_series"]["buckets"]) == 5
    assert all("is_anomaly" not in r for r in small["slowest_spawn_events"])
    assert all("is_anomaly" in r for r in large["slowest_spawn_events"])


def test_baseline_scope_and_time_filtering(service):
    args = dict(start_time=datetime(2017,5,14),end_time=datetime(2017,5,15),interval="hour")
    corpus = service.analyze_vm_spawn_performance(**args)
    analysis = service.analyze_vm_spawn_performance(**args, baseline_scope="analysis")
    assert corpus["counts"]["spawn_event_count"] == analysis["counts"]["spawn_event_count"] == 197
    assert corpus["baseline"]["spawn_event_count"] == 2066
    assert analysis["baseline"]["spawn_event_count"] == 197
    assert analysis["baseline"]["p95_seconds"] == pytest.approx(21.102)
    for result in (corpus, analysis):
        assert result["detection"]["effective_threshold_seconds"] == pytest.approx(result["baseline"]["p95_seconds"]*1.25)
        assert sum(b["spawn_event_count"] for b in result["time_series"]["buckets"]) == 197
    # Independently observed completion timestamp; check exact inclusion and
    # exclusion, not just matching SQL count/group predicates.
    first = datetime(2017,5,14,19,39,22,590000)
    for start,end,expected in ((first,first+timedelta(microseconds=1),1), (None,first,0),
                               (first,None,2066), (datetime(2030,1,1),None,0)):
        result = service.analyze_vm_spawn_performance(start_time=start,end_time=end)
        assert result["counts"]["spawn_event_count"] == expected


def test_empty_analysis_and_empty_baseline_are_distinct(service):
    for scope in ("corpus","analysis"):
        result = service.analyze_vm_spawn_performance(start_time=datetime(2030,1,1),baseline_scope=scope)
        assert result["counts"]["spawn_event_count"] == 0
        assert all(v is None for v in result["statistics"].values())
        assert result["slowest_spawn_events"] == result["time_series"]["buckets"] == []
        assert result["detection"]["slow_event_percentage"] is None
        assert result["ranking"]["truncated"] is False
        if scope == "corpus":
            assert result["detection"]["status"] == "ready"
            assert result["detection"]["slow_event_count"] == 0
        else:
            assert result["detection"]["status"] == "unavailable_baseline"
            assert result["detection"]["slow_event_count"] is None


def test_bucket_order_and_disabled_time_series(service):
    asc = service.analyze_vm_spawn_performance(interval="hour",sort_direction="asc")
    desc = service.analyze_vm_spawn_performance(interval="hour",sort_direction="desc")
    assert asc["time_series"]["buckets"] == list(reversed(desc["time_series"]["buckets"]))
    last = service.analyze_vm_spawn_performance(interval="hour",bucket_limit=1)
    assert last["time_series"]["buckets"] == desc["time_series"]["buckets"][:1]
    off = service.analyze_vm_spawn_performance(interval=None)
    assert not off["time_series"]["enabled"] and off["time_series"]["buckets"] == []
    assert off["statistics"] == asc["statistics"]


def synthetic(settings, records, **options):
    """Replace only the code-owned candidate SELECT with bound VALUES, no DDL.

    records: (record_id, timestamp, instance_id, message, stored_label).
    The actual production parser, summary, baseline, detection and ranking SQL
    then execute unchanged, including optional labels and all output limits.
    """
    plan = build_vm_spawn_plan(SQL_DATASET_SPECS["openstack"],**options)
    params = dict(plan.parameters)
    tuples = []
    for i, record in enumerate(records):
        names = [f"fixture_{i}_{j}" for j in range(5)]
        params.update(zip(names,record))
        tuples.append("("+",".join(f"%({n})t" for n in names)+")")
    labels = ", CAST(label AS BOOLEAN) AS is_anomaly" if options.get("include_anomaly_labels") else ""
    source = ("SELECT CAST(id AS BIGINT) AS record_id,CAST(ts AS TIMESTAMP) AS timestamp,"
              "CAST(vm AS VARCHAR) AS instance_id,'nova.compute.manager' AS component,CAST(msg AS VARCHAR) AS message"
              +labels+" FROM (VALUES "+",".join(tuples)+") f(id,ts,vm,msg,label)")
    plan = replace(plan,sql=plan.sql.replace(plan.candidate_sql,source,1),parameters=params)
    return execute_vm_spawn_plan(lambda:connect(settings),plan)


def message(duration):
    return f"Took {duration} seconds to spawn the instance on the hypervisor."


def test_malformed_missing_vm_repeated_vm_and_calendar_boundary(settings):
    end = datetime(2024,3,1)
    durations = ["25","25.00000001","0","-1","1..2","", "9"*400]
    records = [(i+1,end+timedelta(microseconds=i-1),None if i==2 else "same-vm",message(d),bool(i%2))
               for i,d in enumerate(durations)]
    result = synthetic(settings,records,slow_threshold_seconds=25,top_limit=50)
    assert result["counts"] == dict(candidate_record_count=7,spawn_event_count=3,invalid_duration_record_count=4,
                                    unique_instance_count=1,missing_instance_id_event_count=1)
    assert result["detection"]["slow_event_count"] == 1
    assert result["statistics"]["min_seconds"] == 0
    assert result["ranking"]["returned_event_count"] == 3 and not result["ranking"]["truncated"]
    assert result["slowest_spawn_events"][0]["is_slow"] is True
    assert result["slowest_spawn_events"][1]["is_slow"] is False
    assert [b["bucket_start"] for b in result["time_series"]["buckets"]] == ["2024-03-01T00:00:00","2024-02-29T00:00:00"]
    assert sum(b["invalid_duration_record_count"] for b in result["time_series"]["buckets"]) == 4


def test_changing_labels_cannot_change_detection_or_baseline(settings):
    records = [(i+1,datetime(2024,1,1)+timedelta(seconds=i),f"vm-{i}",message(d),i==0)
               for i,d in enumerate((10,)*20+(30,))]
    a = synthetic(settings,records,include_anomaly_labels=True)
    b = synthetic(settings,[(*r[:-1],not r[-1]) for r in records],include_anomaly_labels=True)
    assert a["detection"]["slow_event_count"] == b["detection"]["slow_event_count"] == 1
    for key in ("counts","statistics","baseline","detection","ranking","time_series"):
        assert a[key] == b[key]
    assert a["slowest_spawn_events"][0]["is_anomaly"] is not b["slowest_spawn_events"][0]["is_anomaly"]
    assert [{k:v for k,v in r.items() if k!="is_anomaly"} for r in a["slowest_spawn_events"]] == [
        {k:v for k,v in r.items() if k!="is_anomaly"} for r in b["slowest_spawn_events"]]


@pytest.mark.parametrize("duration", ["", "0"])
def test_unavailable_relative_baseline_does_not_invent_slow_counts(settings,duration):
    records=[(1,datetime(2024,1,1),None,message(duration),False)]
    relative=synthetic(settings,records)
    assert relative["detection"]["status"] == "unavailable_baseline"
    assert relative["detection"]["slow_event_count"] is None
    absolute=synthetic(settings,records,slow_threshold_seconds=0)
    assert absolute["detection"]["status"] == "ready" and absolute["detection"]["slow_event_count"] == 0
    if duration=="":
        bucket=relative["time_series"]["buckets"][0]
        assert bucket["spawn_event_count"] == 0 and bucket["slow_event_percentage"] is None


def test_ties_use_timestamp_then_record_id(settings):
    t=datetime(2024,1,1)
    records=[(3,t,"c",message(10),False),(2,t,"b",message(10),False),(1,t-timedelta(seconds=1),"a",message(10),False)]
    result=synthetic(settings,records,top_limit=2)
    assert [r["record_id"] for r in result["slowest_spawn_events"]] == [1,2]
    assert result["ranking"]["truncated"]


@pytest.mark.parametrize("mode", ["auto","legacy"])
def test_performance_and_existing_features_over_http(settings,mode):
    app=build_server(settings,SQL_DATASET_SPECS).streamable_http_app(host="127.0.0.1")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1",0)); listener.listen(16)
        server=uvicorn.Server(uvicorn.Config(app,log_level="warning"))
        thread=threading.Thread(target=server.run,kwargs={"sockets":[listener]},daemon=True)
        thread.start()
        try:
            async def run():
                with anyio.fail_after(10):
                    while not server.started:
                        await anyio.sleep(0.02)
                report=await verify_endpoint(f"http://127.0.0.1:{listener.getsockname()[1]}/mcp",mode=mode,
                                             patterns=True,tracing=True,performance=True)
                assert report["vm_spawn_performance"]["spawn_event_count"] == 2066
            anyio.run(run)
        finally:
            server.should_exit=True
            thread.join(timeout=10)
            assert not thread.is_alive()
