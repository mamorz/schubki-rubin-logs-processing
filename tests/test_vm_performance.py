"""Validation, SQL-scope and MCP contracts for exact spawn performance."""

from datetime import datetime, timezone
from copy import deepcopy

import anyio
from mcp import Client
import pytest

from log_insights.config import Settings
from log_insights.mcp_server import build_server
from log_insights.queries import DATASET_SPECS, SQL_DATASET_SPECS, QueryService
from log_insights.vm_performance import build_vm_spawn_plan, serialize_vm_spawn_result


def plan(**kwargs):
    return build_vm_spawn_plan(SQL_DATASET_SPECS["openstack"], **kwargs)


@pytest.mark.parametrize("scope", ["corpus", "analysis"])
@pytest.mark.parametrize("interval", ["hour", "day", None])
@pytest.mark.parametrize("threshold", [None, 0, 25.123456789])
@pytest.mark.parametrize("labels", [False, True])
def test_one_statement_full_scopes_parameterization_and_no_label_detection(scope, interval, threshold, labels):
    start, end = datetime(2017, 5, 14), datetime(2017, 5, 15)
    p = plan(start_time=start, end_time=end, baseline_scope=scope, interval=interval,
             slow_threshold_seconds=threshold, include_anomaly_labels=labels, top_limit=1, bucket_limit=1)
    assert p.sql.startswith("WITH matching_rows AS MATERIALIZED")
    assert p.sql.count("regexp_extract(") == 1
    assert p.sql.count("FROM openstack_logs\n") == 1
    assert "openstack_logs_idx WHERE message @@ ts_phrase(%(spawn_marker)s)" in p.sql
    assert "timestamp >= %(start_time)t AND timestamp < %(end_time)t" in p.sql
    assert "start_time" not in p.candidate_sql and "end_time" not in p.candidate_sql
    assert "LIMIT" not in p.candidate_sql
    assert "2066" not in p.sql and "is_anomaly =" not in p.sql
    assert "duration_seconds > seconds" in p.sql
    assert "ROUND(" not in p.sql.upper()
    assert ("is_anomaly" in p.sql) is labels
    assert p.parameters["top_limit"] == 1
    assert p.parameters["start_time"] == start
    assert p.parameters["end_time"] == end
    baseline_section = p.sql.split("baseline_summary AS MATERIALIZED (")[1].split("),\nthreshold")[0]
    assert f"FROM {'parsed' if scope == 'corpus' else 'analysis_candidates'}" in baseline_section
    assert "LIMIT" not in baseline_section and "is_anomaly" not in baseline_section
    if threshold is None:
        assert p.parameters["baseline_multiplier"] == 1.25
        assert "slow_threshold_seconds" not in p.parameters
    else:
        assert p.parameters["slow_threshold_seconds"] == threshold
        assert "baseline_multiplier" not in p.parameters
        assert "SELECT CAST(%(slow_threshold_seconds)t AS DOUBLE) AS seconds" in p.sql
    if interval is None:
        assert "date_trunc" not in p.sql and "bucket_limit" not in p.parameters
    else:
        assert f"date_trunc('{interval}', timestamp)" in p.sql
        assert "LIMIT %(bucket_limit)t" in p.sql
        assert p.parameters["bucket_limit"] == 1


@pytest.mark.parametrize("args", [
    {"slow_threshold_seconds": -1}, {"slow_threshold_seconds": True},
    {"slow_threshold_seconds": float("nan")}, {"slow_threshold_seconds": float("inf")},
    {"slow_threshold_seconds": "25' OR 1=1 --"}, {"slow_threshold_seconds": 10**1000},
    {"baseline_multiplier": 0}, {"baseline_multiplier": 1}, {"baseline_multiplier": True},
    {"baseline_multiplier": float("inf")}, {"baseline_multiplier": float("nan")},
    {"baseline_scope": "healthy"}, {"baseline_scope": "corpus'; SELECT 1; --"},
    {"dataset": "openstack'; SELECT 1; --"}, {"dataset": None},
    {"interval": "week"}, {"interval": "day'; SELECT 1; --"}, {"interval": True},
    {"top_limit": 0}, {"top_limit": 51}, {"top_limit": True}, {"top_limit": 1.5},
    {"bucket_limit": 0}, {"bucket_limit": 101}, {"bucket_limit": True},
    {"sort_direction": "desc; SELECT 1; --"}, {"sort_direction": None},
    {"include_anomaly_labels": 1}, {"include_anomaly_labels": None},
    {"start_time": "2017-05-14"}, {"end_time": 1},
    {"start_time": datetime(2017, 5, 14, tzinfo=timezone.utc)},
    {"start_time": datetime(2017, 5, 15), "end_time": datetime(2017, 5, 14)},
    {"start_time": datetime(2017, 5, 14), "end_time": datetime(2017, 5, 14)},
])
def test_invalid_requests_never_open_database(args):
    def never_connect():
        pytest.fail("invalid performance request reached database")
    with pytest.raises(ValueError):
        QueryService(never_connect, SQL_DATASET_SPECS).analyze_vm_spawn_performance(**args)


@pytest.mark.parametrize("specs,dataset", [(SQL_DATASET_SPECS, "bgl"), (DATASET_SPECS, "openstack"), (DATASET_SPECS, "bgl")])
def test_unsupported_backend(specs, dataset):
    with pytest.raises(ValueError, match="SQL-backed OpenStack"):
        QueryService(lambda: pytest.fail("unsupported backend connected"), specs).analyze_vm_spawn_performance(dataset=dataset)


def summary(n=100, invalid=0):
    return dict(candidate_record_count=n+invalid, spawn_event_count=n, invalid_duration_record_count=invalid,
                unique_instance_count=n, missing_instance_id_event_count=0,
                mean_seconds=20.0 if n else None, median_seconds=20.0 if n else None,
                p95_seconds=20.0 if n else None, p99_seconds=20.0 if n else None,
                min_seconds=20.0 if n else None, max_seconds=20.0 if n else None,
                observed_start="2017-05-14 20:00:00" if n else None,
                observed_end="2017-05-14 21:00:00" if n else None)


def result_row(n=100, slow=5):
    return dict(summary=summary(n), baseline=summary(n),
                detection=dict(effective_threshold_seconds=25.0 if n else None, slow_event_count=slow if n else None),
                events=[dict(record_id=1, instance_id=None, timestamp="2017-05-14 20:00:00", duration_seconds=30.,
                             component="nova.compute.manager", is_slow=True)] if n else None,
                buckets=None, total_bucket_count=3 if n else 0)


def test_serialization_counts_complete_population_and_normalizes_timestamps():
    data = serialize_vm_spawn_result(result_row(), plan(top_limit=1, bucket_limit=1))
    assert data["counts"]["spawn_event_count"] == 100
    assert data["detection"]["slow_event_count"] == 5
    assert data["detection"]["slow_event_percentage"] == 5.0
    assert data["ranking"]["returned_event_count"] == 1
    assert data["ranking"]["truncated"] and data["ranking"]["slow_events_truncated"]
    assert data["time_series"]["truncated"]
    assert data["slowest_spawn_events"][0]["instance_id"] is None
    assert data["slowest_spawn_events"][0]["timestamp"] == "2017-05-14T20:00:00"
    assert data["scope"]["complete_population_aggregated"]


def test_empty_population_has_no_fabricated_statistics_or_detection():
    data = serialize_vm_spawn_result(result_row(0), plan())
    assert data["counts"]["spawn_event_count"] == 0
    assert all(v is None for v in data["statistics"].values())
    assert data["detection"]["status"] == "unavailable_baseline"
    assert data["detection"]["slow_event_count"] is None
    assert data["detection"]["slow_event_percentage"] is None
    assert data["slowest_spawn_events"] == data["time_series"]["buckets"] == []


def test_nonfinite_results_cannot_be_reported():
    raw = result_row()
    raw["summary"]["mean_seconds"] = float("inf")
    with pytest.raises(RuntimeError, match="non-finite"):
        serialize_vm_spawn_result(raw, plan())


class Connection:
    def __init__(self):
        self.calls = []
        self.row = result_row()

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


def test_service_executes_one_complete_statement_and_requires_a_result():
    connection = Connection()
    service = QueryService(lambda: connection, SQL_DATASET_SPECS)
    assert service.analyze_vm_spawn_performance()["detection"]["slow_event_count"] == 5
    assert len(connection.calls) == 1
    connection.row = None
    with pytest.raises(RuntimeError, match="summary"):
        service.analyze_vm_spawn_performance()


def test_mcp_schema_defaults_validation_and_functionality(monkeypatch):
    connection = Connection()
    monkeypatch.setattr("log_insights.mcp_server.connection_factory", lambda _: lambda: connection)
    async def run():
        async with Client(build_server(Settings(None))) as client:
            tool = next(t for t in (await client.list_tools()).tools if t.name == "analyze_vm_spawn_performance")
            assert not tool.input_schema.get("required")
            properties = tool.input_schema["properties"]
            for field, default in dict(dataset="openstack", baseline_scope="corpus", baseline_multiplier=1.25,
                                       slow_threshold_seconds=None, top_limit=10, bucket_limit=100,
                                       interval="day", sort_direction="desc", include_anomaly_labels=False).items():
                assert properties[field]["default"] == default
            response = await client.call_tool(tool.name, {})
            assert not response.is_error
            assert response.structured_content["counts"]["spawn_event_count"] == 100
            assert response.structured_content["provenance"]["statement_count"] == 1
            before = len(connection.calls)
            for args in ({"slow_threshold_seconds": True}, {"top_limit": True}, {"top_limit": 1.5},
                         {"include_anomaly_labels": 1}, {"dataset": "bgl"}, {"start_time": "bad"},
                         {"baseline_scope": "healthy"}, {"slow_threshold_seconds": -1}):
                assert (await client.call_tool(tool.name, args)).is_error
            assert len(connection.calls) == before
    anyio.run(run)
