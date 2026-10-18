"""SELECT-only BGL spike screening with complete counts and bounded evidence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import math
from typing import Any, Mapping, TYPE_CHECKING

from .db import ConnectionFactory

if TYPE_CHECKING:
    from .queries import DatasetSpec

DEFAULT_SEVERITIES = ("WARNING", "ERROR", "SEVERE", "FAILURE", "FATAL")
BGL_SEVERITIES = ("INFO", *DEFAULT_SEVERITIES)
METHOD_VERSION = "component-active-empirical-tail-v1"
DIAGNOSTIC_LIMIT = 20
BASELINE_LIMIT = 100
MESSAGE_LIMIT = 1000


@dataclass(frozen=True)
class SpikePlan:
    sql: str
    parameters: Mapping[str, Any]
    applied_filters: Mapping[str, Any]
    source_sql: str
    table: str
    scope_label: str


def _choice(value: Any, name: str, choices: tuple[str, ...]) -> str:
    if not isinstance(value, str) or value not in choices:
        raise ValueError(f"{name} must be one of: {', '.join(choices)}")
    return value


def _integer(value: Any, name: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValueError(f"{name} must be an integer between 1 and {maximum}")
    return value


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    try:
        result = float(value)
    except OverflowError as error:
        raise ValueError(f"{name} must be a finite number") from error
    if not math.isfinite(result):
        raise ValueError(f"{name} must be a finite number")
    return result


def _time_range(start: datetime | None, end: datetime | None, prefix: str) -> None:
    for field, value in (("start_time", start), ("end_time", end)):
        if value is not None and (not isinstance(value, datetime) or value.utcoffset() is not None):
            raise ValueError(f"{prefix}{field} must be a datetime without timezone information")
    if start is not None and end is not None and start >= end:
        raise ValueError(f"{prefix}start_time must be earlier than {prefix}end_time")


def build_spike_plan(
    spec: DatasetSpec, *, start_time: datetime | None = None, end_time: datetime | None = None,
    component: str | None = None, interval: str = "hour", signal: str = "severity_volume",
    severities: list[str] | None = None, baseline_scope: str = "corpus",
    baseline_start_time: datetime | None = None, baseline_end_time: datetime | None = None,
    baseline_quantile: float = 0.99, threshold_multiplier: float = 1.5,
    min_bucket_count: int = 20, min_baseline_buckets: int = 100, max_findings: int = 10,
    patterns_per_finding: int = 3, evidence_per_pattern: int = 1,
) -> SpikePlan:
    if spec.name != "bgl" or not spec.supports_bgl_spikes:
        raise ValueError("detect_log_spikes requires SQL-backed BGL (MCP_DATASET_BACKEND=sql)")
    interval = _choice(interval, "interval", ("hour", "day"))
    signal = _choice(signal, "signal", ("severity_volume", "total_volume"))
    baseline_scope = _choice(baseline_scope, "baseline_scope", ("corpus", "reference"))
    _time_range(start_time, end_time, "")
    _time_range(baseline_start_time, baseline_end_time, "baseline_")
    if baseline_scope == "corpus":
        if baseline_start_time is not None or baseline_end_time is not None:
            raise ValueError("baseline_start_time/baseline_end_time require baseline_scope='reference'")
    else:
        if any(v is None for v in (start_time, end_time, baseline_start_time, baseline_end_time)):
            raise ValueError("reference requires explicit analysis and baseline start_time/end_time ranges")
        if not (baseline_end_time <= start_time or end_time <= baseline_start_time):
            raise ValueError("reference and analysis ranges must not overlap")
    if component is not None:
        if not isinstance(component, str) or not component.strip() or len(component) > 256:
            raise ValueError("component must be a non-empty string of at most 256 characters")
        component = component.strip()
    if signal == "total_volume" and severities is not None:
        raise ValueError("severities is only valid with signal='severity_volume'; total_volume includes every level")
    if severities is not None:
        if not isinstance(severities, list) or not 1 <= len(severities) <= len(BGL_SEVERITIES):
            raise ValueError("severities must be a non-empty list of BGL severity names")
        if any(not isinstance(s, str) or s not in BGL_SEVERITIES for s in severities):
            raise ValueError(f"severities must contain only: {', '.join(BGL_SEVERITIES)}")
    effective_severities = sorted(set(DEFAULT_SEVERITIES if severities is None else severities))
    if signal == "total_volume":
        effective_severities = None
    baseline_quantile = _finite(baseline_quantile, "baseline_quantile")
    threshold_multiplier = _finite(threshold_multiplier, "threshold_multiplier")
    if not 0 < baseline_quantile < 1:
        raise ValueError("baseline_quantile must be greater than 0 and less than 1")
    if threshold_multiplier < 1:
        raise ValueError("threshold_multiplier must be at least 1")
    min_bucket_count = _integer(min_bucket_count, "min_bucket_count", 1_000_000_000)
    min_baseline_buckets = _integer(min_baseline_buckets, "min_baseline_buckets", 1_000_000)
    max_findings = _integer(max_findings, "max_findings", 20)
    patterns_per_finding = _integer(patterns_per_finding, "patterns_per_finding", 5)
    evidence_per_pattern = _integer(evidence_per_pattern, "evidence_per_pattern", 3)

    parameters: dict[str, Any] = dict(baseline_quantile=baseline_quantile,
        threshold_multiplier=threshold_multiplier, min_bucket_count=min_bucket_count,
        min_baseline_buckets=min_baseline_buckets, max_findings=max_findings,
        patterns_per_finding=patterns_per_finding, evidence_per_pattern=evidence_per_pattern)
    selected = "TRUE"
    if effective_severities is not None:
        names = []
        for i, severity in enumerate(effective_severities):
            key = f"severity_{i}"
            parameters[key] = severity
            names.append(f"%({key})s")
        selected = "severity IN (" + ", ".join(names) + ")"
    component_where = "TRUE"
    if component is not None:
        parameters["component"] = component
        component_where = "component = %(component)s"
    delta = f"INTERVAL '1 {interval}'"
    overlap, complete, input_predicates = [], [], [component_where]
    for name, value, overlap_sql, complete_sql, input_sql in (
        ("start_time", start_time, f"bucket_start + {delta} > %(start_time)t",
         "bucket_start >= %(start_time)t", "timestamp >= %(start_time)t"),
        ("end_time", end_time, "bucket_start < %(end_time)t",
         f"bucket_start + {delta} <= %(end_time)t", "timestamp < %(end_time)t"),
    ):
        if value is not None:
            parameters[name] = value
            overlap.append(overlap_sql)
            complete.append(complete_sql)
            input_predicates.append(input_sql)
    overlap_sql = " AND ".join(overlap) or "TRUE"
    complete_sql = " AND ".join(complete) or "TRUE"
    reference_sql = "TRUE"
    if baseline_scope == "reference":
        parameters.update(baseline_start_time=baseline_start_time, baseline_end_time=baseline_end_time)
        reference_sql = f"bucket_start >= %(baseline_start_time)t AND bucket_start + {delta} <= %(baseline_end_time)t"
    # NOT MATERIALIZED enables narrow-column scans. Never materialize the full
    # corpus messages or put timestamp predicates on an inverted relation.
    source_sql = f"""SELECT {spec.id_expression} AS record_id, {spec.time_column} AS timestamp,
    epoch, component, {spec.severity_column} AS severity, node, message FROM {spec.table}"""
    sql = f"""WITH source AS NOT MATERIALIZED (
{source_sql}
),
bucket_counts AS MATERIALIZED (
    SELECT component, date_trunc('{interval}', timestamp) AS bucket_start,
        COUNT(*) AS total_record_count, COUNT(*) FILTER (WHERE {selected}) AS selected_record_count,
        MIN(timestamp) AS first_record_time, MAX(timestamp) AS last_record_time,
        MIN(timestamp) FILTER (WHERE {selected}) AS first_selected_time,
        MAX(timestamp) FILTER (WHERE {selected}) AS last_selected_time,
        MIN(round((epoch(timestamp) - epoch) / 3600.0)) AS min_clock_offset_hours,
        MAX(round((epoch(timestamp) - epoch) / 3600.0)) AS max_clock_offset_hours,
        COUNT(*) FILTER (WHERE epoch IS NULL) AS missing_epoch_record_count
    FROM source GROUP BY component, bucket_start
),
corpus AS MATERIALIZED (
    SELECT COALESCE(SUM(total_record_count), 0) AS record_count,
        MIN(first_record_time) AS observed_start, MAX(last_record_time) AS observed_end
    FROM bucket_counts
),
clock AS MATERIALIZED (
    SELECT bucket_start, MIN(min_clock_offset_hours) AS min_clock_offset_hours,
        MAX(max_clock_offset_hours) AS max_clock_offset_hours,
        SUM(missing_epoch_record_count) AS missing_epoch_record_count
    FROM bucket_counts GROUP BY bucket_start
),
clock_gaps AS MATERIALIZED (
    SELECT bucket_start + {delta} AS gap_start,
        LEAD(bucket_start) OVER (ORDER BY bucket_start) AS gap_end
    FROM clock
),
classified AS MATERIALIZED (
    SELECT b.*, bucket_start < observed_start OR bucket_start + {delta} > observed_end AS corpus_boundary,
        c.min_clock_offset_hours <> c.max_clock_offset_hours AS ambiguous_clock,
        c.missing_epoch_record_count > 0 AS missing_clock
    FROM bucket_counts b JOIN clock c USING (bucket_start) CROSS JOIN corpus
    WHERE {component_where}
),
eligible_corpus AS MATERIALIZED (
    SELECT * FROM classified WHERE NOT corpus_boundary AND NOT ambiguous_clock AND NOT missing_clock
),
analysis_candidates AS MATERIALIZED (
    SELECT *, NOT ({complete_sql}) AS request_boundary FROM classified WHERE {overlap_sql}
),
analysis_eligible AS MATERIALIZED (
    SELECT * FROM analysis_candidates
    WHERE NOT corpus_boundary AND NOT ambiguous_clock AND NOT missing_clock AND NOT request_boundary
),
baseline_stats AS MATERIALIZED (
    SELECT component, COUNT(*) AS bucket_count,
        SUM(total_record_count) AS total_record_count, SUM(selected_record_count) AS selected_record_count,
        MIN(bucket_start) AS observed_start, MAX(bucket_start) + {delta} AS observed_end,
        AVG(selected_record_count ORDER BY bucket_start) AS mean_count,
        quantile_cont(selected_record_count, 0.5) AS median_count,
        quantile_cont(selected_record_count, 0.95) AS p95_count,
        quantile_cont(selected_record_count, 0.99) AS p99_count,
        quantile_cont(selected_record_count, CAST(%(baseline_quantile)t AS DOUBLE)) AS quantile_count
    FROM eligible_corpus WHERE {reference_sql} GROUP BY component
),
baselines AS MATERIALIZED (
    SELECT c.component, COALESCE(s.bucket_count, 0) AS bucket_count,
        s.total_record_count, s.selected_record_count, s.observed_start, s.observed_end,
        s.mean_count, s.median_count, s.p95_count, s.p99_count, s.quantile_count,
        CASE WHEN COALESCE(s.bucket_count, 0) < %(min_baseline_buckets)t THEN 'insufficient_baseline'
             WHEN NOT isfinite(s.quantile_count * CAST(%(threshold_multiplier)t AS DOUBLE)) THEN 'nonfinite_threshold'
             ELSE 'ready' END AS status,
        CASE WHEN s.bucket_count >= %(min_baseline_buckets)t
             AND isfinite(s.quantile_count * CAST(%(threshold_multiplier)t AS DOUBLE))
             THEN s.quantile_count * CAST(%(threshold_multiplier)t AS DOUBLE) ELSE NULL END AS threshold
    FROM (SELECT DISTINCT component FROM classified) c LEFT JOIN baseline_stats s USING (component)
),
scored AS MATERIALIZED (
    SELECT a.*, b.status AS baseline_status, b.bucket_count AS baseline_bucket_count,
        b.mean_count AS baseline_mean_count, b.quantile_count AS baseline_quantile_count,
        b.threshold, b.status = 'ready' AND a.selected_record_count >= %(min_bucket_count)t
            AND a.selected_record_count > b.threshold AS elevated
    FROM analysis_eligible a JOIN baselines b USING (component)
),
flagged AS MATERIALIZED (
    SELECT *, bucket_start - ROW_NUMBER() OVER (PARTITION BY component ORDER BY bucket_start) * {delta} AS island
    FROM scored WHERE elevated
),
windows AS MATERIALIZED (
    SELECT component, island, MIN(bucket_start) AS start_time, MAX(bucket_start) + {delta} AS end_time,
        COUNT(*) AS elevated_bucket_count, SUM(selected_record_count) AS selected_record_count,
        SUM(total_record_count) AS total_record_count, MAX(selected_record_count) AS peak_bucket_count,
        MIN(first_selected_time) AS first_selected_time, MAX(last_selected_time) AS last_selected_time,
        MIN(baseline_bucket_count) AS baseline_bucket_count, MIN(baseline_mean_count) AS baseline_mean_count,
        MIN(baseline_quantile_count) AS baseline_quantile_count, MIN(threshold) AS threshold
    FROM flagged GROUP BY component, island
),
returned_windows AS MATERIALIZED (
    SELECT *, ROW_NUMBER() OVER (ORDER BY selected_record_count DESC, start_time, component) AS finding_id
    FROM windows ORDER BY selected_record_count DESC, start_time, component LIMIT %(max_findings)t
),
window_rows AS NOT MATERIALIZED (
    SELECT w.finding_id, s.* FROM source s JOIN returned_windows w
        ON s.component = w.component AND s.timestamp >= w.start_time AND s.timestamp < w.end_time
    WHERE {selected}
),
window_quality AS MATERIALIZED (
    SELECT finding_id, COUNT(DISTINCT node) AS distinct_node_identifier_count,
        COUNT(*) FILTER (WHERE node IS NULL) AS missing_node_record_count
    FROM window_rows GROUP BY finding_id
),
patterns AS MATERIALIZED (
    SELECT finding_id, severity, message, COUNT(*) AS record_count,
        COUNT(DISTINCT node) AS distinct_node_identifier_count,
        MIN(timestamp) AS first_seen, MAX(timestamp) AS last_seen
    FROM window_rows GROUP BY finding_id, severity, message
),
ranked_patterns AS MATERIALIZED (
    SELECT *, ROW_NUMBER() OVER (PARTITION BY finding_id ORDER BY record_count DESC, message, severity) AS pattern_rank
    FROM patterns
),
returned_patterns AS MATERIALIZED (
    SELECT * FROM ranked_patterns WHERE pattern_rank <= %(patterns_per_finding)t
),
evidence_ranked AS (
    SELECT r.finding_id, p.pattern_rank, r.record_id, r.timestamp, r.component, r.severity, r.node,
        ROW_NUMBER() OVER (PARTITION BY r.finding_id, p.pattern_rank ORDER BY r.timestamp, r.record_id) AS evidence_rank
    FROM window_rows r JOIN returned_patterns p
        ON r.finding_id = p.finding_id AND r.severity = p.severity AND r.message = p.message
),
evidence AS MATERIALIZED (
    SELECT * FROM evidence_ranked WHERE evidence_rank <= %(evidence_per_pattern)t
),
input_summary AS (
    SELECT COUNT(*) AS record_count, COUNT(*) FILTER (WHERE {selected}) AS selected_record_count
    FROM source WHERE {' AND '.join(input_predicates)}
)
SELECT
    (SELECT to_json(c) FROM corpus c) AS corpus,
    (SELECT to_json(i) FROM input_summary i) AS input,
    (SELECT to_json(x) FROM (SELECT
        (SELECT COUNT(*) FROM clock) AS observed_bucket_count,
        (SELECT COALESCE(date_diff('{interval}', MIN(bucket_start), MAX(bucket_start)) + 1, 0) FROM clock) AS calendar_bucket_count,
        (SELECT COUNT(*) FROM clock WHERE min_clock_offset_hours <> max_clock_offset_hours) AS ambiguous_clock_bucket_count,
        (SELECT COUNT(*) FROM clock WHERE missing_epoch_record_count > 0) AS missing_clock_bucket_count,
        (SELECT COUNT(*) FROM clock_gaps WHERE gap_end > gap_start) AS gap_count,
        (SELECT COUNT(*) FROM analysis_candidates) AS analysis_component_active_bucket_count,
        (SELECT COUNT(*) FROM analysis_candidates WHERE corpus_boundary OR request_boundary) AS incomplete_boundary_component_bucket_count,
        (SELECT COUNT(*) FROM analysis_candidates WHERE ambiguous_clock) AS ambiguous_clock_component_bucket_count,
        (SELECT COUNT(*) FROM analysis_candidates WHERE missing_clock) AS missing_clock_component_bucket_count,
        (SELECT COUNT(*) FROM analysis_eligible) AS eligible_component_bucket_count,
        (SELECT COALESCE(SUM(total_record_count), 0) FROM analysis_eligible) AS eligible_record_count,
        (SELECT COALESCE(SUM(selected_record_count), 0) FROM analysis_eligible) AS eligible_selected_record_count,
        (SELECT COUNT(*) FROM scored WHERE baseline_status = 'ready') AS evaluated_component_bucket_count,
        (SELECT COUNT(*) FROM flagged) AS elevated_component_bucket_count,
        (SELECT COALESCE(SUM(selected_record_count), 0) FROM flagged) AS elevated_record_count,
        (SELECT COUNT(*) FROM windows) AS spike_window_count,
        (SELECT COUNT(*) FROM baselines) AS baseline_component_count,
        (SELECT COUNT(*) FROM baselines WHERE status <> 'ready') AS unavailable_baseline_component_count
    ) x) AS counts,
    (SELECT to_json(list(g ORDER BY gap_start)) FROM (
        SELECT gap_start, gap_end, date_diff('{interval}', gap_start, gap_end) AS missing_bucket_count
        FROM clock_gaps WHERE gap_end > gap_start ORDER BY gap_start LIMIT {DIAGNOSTIC_LIMIT}
    ) g) AS gaps,
    (SELECT to_json(list(c ORDER BY bucket_start)) FROM (
        SELECT * FROM clock WHERE min_clock_offset_hours <> max_clock_offset_hours OR missing_epoch_record_count > 0
        ORDER BY bucket_start LIMIT {DIAGNOSTIC_LIMIT}
    ) c) AS clock_diagnostics,
    (SELECT COUNT(*) FROM clock WHERE min_clock_offset_hours <> max_clock_offset_hours OR missing_epoch_record_count > 0) AS clock_diagnostic_count,
    (SELECT to_json(list(b ORDER BY component)) FROM (SELECT * FROM baselines ORDER BY component LIMIT {BASELINE_LIMIT}) b) AS baselines,
    (SELECT to_json(list(w ORDER BY finding_id)) FROM returned_windows w) AS findings,
    (SELECT to_json(list(q ORDER BY finding_id)) FROM window_quality q) AS window_quality,
    (SELECT to_json(list(p ORDER BY finding_id, pattern_rank)) FROM (
        SELECT finding_id, pattern_rank, severity, left(message, {MESSAGE_LIMIT}) AS message,
            length(message) AS message_character_count, length(message) > {MESSAGE_LIMIT} AS message_truncated,
            md5(message) AS message_fingerprint, record_count, distinct_node_identifier_count, first_seen, last_seen
        FROM returned_patterns
    ) p) AS patterns,
    (SELECT to_json(list(e ORDER BY finding_id, pattern_rank, evidence_rank)) FROM evidence e) AS evidence,
    (SELECT to_json(list(p ORDER BY finding_id)) FROM (
        SELECT finding_id, COUNT(*) AS total_pattern_count, SUM(record_count) AS complete_pattern_record_count
        FROM patterns GROUP BY finding_id
    ) p) AS pattern_counts"""
    filters = dict(dataset=spec.name, start_time=start_time, end_time=end_time, component=component,
        interval=interval, signal=signal, severities=effective_severities, baseline_scope=baseline_scope,
        baseline_start_time=baseline_start_time, baseline_end_time=baseline_end_time,
        baseline_quantile=baseline_quantile, threshold_multiplier=threshold_multiplier,
        min_bucket_count=min_bucket_count, min_baseline_buckets=min_baseline_buckets,
        max_findings=max_findings, patterns_per_finding=patterns_per_finding, evidence_per_pattern=evidence_per_pattern)
    return SpikePlan(sql, parameters, filters, source_sql, spec.table, spec.scope_label)


def _json(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        result = {}
        for key, item in value.items():
            if isinstance(item, str) and key in {"timestamp", "bucket_start", "start_time", "end_time",
                    "observed_start", "observed_end", "first_selected_time", "last_selected_time",
                    "first_seen", "last_seen", "gap_start", "gap_end", "corpus_observed_start", "corpus_observed_end"}:
                item = datetime.fromisoformat(item)
            result[key] = _json(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_json(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        raise RuntimeError("SereneDB returned a non-finite spike statistic")
    return value


def _ratio(numerator: int | float, denominator: int | float | None) -> float | None:
    return numerator / denominator if denominator else None


def serialize_spikes(row: Mapping[str, Any], plan: SpikePlan) -> dict[str, Any]:
    filters = dict(plan.applied_filters)
    counts = {k: int(v) for k, v in row["counts"].items()}
    counts.update(corpus_record_count=int(row["corpus"]["record_count"]),
                  analysis_record_count=int(row["input"]["record_count"]),
                  analysis_selected_record_count=int(row["input"]["selected_record_count"]))
    baselines = [dict(b) for b in row["baselines"] or []]
    findings = [dict(w) for w in row["findings"] or []]
    quality = {q["finding_id"]: q for q in row["window_quality"] or []}
    pattern_counts = {p["finding_id"]: p for p in row["pattern_counts"] or []}
    for baseline in baselines:
        for field in ("bucket_count", "total_record_count", "selected_record_count"):
            if baseline[field] is not None:
                baseline[field] = int(baseline[field])
    for finding in findings:
        finding.pop("island")
        fid = finding["finding_id"]
        for field in ("selected_record_count", "total_record_count", "elevated_bucket_count"):
            finding[field] = int(finding[field])
        complete_pattern_count = int(pattern_counts[fid]["complete_pattern_record_count"])
        total_patterns = int(pattern_counts[fid]["total_pattern_count"])
        if complete_pattern_count != finding["selected_record_count"]:
            raise RuntimeError("Complete spike contributor population does not reconcile with its window")
        finding.update({k: int(v) for k, v in quality[fid].items() if k != "finding_id"})
        patterns = [dict(p) for p in row["patterns"] or [] if p["finding_id"] == fid]
        for pattern in patterns:
            pattern.pop("finding_id")
            pattern["percentage_of_window_selected_records"] = 100.0 * pattern["record_count"] / finding["selected_record_count"]
            evidence = [dict(e) for e in row["evidence"] or []
                        if e["finding_id"] == fid and e["pattern_rank"] == pattern["pattern_rank"]]
            for event in evidence:
                event.pop("finding_id")
                event.pop("pattern_rank")
                event.pop("evidence_rank")
            pattern.update(evidence=evidence, returned_evidence_count=len(evidence),
                           evidence_truncated=pattern["record_count"] > len(evidence))
        returned_count = sum(p["record_count"] for p in patterns)
        omitted = finding["selected_record_count"] - returned_count
        if omitted < 0 or len(patterns) > total_patterns:
            raise RuntimeError("Spike contributor counts do not reconcile with the complete window")
        finding.update(contributors=patterns, total_pattern_count=total_patterns, returned_pattern_count=len(patterns),
            complete_pattern_record_count=complete_pattern_count,
            patterns_truncated=total_patterns > len(patterns), returned_pattern_record_count=returned_count,
            omitted_pattern_record_count=omitted,
            omitted_pattern_record_percentage=100.0 * omitted / finding["selected_record_count"],
            selected_record_percentage=100.0 * finding["selected_record_count"] / finding["total_record_count"],
            mean_selected_records_per_bucket=finding["selected_record_count"] / finding["elevated_bucket_count"],
            peak_to_baseline_mean_ratio=_ratio(finding["peak_bucket_count"], finding["baseline_mean_count"]),
            peak_to_baseline_quantile_ratio=_ratio(finding["peak_bucket_count"], finding["baseline_quantile_count"]))
    gaps, clocks = row["gaps"] or [], row["clock_diagnostics"] or []
    warnings = ["Spikes are elevated log-record volumes, not confirmed incidents, root causes or outages."]
    if filters["baseline_scope"] == "corpus":
        warnings.append("The corpus baseline is retrospective, includes the analysis population and may include later periods.")
    elif filters["baseline_start_time"] >= filters["end_time"]:
        warnings.append("The explicit reference period follows the analysis; this is a retrospective comparison, not a prior historical baseline.")
    if counts["unavailable_baseline_component_count"]:
        warnings.append("Some components have insufficient baseline history or an unavailable threshold; their buckets are not classified as normal.")
    if not counts["evaluated_component_bucket_count"]:
        warnings.append("No eligible analysis buckets have an available baseline.")
    if counts["ambiguous_clock_bucket_count"]:
        warnings.append("Mixed rounded clock-offset buckets were excluded; no source timezone was inferred.")
    counts["returned_spike_window_count"] = len(findings)
    coverage = dict(corpus_observed_start=row["corpus"]["observed_start"], corpus_observed_end=row["corpus"]["observed_end"],
        observed_global_bucket_count=counts.pop("observed_bucket_count"),
        calendar_global_bucket_count=counts.pop("calendar_bucket_count"),
        ambiguous_global_clock_bucket_count=counts.pop("ambiguous_clock_bucket_count"),
        missing_global_clock_bucket_count=counts.pop("missing_clock_bucket_count"),
        gaps=gaps, total_gap_count=counts.pop("gap_count"), gaps_truncated=False,
        clock_diagnostics=clocks, total_clock_diagnostic_count=int(row["clock_diagnostic_count"]),
        clock_diagnostics_truncated=int(row["clock_diagnostic_count"]) > len(clocks),
        diagnostics_limit=DIAGNOSTIC_LIMIT, missing_periods_treated_as_zero=False,
        baseline_observation_policy="component-active buckets; unselected-severity-only buckets remain valid zero selected observations",
        boundary_policy="Only whole source-clock buckets inside both corpus timestamp coverage and requested ranges",
        clock_policy="Exclude globally mixed rounded (source timestamp minus epoch) offsets in hours, or missing epoch",
        exclusion_counts_can_overlap=True, collection_completeness_known=False)
    coverage["unobserved_global_bucket_count"] = coverage["calendar_global_bucket_count"] - coverage["observed_global_bucket_count"]
    coverage["gaps_truncated"] = coverage["total_gap_count"] > len(gaps)
    return _json(dict(scope=dict(dataset="bgl", table=plan.table, source=plan.scope_label,
        count_unit="log records; windows and distinct node identifiers are not incident counts",
        timestamp_basis="naive source-log timestamp; no timezone conversion",
        timestamp_bounds="start inclusive, end exclusive", complete_population_aggregated=True),
        method=dict(version=METHOD_VERSION, baseline_scope=filters["baseline_scope"],
            baseline_quantile=filters["baseline_quantile"], threshold_multiplier=filters["threshold_multiplier"],
            min_bucket_count=filters["min_bucket_count"], min_baseline_buckets=filters["min_baseline_buckets"],
            rule="selected_record_count >= min_bucket_count AND selected_record_count > unrounded threshold",
            interpretation="configurable empirical screening heuristic; no calibrated false-positive probability",
            labels_used_for_detection=False, seasonality_adjusted=False),
        counts=counts, coverage=coverage, baselines=baselines, findings=findings,
        truncation=dict(max_findings=filters["max_findings"], findings_truncated=counts["spike_window_count"] > len(findings),
            patterns_per_finding=filters["patterns_per_finding"], evidence_per_pattern=filters["evidence_per_pattern"],
            baseline_limit=BASELINE_LIMIT, baselines_truncated=counts["baseline_component_count"] > len(baselines),
            message_character_limit=MESSAGE_LIMIT),
        warnings=warnings, limitations=["Silent periods do not establish zero activity or collection completeness.",
            "Component activity, workload, logging changes and repeated dumps can produce spikes.",
            "Window boundaries describe elevated buckets, not incident onset or resolution.",
            "Exact messages are grouped without inferred event templates; message fingerprints are display references only."],
        provenance=dict(executed_sql={"analysis": plan.sql}, parameters={"analysis": dict(plan.parameters)},
            applied_filters=filters, statement_count=1, methodology_version=METHOD_VERSION,
            percentile_method="quantile_cont inclusive continuous interpolation over all eligible component-active buckets")))


def execute_spike_plan(connections: ConnectionFactory, plan: SpikePlan) -> dict[str, Any]:
    with connections() as connection, connection.cursor() as cursor:
        cursor.execute(plan.sql, plan.parameters)
        row = cursor.fetchone()
    if row is None:
        raise RuntimeError("SereneDB did not return a spike summary")
    return serialize_spikes(row, plan)
