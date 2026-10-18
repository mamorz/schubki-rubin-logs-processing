from datetime import datetime, timezone

import pytest

from log_insights.ingestion import (
    BGL_SEARCH_INDEX,
    BGL_TABLE,
    OPENSTACK_SEARCH_INDEX,
    OPENSTACK_TABLE,
)
from log_insights.queries import (
    MAX_NEARBY_EVENTS,
    MAX_SEARCH_LIMIT,
    MAX_SUMMARY_LIMIT,
    _add_time_gaps,
    _mapping_coverage,
    build_aggregation_query_plan,
    build_nearby_query_plan,
    build_search_query_plan,
)
from log_insights.ownership import OWNERSHIP_TABLE


def test_builds_allowlisted_parameterized_bgl_query() -> None:
    malicious_text = "core' OR 1=1 --"
    malicious_component = "KERNEL'; DROP TABLE logs; --"
    start = datetime(2005, 6, 5, 0, 0)
    end = datetime(2005, 6, 6, 0, 0)

    plan = build_search_query_plan(
        dataset=" BGL ",
        text=malicious_text,
        start_time=start,
        end_time=end,
        component=malicious_component,
        severity="INFO",
        limit=7,
    )

    assert plan.dataset == "bgl"
    assert f"FROM {BGL_SEARCH_INDEX}" in plan.sql
    assert "message @@ %(text)s" in plan.sql
    assert "event_time >= %(start_time)t" in plan.sql
    assert "event_time < %(end_time)t" in plan.sql
    assert "component = %(component)s" in plan.sql
    assert "severity = %(severity)s" in plan.sql
    assert malicious_text not in plan.sql
    assert malicious_component not in plan.sql
    assert plan.parameters == {
        "text": malicious_text,
        "start_time": start,
        "end_time": end,
        "component": malicious_component,
        "severity": "INFO",
        "fetch_limit": 8,
    }
    assert "fetch_limit" not in plan.count_parameters


def test_builds_openstack_query_with_default_limit() -> None:
    plan = build_search_query_plan(dataset="OpenStack", text="instance")

    assert plan.dataset == "openstack"
    assert f"FROM {OPENSTACK_SEARCH_INDEX}" in plan.sql
    assert plan.parameters == {"text": "instance", "fetch_limit": 21}
    assert plan.applied_filters["result_limit"] == 20


def test_accepts_maximum_limit_and_fetches_one_extra_row() -> None:
    plan = build_search_query_plan(
        dataset="bgl", text="core", limit=MAX_SEARCH_LIMIT
    )

    assert plan.parameters["fetch_limit"] == MAX_SEARCH_LIMIT + 1


@pytest.mark.parametrize("dataset", ["", "unknown", "bgl; DROP TABLE x"])
def test_rejects_unknown_dataset(dataset: str) -> None:
    with pytest.raises(ValueError, match="dataset must be one of"):
        build_search_query_plan(dataset=dataset, text="core")


@pytest.mark.parametrize("text", ["", "   ", "\x00"])
def test_rejects_invalid_full_text_query(text: str) -> None:
    with pytest.raises(ValueError):
        build_search_query_plan(dataset="bgl", text=text)


@pytest.mark.parametrize("limit", [0, -1, MAX_SEARCH_LIMIT + 1, True, 2.5])
def test_rejects_invalid_limit(limit: object) -> None:
    with pytest.raises(ValueError, match="limit"):
        build_search_query_plan(dataset="bgl", text="core", limit=limit)  # type: ignore[arg-type]


def test_rejects_blank_optional_filter() -> None:
    with pytest.raises(ValueError, match="component must not be empty"):
        build_search_query_plan(dataset="bgl", text="core", component="  ")


def test_rejects_reversed_timestamp_range() -> None:
    timestamp = datetime(2017, 5, 16, 0, 0)
    with pytest.raises(ValueError, match="earlier than"):
        build_search_query_plan(
            dataset="openstack",
            text="instance",
            start_time=timestamp,
            end_time=timestamp,
        )


def test_rejects_timezone_aware_timestamp() -> None:
    with pytest.raises(ValueError, match="must not include a timezone"):
        build_search_query_plan(
            dataset="openstack",
            text="instance",
            start_time=datetime(2017, 5, 16, tzinfo=timezone.utc),
        )


def test_builds_parameterized_component_aggregation() -> None:
    malicious_severity = "ERROR'; DROP TABLE logs; --"

    plan = build_aggregation_query_plan(
        dataset="BGL",
        group_by="component",
        severity=malicious_severity,
        limit=5,
    )

    assert plan.dataset == "bgl"
    assert plan.aggregation_type == "grouped_count"
    assert f"FROM {BGL_TABLE}" in plan.sql
    assert "SELECT\n    component," in plan.sql
    assert "GROUP BY component" in plan.sql
    assert "ORDER BY record_count DESC, component" in plan.sql
    assert malicious_severity not in plan.sql
    assert plan.parameters == {
        "severity": malicious_severity,
        "fetch_limit": 6,
    }
    assert plan.count_parameters == {"severity": malicious_severity}


def test_builds_event_id_aggregation_on_openstack() -> None:
    plan = build_aggregation_query_plan(
        dataset="openstack",
        group_by="event_id",
        component="nova.compute.manager",
    )

    assert f"FROM {OPENSTACK_TABLE}" in plan.sql
    assert "GROUP BY event_id" in plan.sql
    assert plan.parameters["component"] == "nova.compute.manager"


def test_builds_team_aggregation_with_left_join_and_unmapped_fallback() -> None:
    plan = build_aggregation_query_plan(
        dataset="openstack",
        group_by="team",
        severity="ERROR",
        limit=4,
    )

    assert f"LEFT JOIN {OWNERSHIP_TABLE} AS o" in plan.sql
    assert "o.dataset = %(ownership_dataset)s" in plan.sql
    assert "o.component = l.component" in plan.sql
    assert "COALESCE(o.team, 'UNMAPPED') AS team" in plan.sql
    assert "l.severity = %(severity)s" in plan.sql
    assert "GROUP BY COALESCE(o.team, 'UNMAPPED')" in plan.sql
    assert plan.parameters == {
        "severity": "ERROR",
        "ownership_dataset": "openstack",
        "fetch_limit": 5,
    }
    assert plan.count_parameters == {"severity": "ERROR"}
    assert plan.coverage_sql is not None
    assert "COUNT(o.component) AS mapped_record_count" in plan.coverage_sql
    assert "COUNT(DISTINCT l.component) AS total_component_count" in plan.coverage_sql
    assert plan.coverage_parameters == {
        "severity": "ERROR",
        "ownership_dataset": "openstack",
    }
    assert "LEFT JOIN" not in plan.count_sql
    assert "l.severity" not in plan.count_sql


def test_mapping_coverage_reports_records_components_and_empty_scope() -> None:
    assert _mapping_coverage(
        {
            "total_record_count": 10,
            "mapped_record_count": 8,
            "total_component_count": 4,
            "mapped_component_count": 3,
        }
    ) == {
        "total_record_count": 10,
        "mapped_record_count": 8,
        "unmapped_record_count": 2,
        "mapped_record_percentage": 80.0,
        "total_component_count": 4,
        "mapped_component_count": 3,
        "unmapped_component_count": 1,
        "mapped_component_percentage": 75.0,
    }
    assert _mapping_coverage(
        {
            "total_record_count": 0,
            "mapped_record_count": 0,
            "total_component_count": 0,
            "mapped_component_count": 0,
        }
    )["mapped_record_percentage"] == 0.0


@pytest.mark.parametrize("interval", ["hour", "day"])
def test_builds_allowlisted_time_bucket_aggregation(interval: str) -> None:
    plan = build_aggregation_query_plan(
        dataset="bgl", group_by="time", interval=interval, limit=2
    )

    assert plan.aggregation_type == "time_bucket_count"
    assert f"date_trunc('{interval}', event_time)" in plan.sql
    assert "ORDER BY bucket_start" in plan.sql
    assert plan.parameters["fetch_limit"] == 3


@pytest.mark.parametrize("group_by", ["", "message", "component; DROP TABLE x"])
def test_rejects_unapproved_summary_grouping(group_by: str) -> None:
    with pytest.raises(ValueError, match="group_by must be one of"):
        build_aggregation_query_plan(dataset="bgl", group_by=group_by)


def test_requires_interval_for_time_grouping() -> None:
    with pytest.raises(ValueError, match="interval is required"):
        build_aggregation_query_plan(dataset="bgl", group_by="time")


def test_rejects_interval_for_non_time_grouping() -> None:
    with pytest.raises(ValueError, match="only valid"):
        build_aggregation_query_plan(
            dataset="bgl", group_by="component", interval="hour"
        )


def test_rejects_unapproved_time_interval() -> None:
    with pytest.raises(ValueError, match="interval must be one of"):
        build_aggregation_query_plan(
            dataset="bgl", group_by="time", interval="week"
        )


@pytest.mark.parametrize("limit", [0, MAX_SUMMARY_LIMIT + 1, True, 1.5])
def test_rejects_invalid_summary_limit(limit: object) -> None:
    with pytest.raises(ValueError, match="limit"):
        build_aggregation_query_plan(
            dataset="bgl",
            group_by="severity",
            limit=limit,  # type: ignore[arg-type]
        )


def test_summary_limit_does_not_enter_full_scope_count_query() -> None:
    plan = build_aggregation_query_plan(
        dataset="openstack",
        group_by="severity",
        limit=MAX_SUMMARY_LIMIT,
    )

    assert plan.parameters["fetch_limit"] == MAX_SUMMARY_LIMIT + 1
    assert "LIMIT" not in plan.count_sql
    assert "fetch_limit" not in plan.count_parameters


def test_builds_deterministic_parameterized_nearby_queries() -> None:
    plan = build_nearby_query_plan(
        dataset="BGL", record_id=12, before=5, after=7
    )

    assert plan.dataset == "bgl"
    assert f"FROM {BGL_TABLE}" in plan.anchor_sql
    assert plan.anchor_parameters == {"record_id": 12}
    assert "event_time < %(anchor_time)s" in plan.before_sql
    assert "event_time = %(anchor_time)s AND line_id < %(record_id)s" in plan.before_sql
    assert "ORDER BY event_time DESC, line_id DESC" in plan.before_sql
    assert "event_time > %(anchor_time)s" in plan.after_sql
    assert "event_time = %(anchor_time)s AND line_id > %(record_id)s" in plan.after_sql
    assert "ORDER BY event_time, line_id" in plan.after_sql
    assert "component =" not in plan.before_sql
    assert plan.before == 5
    assert plan.after == 7


def test_same_component_nearby_query_uses_parameter_placeholder() -> None:
    plan = build_nearby_query_plan(
        dataset="openstack",
        record_id=7,
        same_component=True,
    )

    assert f"FROM {OPENSTACK_TABLE}" in plan.before_sql
    assert "component = %(component)s" in plan.before_sql
    assert "component = %(component)s" in plan.after_sql


@pytest.mark.parametrize("record_id", [0, -1, True, "1", 1.5])
def test_rejects_invalid_nearby_record_id(record_id: object) -> None:
    with pytest.raises(ValueError, match="record_id"):
        build_nearby_query_plan(
            dataset="bgl", record_id=record_id  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("field", ["before", "after"])
@pytest.mark.parametrize("value", [-1, MAX_NEARBY_EVENTS + 1, True, 1.5])
def test_rejects_invalid_neighbor_counts(field: str, value: object) -> None:
    arguments = {"dataset": "bgl", "record_id": 1, field: value}
    with pytest.raises(ValueError, match=field):
        build_nearby_query_plan(**arguments)  # type: ignore[arg-type]


def test_accepts_zero_and_maximum_neighbor_counts() -> None:
    plan = build_nearby_query_plan(
        dataset="bgl",
        record_id=1,
        before=0,
        after=MAX_NEARBY_EVENTS,
    )

    assert plan.before == 0
    assert plan.after == MAX_NEARBY_EVENTS


def test_rejects_non_boolean_same_component() -> None:
    with pytest.raises(ValueError, match="same_component"):
        build_nearby_query_plan(
            dataset="bgl",
            record_id=1,
            same_component="true",  # type: ignore[arg-type]
        )


def test_time_gap_annotations_handle_equal_timestamps() -> None:
    timestamp = datetime(2017, 5, 16, 0, 0)
    earlier = {
        "record_id": 1,
        "timestamp": timestamp,
    }
    anchor = {
        "record_id": 2,
        "timestamp": timestamp,
    }
    later = {
        "record_id": 3,
        "timestamp": datetime(2017, 5, 16, 0, 0, 2),
    }

    timeline = _add_time_gaps([earlier], anchor, [later])

    assert timeline[0]["seconds_from_anchor"] == 0.0
    assert timeline[0]["gap_from_previous_seconds"] is None
    assert timeline[1]["seconds_from_anchor"] == 0.0
    assert timeline[1]["gap_from_previous_seconds"] == 0.0
    assert timeline[2]["seconds_from_anchor"] == 2.0
    assert timeline[2]["gap_from_previous_seconds"] == 2.0
