"""BGL spike input, query population, serialization and MCP contracts."""

from copy import deepcopy
from datetime import datetime, timezone

import anyio
from mcp import Client
import pytest

from log_insights.config import Settings
from log_insights.mcp_server import build_server
from log_insights.queries import DATASET_SPECS, SQL_DATASET_SPECS, QueryService
from log_insights.spikes import DEFAULT_SEVERITIES, build_spike_plan, serialize_spikes


def plan(**kwargs):
    return build_spike_plan(SQL_DATASET_SPECS["bgl"], **kwargs)


@pytest.mark.parametrize("interval", ["hour", "day"])
@pytest.mark.parametrize("signal", ["severity_volume", "total_volume"])
@pytest.mark.parametrize("reference", [False, True])
def test_population_sql_and_bound_values(interval, signal, reference):
    args = dict(start_time=datetime(2005, 7, 1, 0, 15), end_time=datetime(2005, 7, 3),
                component="KERNEL' OR 1=1 --", interval=interval, signal=signal)
    if reference:
        args.update(baseline_scope="reference", baseline_start_time=datetime(2005, 6, 1),
                    baseline_end_time=datetime(2005, 7, 1))
    p = plan(**args)
    assert p.sql.startswith("WITH source AS NOT MATERIALIZED")
    assert "bgl_logs_idx" not in p.sql
    assert "is_alert" not in p.sql and "alert_label" not in p.sql
    assert "4747963" not in p.sql
    assert "KERNEL" not in p.sql and p.parameters["component"] == args["component"]
    assert "date_trunc('" + interval + "', timestamp)" in p.sql
    assert "selected_record_count > b.threshold" in p.sql
    assert "selected_record_count >= %(min_bucket_count)t" in p.sql
    assert "bucket_start >= %(start_time)t" in p.sql
    assert "timestamp >= %(start_time)t" in p.sql
    assert "LIMIT" not in p.sql.split("windows AS MATERIALIZED")[0]
    assert "GROUP BY finding_id, severity, message" in p.sql
    assert "COUNT(DISTINCT node)" in p.sql
    assert "message = p.message" in p.sql
    assert "LEAD(bucket_start)" in p.sql
    assert "NOT ambiguous_clock" in p.sql
    assert p.parameters["baseline_quantile"] == .99
    assert p.parameters["threshold_multiplier"] == 1.5
    if signal == "severity_volume":
        assert {v for k, v in p.parameters.items() if k.startswith("severity_")} == set(DEFAULT_SEVERITIES)
    else:
        assert p.applied_filters["severities"] is None
        assert not any(k.startswith("severity_") for k in p.parameters)


@pytest.mark.parametrize("args", [
    {"dataset": "openstack"}, {"dataset": "bgl'; SELECT 1 --"}, {"dataset": None},
    {"interval": "minute"}, {"interval": "hour'); SELECT 1 --"}, {"interval": None},
    {"signal": "is_alert"}, {"signal": None},
    {"severities": []}, {"severities": "ERROR"}, {"severities": [None]}, {"severities": [True]},
    {"severities": ["CRITICAL"]}, {"severities": ["ERROR' OR TRUE --"]},
    {"signal": "total_volume", "severities": ["INFO"]},
    {"baseline_scope": "normal"}, {"baseline_scope": "reference"},
    {"baseline_start_time": datetime(2005, 6, 1)},
    {"baseline_scope": "reference", "start_time": datetime(2005, 6, 1), "end_time": datetime(2005, 7, 1),
     "baseline_start_time": datetime(2005, 6, 20), "baseline_end_time": datetime(2005, 8, 1)},
    {"start_time": "2005-06-01"}, {"end_time": 1},
    {"start_time": datetime(2005, 6, 1, tzinfo=timezone.utc)},
    {"baseline_end_time": datetime(2005, 6, 1, tzinfo=timezone.utc)},
    {"start_time": datetime(2005, 7, 1), "end_time": datetime(2005, 7, 1)},
    {"component": ""}, {"component": 1}, {"component": "x" * 257},
    {"baseline_quantile": 0}, {"baseline_quantile": 1}, {"baseline_quantile": True},
    {"baseline_quantile": float("nan")}, {"baseline_quantile": "0.99"},
    {"threshold_multiplier": .9}, {"threshold_multiplier": True},
    {"threshold_multiplier": float("inf")}, {"threshold_multiplier": 10**1000},
    {"min_bucket_count": 0}, {"min_bucket_count": True}, {"min_bucket_count": 1.5},
    {"min_baseline_buckets": 0}, {"min_baseline_buckets": 1_000_001},
    {"max_findings": 0}, {"max_findings": 21}, {"max_findings": True},
    {"patterns_per_finding": 0}, {"patterns_per_finding": 6},
    {"evidence_per_pattern": 0}, {"evidence_per_pattern": 4},
])
def test_invalid_input_cannot_reach_database(args):
    with pytest.raises(ValueError):
        QueryService(lambda: pytest.fail("invalid request connected"), SQL_DATASET_SPECS).detect_log_spikes(**args)


@pytest.mark.parametrize("specs,dataset", [(DATASET_SPECS, "bgl"), (DATASET_SPECS, "openstack")])
def test_sample_backends_unsupported(specs, dataset):
    with pytest.raises(ValueError, match="SQL-backed BGL"):
        QueryService(lambda: pytest.fail("sample connected"), specs).detect_log_spikes(dataset=dataset)


def test_severities_deduplicate_deterministically():
    assert plan(severities=["ERROR", "INFO", "ERROR"]).applied_filters["severities"] == ["ERROR", "INFO"]


def raw_result():
    counts = dict(observed_bucket_count=5, calendar_bucket_count=7, ambiguous_clock_bucket_count=1,
        missing_clock_bucket_count=0, gap_count=2, analysis_component_active_bucket_count=5,
        incomplete_boundary_component_bucket_count=2, ambiguous_clock_component_bucket_count=1,
        missing_clock_component_bucket_count=0, eligible_component_bucket_count=2,
        eligible_record_count=100, eligible_selected_record_count=80, evaluated_component_bucket_count=2,
        elevated_component_bucket_count=2, elevated_record_count=80, spike_window_count=7,
        baseline_component_count=1, unavailable_baseline_component_count=0)
    return dict(corpus=dict(record_count=1000, observed_start="2005-06-01 00:15:00", observed_end="2005-06-04 00:15:00"),
        input=dict(record_count=100, selected_record_count=80), counts=counts, baselines=[], gaps=None,
        clock_diagnostics=None, clock_diagnostic_count=1,
        findings=[dict(finding_id=1, island="private", component="MMCS", start_time="2005-06-02 00:00:00",
            end_time="2005-06-02 02:00:00", elevated_bucket_count=2, selected_record_count=80,
            total_record_count=100, peak_bucket_count=50, baseline_mean_count=0., baseline_quantile_count=0.)],
        window_quality=[dict(finding_id=1, distinct_node_identifier_count=1, missing_node_record_count=4)],
        patterns=[dict(finding_id=1, pattern_rank=1, message="exact", severity="ERROR", record_count=60)],
        evidence=[dict(finding_id=1, pattern_rank=1, evidence_rank=1, record_id=5, timestamp="2005-06-02 00:00:00", node=None)],
        pattern_counts=[dict(finding_id=1, total_pattern_count=2, complete_pattern_record_count=80)])


def test_serialization_preserves_full_counts_remainder_nulls_and_truncation():
    result = serialize_spikes(raw_result(), plan(max_findings=1, patterns_per_finding=1))
    assert result["counts"]["spike_window_count"] == 7
    assert result["truncation"]["findings_truncated"]
    f = result["findings"][0]
    assert f["selected_record_count"] == f["returned_pattern_record_count"] + f["omitted_pattern_record_count"] == 80
    assert f["omitted_pattern_record_count"] == 20
    assert f["contributors"][0]["percentage_of_window_selected_records"] == 75
    assert f["contributors"][0]["evidence"][0]["node"] is None
    assert f["contributors"][0]["evidence_truncated"]
    assert f["patterns_truncated"] and "island" not in f
    assert f["peak_to_baseline_mean_ratio"] is None
    assert result["coverage"]["unobserved_global_bucket_count"] == 2
    assert result["coverage"]["gaps_truncated"]
    assert not result["method"]["labels_used_for_detection"]


def test_broken_reconciliation_and_nonfinite_results_fail_closed():
    raw = raw_result()
    raw["patterns"][0]["record_count"] = 81
    with pytest.raises(RuntimeError, match="reconcile"):
        serialize_spikes(raw, plan())
    raw = raw_result()
    raw["pattern_counts"][0]["complete_pattern_record_count"] = 79
    with pytest.raises(RuntimeError, match="reconcile"):
        serialize_spikes(raw, plan())
    raw = raw_result()
    raw["findings"][0]["baseline_mean_count"] = float("inf")
    with pytest.raises(RuntimeError, match="non-finite"):
        serialize_spikes(raw, plan())


class Connection:
    def __init__(self):
        self.calls = []
        self.row = raw_result()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def cursor(self):
        return self

    def execute(self, sql, parameters):
        self.calls.append((sql, parameters))

    def fetchone(self):
        return deepcopy(self.row)


def test_one_statement_and_missing_summary():
    connection = Connection()
    service = QueryService(lambda: connection, SQL_DATASET_SPECS)
    assert service.detect_log_spikes()["counts"]["spike_window_count"] == 7
    assert len(connection.calls) == 1
    connection.row = None
    with pytest.raises(RuntimeError, match="summary"):
        service.detect_log_spikes()


def test_mcp_schema_defaults_strict_validation_and_response(monkeypatch):
    connection = Connection()
    monkeypatch.setattr("log_insights.mcp_server.connection_factory", lambda _: lambda: connection)

    async def run():
        async with Client(build_server(Settings(None))) as client:
            tools = (await client.list_tools()).tools
            tool = next(t for t in tools if t.name == "detect_log_spikes")
            assert len(tools) == 6
            assert not tool.input_schema.get("required")
            properties = tool.input_schema["properties"]
            for key, value in dict(dataset="bgl", interval="hour", baseline_quantile=.99,
                threshold_multiplier=1.5, min_bucket_count=20, min_baseline_buckets=100,
                baseline_scope="corpus", max_findings=10, patterns_per_finding=3, evidence_per_pattern=1).items():
                assert properties[key]["default"] == value
            result = await client.call_tool(tool.name, {})
            assert not result.is_error and result.structured_content["counts"]["spike_window_count"] == 7
            before = len(connection.calls)
            for args in ({"dataset": "openstack"}, {"baseline_quantile": True}, {"min_bucket_count": True},
                         {"max_findings": 1.1}, {"start_time": "bad"}, {"severities": [True]},
                         {"end_time": "2005-06-01T00:00:00Z"}):
                assert (await client.call_tool(tool.name, args)).is_error
            assert len(connection.calls) == before
    anyio.run(run)
