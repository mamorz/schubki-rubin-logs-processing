"""Read-only full-corpus and SELECT/VALUES spike analytics verification."""

from collections import defaultdict
from dataclasses import replace
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
from log_insights.spikes import build_spike_plan, execute_spike_plan
from scripts.verify_mcp_http import verify_endpoint

pytestmark = pytest.mark.skipif(os.getenv("RUN_SQL_BACKEND_INTEGRATION_TESTS") != "1",
    reason="Requires explicit DSN and preloaded SQL corpus; SELECT-only BGL spikes")


@pytest.fixture
def settings():
    result = Settings.from_env()
    result.require_serenedb_dsn()
    return result


@pytest.fixture
def service(settings):
    return QueryService(lambda: connect(settings), SQL_DATASET_SPECS)


def inclusive_quantile(values, q):
    values = sorted(values)
    pos = (len(values) - 1) * q
    i = int(pos)
    return values[i] + (values[min(i + 1, len(values) - 1)] - values[i]) * (pos - i)


def test_complete_population_independent_buckets_quantiles_and_windows(settings, service):
    # Arithmetic bucketing and Python interpolation/merging are independent of
    # production date_trunc, quantile_cont and row-number island SQL. Transfer
    # only ~5k bucket aggregates, never 4.7 million messages.
    with connect(settings) as c:
        rows = c.execute("""SELECT component, CAST(floor(epoch(ts)/3600) AS BIGINT) h,
            COUNT(*) n, COUNT(*) FILTER(WHERE level<>'INFO') selected,
            MIN(ts) first_ts, MAX(ts) last_ts,
            MIN(round((epoch(ts)-epoch)/3600.0)) lo,
            MAX(round((epoch(ts)-epoch)/3600.0)) hi
            FROM bgl_logs GROUP BY 1,2""").fetchall()
    assert sum(r["n"] for r in rows) == 4747963
    assert len(rows) == 5017
    first, last = min(r["first_ts"] for r in rows), max(r["last_ts"] for r in rows)
    offsets = defaultdict(set)
    for row in rows:
        offsets[row["h"]].update((row["lo"], row["hi"]))
    populations = defaultdict(list)
    for row in rows:
        start = datetime(1970, 1, 1) + timedelta(hours=row["h"])
        if start >= first and start + timedelta(hours=1) <= last and len(offsets[row["h"]]) == 1:
            populations[row["component"]].append(row)
    result = service.detect_log_spikes(max_findings=20, patterns_per_finding=5)
    assert result["counts"]["corpus_record_count"] == 4747963
    assert result["counts"]["eligible_component_bucket_count"] == 5013
    expected_windows = []
    for baseline in result["baselines"]:
        population = populations[baseline["component"]]
        assert baseline["bucket_count"] == len(population)
        q = inclusive_quantile([r["selected"] for r in population], .99)
        assert baseline["quantile_count"] == pytest.approx(q)
        assert baseline["median_count"] == pytest.approx(inclusive_quantile([r["selected"] for r in population], .5))
        if len(population) < 100:
            assert baseline["status"] == "insufficient_baseline"
            continue
        assert baseline["threshold"] == pytest.approx(q * 1.5)
        elevated = sorted((r for r in population if r["selected"] >= 20 and r["selected"] > q * 1.5), key=lambda r: r["h"])
        previous = None
        for row in elevated:
            if previous is None or row["h"] != previous + 1:
                expected_windows.append(dict(component=baseline["component"], start=row["h"], end=row["h"] + 1,
                                             records=row["selected"], buckets=1))
            else:
                expected_windows[-1]["end"] += 1
                expected_windows[-1]["records"] += row["selected"]
                expected_windows[-1]["buckets"] += 1
            previous = row["h"]
    expected_windows.sort(key=lambda w: (-w["records"], w["start"], w["component"]))
    assert result["counts"]["elevated_component_bucket_count"] == sum(w["buckets"] for w in expected_windows) == 24
    assert result["counts"]["spike_window_count"] == len(expected_windows) == 16
    for actual, expected in zip(result["findings"], expected_windows, strict=True):
        assert actual["component"] == expected["component"]
        assert actual["start_time"] == (datetime(1970, 1, 1) + timedelta(hours=expected["start"])).isoformat()
        assert actual["end_time"] == (datetime(1970, 1, 1) + timedelta(hours=expected["end"])).isoformat()
        assert actual["selected_record_count"] == expected["records"]
        assert actual["elevated_bucket_count"] == expected["buckets"]
    assert result["coverage"]["unobserved_global_bucket_count"] == 1536
    assert result["coverage"]["ambiguous_global_clock_bucket_count"] == 1


def test_mmcs_exact_contributors_and_original_records(settings, service):
    result = service.detect_log_spikes(component="MMCS", evidence_per_pattern=3)
    finding = next(f for f in result["findings"] if f["start_time"] == "2005-09-20T12:00:00")
    assert finding["selected_record_count"] == 66563
    assert [p["record_count"] for p in finding["contributors"]] == [63468, 3095]
    assert finding["omitted_pattern_record_count"] == 0 and not finding["patterns_truncated"]
    with connect(settings) as c:
        raw = c.execute("""SELECT level,message,count(*) n FROM bgl_logs WHERE component=%(c)s
            AND ts >= %(start)t AND ts < %(end)t GROUP BY level,message ORDER BY n DESC,message""",
            dict(c="MMCS", start=datetime(2005,9,20,12), end=datetime(2005,9,20,13))).fetchall()
        assert [(p["severity"],p["message"],p["record_count"]) for p in finding["contributors"]] == [
            (r["level"],r["message"],r["n"]) for r in raw]
        for pattern in finding["contributors"]:
            assert pattern["percentage_of_window_selected_records"] == pytest.approx(100 * pattern["record_count"] / 66563)
            for evidence in pattern["evidence"]:
                row = c.execute("SELECT ts,message,level FROM bgl_logs WHERE line_no=%s", (evidence["record_id"],)).fetchone()
                assert row["message"] == pattern["message"] and row["level"] == pattern["severity"]
                assert row["ts"].isoformat() == evidence["timestamp"]
    assert not finding["contributors"][0]["message_truncated"]


@pytest.mark.parametrize("options", [dict(max_findings=1), dict(patterns_per_finding=1), dict(evidence_per_pattern=3),
                                       dict(max_findings=1, patterns_per_finding=1, evidence_per_pattern=3)])
def test_limits_cannot_change_counts_baselines_or_window_statistics(service, options):
    full = service.detect_log_spikes(max_findings=20, patterns_per_finding=5, evidence_per_pattern=1)
    limited = service.detect_log_spikes(**options)
    for key in ("coverage", "baselines", "method"):
        assert full[key] == limited[key]
    assert {k:v for k,v in full["counts"].items() if k != "returned_spike_window_count"} == {
        k:v for k,v in limited["counts"].items() if k != "returned_spike_window_count"}
    for i, finding in enumerate(limited["findings"]):
        expected = full["findings"][i]
        for field in ("selected_record_count", "total_record_count", "peak_bucket_count", "threshold",
                      "start_time", "end_time", "distinct_node_identifier_count", "total_pattern_count"):
            assert finding[field] == expected[field]
        assert sum(p["record_count"] for p in finding["contributors"]) + finding["omitted_pattern_record_count"] == finding["selected_record_count"]
        assert sum(p["percentage_of_window_selected_records"] for p in finding["contributors"]) + finding["omitted_pattern_record_percentage"] == pytest.approx(100)
    assert limited["truncation"]["findings_truncated"] is (len(limited["findings"]) < 16)


def test_daily_total_volume_and_real_reference_ranges(service):
    daily = service.detect_log_spikes(interval="day")
    assert daily["counts"]["spike_window_count"] == 2
    total = service.detect_log_spikes(signal="total_volume")
    assert total["counts"]["spike_window_count"] == 25
    assert total["counts"]["analysis_selected_record_count"] == 4747963
    reference = service.detect_log_spikes(component="MMCS", baseline_scope="reference",
        baseline_start_time=datetime(2005,6,1), baseline_end_time=datetime(2005,9,1),
        start_time=datetime(2005,9,20,12), end_time=datetime(2005,9,20,13), min_baseline_buckets=10)
    assert reference["counts"]["analysis_selected_record_count"] == 66563
    assert reference["counts"]["spike_window_count"] == 1
    assert reference["baselines"][0]["observed_end"] <= "2005-09-01T00:00:00"
    assert not any("retrospective" in warning for warning in reference["warnings"])


def test_empty_sparse_and_literal_component_filter(service):
    future = service.detect_log_spikes(start_time=datetime(2030,1,1))
    assert future["counts"]["analysis_record_count"] == future["counts"]["eligible_component_bucket_count"] == 0
    assert future["findings"] == [] and not future["truncation"]["findings_truncated"]
    missing = service.detect_log_spikes(component="KERNEL' OR TRUE --")
    assert missing["counts"]["analysis_record_count"] == 0 and missing["baselines"] == []
    sparse = service.detect_log_spikes(component="SERV_NET")
    assert sparse["baselines"][0]["status"] == "insufficient_baseline"
    assert sparse["counts"]["evaluated_component_bucket_count"] == 0
    assert sparse["findings"] == []


T = datetime(2024,2,28)


def events(hour, n=1, *, severity="ERROR", message="same", component="C", offset=0, node="n", missing_epoch=False):
    return [dict(timestamp=T+timedelta(hours=hour,seconds=i+1), severity=severity, component=component,
                 message=message, offset=offset, node=node, missing_epoch=missing_epoch) for i in range(n)]


def synthetic(settings, rows, *, flip_labels=False, guards=True, **options):
    # SELECT/VALUES only. Inject a code-owned typed source into the unchanged
    # production SQL. No CREATE, INSERT, UPDATE, temporary tables or label writes.
    rows = list(rows)
    if guards and rows:
        daily = options.get("interval") == "day"
        bounds = [r["timestamp"].replace(minute=0,second=0,microsecond=0) for r in rows]
        if daily:
            bounds = [t.replace(hour=0) for t in bounds]
        lo = min(bounds)-timedelta(seconds=1)
        hi = max(bounds)+(timedelta(days=1) if daily else timedelta(hours=1))+timedelta(seconds=1)
        rows += [dict(timestamp=t,severity="INFO",component="guard",message="guard",offset=0,node=None) for t in (lo,hi)]
    plan = build_spike_plan(SQL_DATASET_SPECS["bgl"], **options)
    parameters = dict(plan.parameters)
    tuples = []
    # A typed empty source is also valid, so truly empty populations can execute.
    for i, r in enumerate(rows):
        epoch = None if r.get("missing_epoch") else int((r["timestamp"]-datetime(1970,1,1)).total_seconds())+r["offset"]*3600
        values = (i+1,r["timestamp"],epoch,r["component"],r["severity"],r["node"],r["message"],bool(i%2)^flip_labels)
        names = [f"fixture_{i}_{j}" for j in range(8)]
        parameters.update(zip(names,values))
        tuples.append("("+",".join(f"%({name})t" for name in names)+")")
    if tuples:
        source = "FROM (VALUES "+",".join(tuples)+") f(id,ts,ep,comp,sev,nd,msg,label)"
    else:
        source = "FROM (SELECT NULL id,NULL ts,NULL ep,NULL comp,NULL sev,NULL nd,NULL msg,NULL label WHERE FALSE) f"
    source = ("SELECT CAST(id AS BIGINT) record_id,CAST(ts AS TIMESTAMP) timestamp,CAST(ep AS BIGINT) epoch,"
              "CAST(comp AS VARCHAR) component,CAST(sev AS VARCHAR) severity,CAST(nd AS VARCHAR) node,"
              "CAST(msg AS VARCHAR) message,CAST(label AS BOOLEAN) is_alert,"
              "CASE WHEN CAST(label AS BOOLEAN) THEN 'LABEL' ELSE NULL END alert_label "+source)
    plan = replace(plan,sql=plan.sql.replace(plan.source_sql,source,1),parameters=parameters)
    return execute_spike_plan(lambda:connect(settings),plan)


def ref_options(**kwargs):
    return dict(component="C", baseline_scope="reference", baseline_start_time=T, baseline_end_time=T+timedelta(hours=1),
                start_time=T+timedelta(hours=1), end_time=T+timedelta(hours=10),
                min_baseline_buckets=1, min_bucket_count=1, threshold_multiplier=1, **kwargs)


@pytest.mark.parametrize("severity", ["INFO","WARNING","ERROR","SEVERE","FAILURE","FATAL"])
def test_each_severity_and_zero_adverse_baselines(settings, severity):
    rows = events(0,severity="INFO") + events(1,2,severity=severity)
    result = synthetic(settings,rows,**ref_options())
    expected = 0 if severity == "INFO" else 1
    assert result["counts"]["spike_window_count"] == expected
    assert result["baselines"][0]["bucket_count"] == 1
    assert result["baselines"][0]["quantile_count"] == 0
    chosen = synthetic(settings,rows,**ref_options(),severities=[severity])
    assert chosen["counts"]["spike_window_count"] == 1


def test_threshold_equality_and_minimum_count_are_separate(settings):
    rows = events(0,2) + events(1,2) + events(2,3) + events(3,4)
    args = ref_options()
    args["min_bucket_count"] = 4
    result = synthetic(settings,rows,**args)
    assert result["counts"]["elevated_component_bucket_count"] == 1
    assert result["findings"][0]["selected_record_count"] == 4
    args["min_bucket_count"] = 1
    result = synthetic(settings,rows,**args)
    assert result["counts"]["elevated_component_bucket_count"] == 2
    assert result["findings"][0]["elevated_bucket_count"] == 2
    assert result["findings"][0]["start_time"] == (T+timedelta(hours=2)).isoformat()


def test_missing_excluded_and_non_elevated_buckets_break_windows(settings):
    rows = events(0,severity="INFO") + events(1,2) + events(2,2) + events(4,2)
    rows += events(5,severity="INFO") + events(6,2) + events(7,2) + events(8,2)
    # A different component introduces a second offset globally in hour 7.
    rows += events(7,component="OTHER",offset=1)
    result = synthetic(settings,rows,**ref_options())
    assert result["counts"]["elevated_component_bucket_count"] == 5
    assert [f["elevated_bucket_count"] for f in result["findings"]] == [2,1,1,1]
    assert result["counts"]["ambiguous_clock_component_bucket_count"] == 1
    assert result["coverage"]["unobserved_global_bucket_count"] > 0
    assert result["coverage"]["missing_periods_treated_as_zero"] is False


def test_partial_request_corpus_boundaries_and_timestamp_endpoints(settings):
    rows = events(0,severity="INFO") + events(1,2) + events(2,2) + events(3,2)
    args = ref_options()
    args.update(start_time=T+timedelta(hours=1,microseconds=1), end_time=T+timedelta(hours=3))
    result = synthetic(settings,rows,**args)
    assert result["counts"]["analysis_selected_record_count"] == 4
    assert result["counts"]["eligible_selected_record_count"] == 2
    assert result["counts"]["incomplete_boundary_component_bucket_count"] == 1
    assert result["findings"][0]["start_time"] == (T+timedelta(hours=2)).isoformat()
    # Without guard rows, the first and last source buckets are partial too.
    unguarded = synthetic(settings,rows,guards=False,component="C",min_baseline_buckets=1,min_bucket_count=1)
    assert unguarded["counts"]["incomplete_boundary_component_bucket_count"] == 2
    assert unguarded["counts"]["eligible_component_bucket_count"] == 2


def test_missing_epoch_excludes_global_bucket(settings):
    rows = events(0,severity="INFO") + events(1,2) + events(1,component="OTHER",missing_epoch=True)
    result = synthetic(settings,rows,**ref_options())
    assert result["counts"]["missing_clock_component_bucket_count"] == 1
    assert not result["findings"]


def test_calendar_daily_leap_day_and_non_aligned_reference_boundaries(settings):
    rows = events(0,severity="INFO") + events(24,2) + events(48,3)
    result = synthetic(settings,rows,component="C",interval="day",min_baseline_buckets=1,
        min_bucket_count=1,baseline_quantile=.5,threshold_multiplier=1,
        baseline_scope="reference",baseline_start_time=T,baseline_end_time=T+timedelta(days=1),
        start_time=T+timedelta(days=1),end_time=T+timedelta(days=3))
    assert result["findings"][0]["start_time"] == "2024-02-29T00:00:00"
    assert result["findings"][0]["end_time"] == "2024-03-02T00:00:00"
    assert result["findings"][0]["elevated_bucket_count"] == 2
    args = ref_options()
    args["baseline_start_time"] = T+timedelta(microseconds=1)
    none = synthetic(settings,events(0,severity="INFO")+events(1,2),**args)
    assert none["baselines"][0]["bucket_count"] == 0
    assert none["baselines"][0]["status"] == "insufficient_baseline"


def test_labels_exact_patterns_ties_missing_nodes_and_message_bounds(settings):
    long_a, long_b = "x"*1100+"a", "x"*1100+"b"
    rows = events(0,severity="INFO") + events(1,2,message=long_a,node=None) + events(1,2,message=long_b)
    rows += events(1,message="Case") + events(1,message="case") + events(1,message="case ") + events(1,message="")
    a = synthetic(settings,rows,**ref_options(),patterns_per_finding=5,evidence_per_pattern=2)
    b = synthetic(settings,rows,flip_labels=True,**ref_options(),patterns_per_finding=5,evidence_per_pattern=2)
    for key in ("counts","coverage","baselines","findings","method"):
        assert a[key] == b[key]
    finding = a["findings"][0]
    assert finding["total_pattern_count"] == 6
    assert finding["missing_node_record_count"] == 2
    assert finding["distinct_node_identifier_count"] == 1
    assert finding["omitted_pattern_record_count"] == 1
    first, second = finding["contributors"][:2]
    assert first["message"] == second["message"] == "x"*1000
    assert first["message_truncated"] and second["message_truncated"]
    assert first["message_fingerprint"] != second["message_fingerprint"]
    assert [e["record_id"] for e in first["evidence"]] == [2,3]
    assert first["evidence_truncated"] is False


def test_empty_population_and_insufficient_baseline_are_explicit(settings):
    empty = synthetic(settings,[],component="C")
    assert empty["counts"]["corpus_record_count"] == 0 and empty["findings"] == []
    assert empty["baselines"] == [] and empty["coverage"]["observed_global_bucket_count"] == 0
    sparse = synthetic(settings,events(0,severity="INFO")+events(1,30),component="C")
    assert sparse["baselines"][0]["status"] == "insufficient_baseline"
    assert sparse["counts"]["evaluated_component_bucket_count"] == 0
    assert sparse["counts"]["eligible_component_bucket_count"] == 2


@pytest.mark.parametrize("support", [99,100])
def test_minimum_baseline_support_is_inclusive_and_zero_baseline_can_detect(settings, support):
    rows = [r for h in range(support) for r in events(h,severity="INFO")] + events(101,20)
    result = synthetic(settings,rows,component="C",baseline_scope="reference",
        baseline_start_time=T,baseline_end_time=T+timedelta(hours=100),
        start_time=T+timedelta(hours=101),end_time=T+timedelta(hours=102))
    assert result["baselines"][0]["bucket_count"] == support
    assert result["counts"]["spike_window_count"] == (1 if support == 100 else 0)
    if support == 100:
        assert result["baselines"][0]["threshold"] == 0
        assert result["findings"][0]["peak_to_baseline_quantile_ratio"] is None


def test_finding_ties_order_by_time_then_component(settings):
    rows = events(0,severity="INFO") + events(0,severity="INFO",component="D")
    rows += events(1,2,component="D") + events(1,2) + events(3,2)
    args = ref_options()
    args["component"] = None
    result = synthetic(settings,rows,**args)
    assert [(f["component"],f["start_time"]) for f in result["findings"]] == [
        ("C",(T+timedelta(hours=1)).isoformat()),("D",(T+timedelta(hours=1)).isoformat()),
        ("C",(T+timedelta(hours=3)).isoformat())]


def test_nonfinite_derived_threshold_is_explicit_and_never_classifies(settings):
    args = ref_options()
    args["threshold_multiplier"] = 1e308
    result = synthetic(settings,events(0,2)+events(1,4),**args)
    assert result["baselines"][0]["status"] == "nonfinite_threshold"
    assert result["baselines"][0]["threshold"] is None
    assert result["counts"]["evaluated_component_bucket_count"] == 0
    assert not result["findings"]


@pytest.mark.parametrize("mode", ["auto","legacy"])
def test_spikes_and_existing_features_over_http(settings, mode):
    app = build_server(settings,SQL_DATASET_SPECS).streamable_http_app(host="127.0.0.1")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1",0)); listener.listen(16)
        server = uvicorn.Server(uvicorn.Config(app,log_level="warning"))
        thread = threading.Thread(target=server.run,kwargs={"sockets":[listener]},daemon=True)
        thread.start()
        try:
            async def run():
                with anyio.fail_after(10):
                    while not server.started:
                        await anyio.sleep(.02)
                report = await verify_endpoint(f"http://127.0.0.1:{listener.getsockname()[1]}/mcp",mode=mode,
                    patterns=True,tracing=True,performance=True,spikes=True)
                assert report["log_spikes"]["spike_window_count"] == 16
            anyio.run(run)
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            assert not thread.is_alive()
