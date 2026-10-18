"""Full-population hypervisor spawn analytics; no writes or label-based detection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import math
from typing import Any, Mapping, TYPE_CHECKING

from .db import ConnectionFactory

if TYPE_CHECKING:
    from .queries import DatasetSpec

SPAWN_MARKER = "spawn the instance on the hypervisor"
SPAWN_COMPONENT = "nova.compute.manager"
PARSER_VERSION = "hypervisor-spawn-seconds-v1"
DURATION_PATTERN = (
    r"^(?:\[instance: [^\]]+\]\s+)?Took ([0-9]+(?:\.[0-9]+)?) "
    r"seconds to spawn the instance on the hypervisor\.?$"
)
VALID_DURATION = "duration_seconds IS NOT NULL AND isfinite(duration_seconds) AND duration_seconds >= 0"
COUNT_FIELDS = (
    "candidate_record_count", "spawn_event_count", "invalid_duration_record_count",
    "unique_instance_count", "missing_instance_id_event_count",
)
STAT_FIELDS = ("mean_seconds", "median_seconds", "p95_seconds", "p99_seconds", "min_seconds", "max_seconds")


@dataclass(frozen=True)
class VmSpawnPlan:
    sql: str
    parameters: Mapping[str, Any]
    applied_filters: Mapping[str, Any]
    candidate_sql: str
    table: str
    scope_label: str


def _number(value: Any, name: str, *, minimum: float, exclusive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    try:
        result = float(value)
    except OverflowError as error:
        raise ValueError(f"{name} must be a finite number") from error
    if not math.isfinite(result):
        raise ValueError(f"{name} must be a finite number")
    if result < minimum or (exclusive and result == minimum):
        comparison = "greater than" if exclusive else "at least"
        raise ValueError(f"{name} must be {comparison} {minimum:g}")
    return result


def _enum(value: Any, name: str, choices: tuple[str, ...]) -> str:
    if not isinstance(value, str) or value.strip().lower() not in choices:
        raise ValueError(f"{name} must be one of: {', '.join(choices)}")
    return value.strip().lower()


def _limit(value: Any, name: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValueError(f"{name} must be an integer between 1 and {maximum}")
    return value


def _summary_columns() -> str:
    """Same exact aggregates for global, baseline and bucket populations."""
    return f"""COUNT(*) AS candidate_record_count,
    COUNT(*) FILTER (WHERE {VALID_DURATION}) AS spawn_event_count,
    COUNT(*) - COUNT(*) FILTER (WHERE {VALID_DURATION}) AS invalid_duration_record_count,
    COUNT(DISTINCT instance_id) FILTER (WHERE {VALID_DURATION}) AS unique_instance_count,
    COUNT(*) FILTER (WHERE ({VALID_DURATION}) AND instance_id IS NULL) AS missing_instance_id_event_count,
    AVG(duration_seconds ORDER BY record_id) FILTER (WHERE {VALID_DURATION}) AS mean_seconds,
    quantile_cont(duration_seconds, 0.5) FILTER (WHERE {VALID_DURATION}) AS median_seconds,
    quantile_cont(duration_seconds, 0.95) FILTER (WHERE {VALID_DURATION}) AS p95_seconds,
    quantile_cont(duration_seconds, 0.99) FILTER (WHERE {VALID_DURATION}) AS p99_seconds,
    MIN(duration_seconds) FILTER (WHERE {VALID_DURATION}) AS min_seconds,
    MAX(duration_seconds) FILTER (WHERE {VALID_DURATION}) AS max_seconds,
    MIN(timestamp) AS observed_start,
    MAX(timestamp) AS observed_end"""


def build_vm_spawn_plan(
    spec: DatasetSpec, *, start_time: datetime | None = None,
    end_time: datetime | None = None, slow_threshold_seconds: float | None = None,
    baseline_multiplier: float = 1.25, baseline_scope: str = "corpus",
    interval: str | None = "day", top_limit: int = 10, bucket_limit: int = 100,
    sort_direction: str = "desc", include_anomaly_labels: bool = False,
) -> VmSpawnPlan:
    """Build one SELECT. Relation/column expressions come only from DatasetSpec."""
    if spec.name != "openstack" or not spec.supports_openstack_tracing:
        raise ValueError("analyze_vm_spawn_performance requires SQL-backed OpenStack (MCP_DATASET_BACKEND=sql)")
    for name, value in (("start_time", start_time), ("end_time", end_time)):
        if value is not None and (not isinstance(value, datetime) or value.utcoffset() is not None):
            raise ValueError(f"{name} must be a datetime without timezone information")
    if start_time is not None and end_time is not None and start_time >= end_time:
        raise ValueError("start_time must be earlier than end_time")
    if slow_threshold_seconds is not None:
        slow_threshold_seconds = _number(slow_threshold_seconds, "slow_threshold_seconds", minimum=0)
    baseline_multiplier = _number(baseline_multiplier, "baseline_multiplier", minimum=1, exclusive=True)
    baseline_scope = _enum(baseline_scope, "baseline_scope", ("corpus", "analysis"))
    interval = None if interval is None else _enum(interval, "interval", ("hour", "day"))
    sort_direction = _enum(sort_direction, "sort_direction", ("asc", "desc"))
    top_limit, bucket_limit = _limit(top_limit, "top_limit", 50), _limit(bucket_limit, "bucket_limit", 100)
    if not isinstance(include_anomaly_labels, bool):
        raise ValueError("include_anomaly_labels must be a boolean")

    parameters: dict[str, Any] = {
        "spawn_marker": SPAWN_MARKER, "spawn_component": SPAWN_COMPONENT,
        "duration_pattern": DURATION_PATTERN, "top_limit": top_limit,
    }
    predicates = []
    for name, operator, value in (("start_time", ">=", start_time), ("end_time", "<", end_time)):
        if value is not None:
            # Filter typed base-table timestamps, never the inverted relation.
            predicates.append(f"timestamp {operator} %({name})t")
            parameters[name] = value
    analysis_where = " AND ".join(predicates) if predicates else "TRUE"
    label_column = ", is_anomaly" if include_anomaly_labels else ""
    candidate_sql = f"""SELECT {spec.id_expression} AS record_id,
    {spec.time_column} AS timestamp, NULLIF(instance_id, '') AS instance_id,
    component, message{label_column}
FROM {spec.table}
WHERE component = %(spawn_component)s
    AND {spec.id_expression} IN (SELECT {spec.id_expression}
        FROM {spec.index_relation} WHERE message @@ ts_phrase(%(spawn_marker)s))"""
    baseline_source = "parsed" if baseline_scope == "corpus" else "analysis_candidates"
    if slow_threshold_seconds is not None:
        threshold_expression = "CAST(%(slow_threshold_seconds)t AS DOUBLE)"
        parameters["slow_threshold_seconds"] = slow_threshold_seconds
    else:
        product = "p95_seconds * CAST(%(baseline_multiplier)t AS DOUBLE)"
        threshold_expression = f"CASE WHEN p95_seconds > 0 AND isfinite({product}) THEN {product} ELSE NULL END"
        parameters["baseline_multiplier"] = baseline_multiplier

    bucket_ctes = ""
    bucket_select = "NULL::JSON AS buckets, 0 AS total_bucket_count"
    if interval is not None:
        # interval/direction select code-owned SQL, never arbitrary caller text.
        direction = {"asc": "ASC", "desc": "DESC"}[sort_direction]
        parameters["bucket_limit"] = bucket_limit
        bucket_ctes = f""",
bucket_summary AS MATERIALIZED (
    SELECT date_trunc('{interval}', timestamp) AS bucket_start,
    {_summary_columns()},
    CASE WHEN seconds IS NULL THEN NULL ELSE
        COUNT(*) FILTER (WHERE ({VALID_DURATION}) AND duration_seconds > seconds) END AS slow_event_count
    FROM analysis_candidates CROSS JOIN threshold
    GROUP BY bucket_start, seconds
),
bucket_results AS (
    SELECT *, CASE WHEN spawn_event_count = 0 OR slow_event_count IS NULL THEN NULL
        ELSE 100.0 * slow_event_count / spawn_event_count END AS slow_event_percentage
    FROM bucket_summary
    ORDER BY bucket_start {direction}
    LIMIT %(bucket_limit)t
)"""
        bucket_select = f"""(SELECT to_json(list(b ORDER BY bucket_start {direction})) FROM bucket_results b) AS buckets,
    (SELECT COUNT(*) FROM bucket_summary) AS total_bucket_count"""

    sql = f"""WITH matching_rows AS MATERIALIZED (
{candidate_sql}
),
parsed AS MATERIALIZED (
    SELECT *, TRY_CAST(NULLIF(regexp_extract(message, %(duration_pattern)s, 1), '') AS DOUBLE) AS duration_seconds
    FROM matching_rows
),
analysis_candidates AS MATERIALIZED (
    SELECT * FROM parsed WHERE {analysis_where}
),
analysis_valid AS MATERIALIZED (
    SELECT * FROM analysis_candidates WHERE {VALID_DURATION}
),
analysis_summary AS MATERIALIZED (
    SELECT {_summary_columns()} FROM analysis_candidates
),
baseline_summary AS MATERIALIZED (
    SELECT {_summary_columns()} FROM {baseline_source}
),
threshold AS MATERIALIZED (
    SELECT {threshold_expression} AS seconds FROM baseline_summary
),
detection AS (
    SELECT seconds AS effective_threshold_seconds,
        CASE WHEN seconds IS NULL THEN NULL ELSE
            (SELECT COUNT(*) FROM analysis_valid WHERE duration_seconds > seconds) END AS slow_event_count
    FROM threshold
),
ranked AS (
    SELECT record_id, timestamp, instance_id, component, duration_seconds,
        duration_seconds > seconds AS is_slow{label_column}
    FROM analysis_valid CROSS JOIN threshold
    ORDER BY duration_seconds DESC, timestamp, record_id
    LIMIT %(top_limit)t
){bucket_ctes}
SELECT
    (SELECT to_json(s) FROM analysis_summary s) AS summary,
    (SELECT to_json(b) FROM baseline_summary b) AS baseline,
    (SELECT to_json(d) FROM detection d) AS detection,
    (SELECT to_json(list(r ORDER BY duration_seconds DESC, timestamp, record_id)) FROM ranked r) AS events,
    {bucket_select}"""
    return VmSpawnPlan(sql=sql, parameters=parameters, candidate_sql=candidate_sql,
                       table=spec.table, scope_label=spec.scope_label,
                       applied_filters=dict(dataset=spec.name, start_time=start_time, end_time=end_time,
                           slow_threshold_seconds=slow_threshold_seconds, baseline_multiplier=baseline_multiplier,
                           baseline_scope=baseline_scope, interval=interval, top_limit=top_limit,
                           bucket_limit=bucket_limit, sort_direction=sort_direction,
                           include_anomaly_labels=include_anomaly_labels))


def _json_value(value: Any, key: str = "") -> Any:
    if isinstance(value, Mapping):
        return {k: _json_value(v, k) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str) and key in {"timestamp", "bucket_start", "observed_start", "observed_end"}:
        return datetime.fromisoformat(value).isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        raise RuntimeError("SereneDB returned a non-finite performance aggregate; cannot report reliable statistics")
    return value


def serialize_vm_spawn_result(row: Mapping[str, Any], plan: VmSpawnPlan) -> dict[str, Any]:
    """Format SQL results; only bounded output counts/flags are calculated here."""
    summary, baseline, detection = row["summary"], row["baseline"], dict(row["detection"])
    events, buckets = row["events"] or [], row["buckets"] or []
    filters = plan.applied_filters
    absolute = filters["slow_threshold_seconds"] is not None
    threshold = detection["effective_threshold_seconds"]
    slow_count, event_count = detection["slow_event_count"], summary["spawn_event_count"]
    # These scalar ratios use complete SQL counts, never the ranked sample.
    detection.update(method="absolute" if absolute else "baseline_p95_multiplier",
                     baseline_multiplier=None if absolute else filters["baseline_multiplier"],
                     comparison="duration_seconds > effective_threshold_seconds (unrounded)",
                     status="ready" if threshold is not None else "unavailable_baseline",
                     slow_event_percentage=None if not event_count or slow_count is None else 100.0 * slow_count / event_count)
    returned_slow = sum(event["is_slow"] is True for event in events)
    warnings = []
    if not event_count:
        warnings.append("No valid measured spawn events in the analysis range; duration statistics are null.")
    if summary["invalid_duration_record_count"]:
        warnings.append("Malformed or non-finite durations were excluded; see counts for the full excluded population.")
    if threshold is None:
        warnings.append("Relative detection is unavailable: baseline P95 is empty, nonpositive, or produces a non-finite threshold.")
    if not absolute and baseline["spawn_event_count"] < 20:
        warnings.append("The empirical baseline has fewer than 20 valid events; tail percentiles have limited support.")
    if filters["baseline_scope"] == "corpus":
        warnings.append("The corpus baseline is retrospective and includes all loaded periods and stored label values.")
    response = {
        "scope": dict(dataset="openstack", table=plan.table, source=plan.scope_label,
                      start_time=filters["start_time"], end_time=filters["end_time"],
                      observed_start=summary["observed_start"], observed_end=summary["observed_end"],
                      timestamp_bounds="start inclusive, end exclusive; naive source-log time",
                      metric="reported hypervisor spawn duration, not total boot/build or HTTP latency",
                      count_unit="spawn-duration log events, not deduplicated operations or unique VMs",
                      complete_population_aggregated=True),
        "counts": {k: summary[k] for k in COUNT_FIELDS},
        "statistics": {k: summary[k] for k in STAT_FIELDS},
        "baseline": dict(scope=filters["baseline_scope"], **baseline),
        "detection": detection,
        "slowest_spawn_events": events,
        "ranking": dict(top_limit=filters["top_limit"], returned_event_count=len(events),
                        total_event_count=event_count, truncated=event_count > len(events),
                        returned_slow_event_count=returned_slow,
                        slow_events_truncated=None if slow_count is None else slow_count > returned_slow,
                        order="duration_seconds DESC, timestamp ASC, record_id ASC"),
        "time_series": dict(enabled=filters["interval"] is not None, interval=filters["interval"],
                            sort_direction=filters["sort_direction"], buckets=buckets,
                            bucket_limit=filters["bucket_limit"], returned_bucket_count=len(buckets),
                            total_bucket_count=int(row["total_bucket_count"]),
                            truncated=int(row["total_bucket_count"]) > len(buckets),
                            empty_periods_included=False, timestamp_basis="completion-log timestamp",
                            boundary_buckets_may_be_partial=filters["start_time"] is not None or filters["end_time"] is not None),
        "anomaly_labels": dict(included=filters["include_anomaly_labels"],
                               source="openstack_logs.is_anomaly" if filters["include_anomaly_labels"] else None,
                               used_for_detection=False),
        "warnings": warnings,
        "limitations": ["Only observed duration messages are measured; missing or unfinished spawns are not reconstructed.",
                        "A general empirical baseline is not a healthy-only baseline or proof of a root cause."],
        "provenance": dict(executed_sql={"analysis": plan.sql}, parameters={"analysis": dict(plan.parameters)},
                           applied_filters=dict(filters), parser_version=PARSER_VERSION,
                           percentile_method="quantile_cont on DOUBLE durations; continuous inclusive interpolation over every valid event",
                           statement_count=1, candidate_marker=SPAWN_MARKER, component=SPAWN_COMPONENT),
    }
    return _json_value(response)


def execute_vm_spawn_plan(connections: ConnectionFactory, plan: VmSpawnPlan) -> dict[str, Any]:
    with connections() as connection, connection.cursor() as cursor:
        cursor.execute(plan.sql, plan.parameters)
        row = cursor.fetchone()
    if row is None:
        raise RuntimeError("SereneDB did not return a spawn-performance summary")
    return serialize_vm_spawn_result(row, plan)
