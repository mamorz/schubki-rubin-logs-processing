"""Allowlisted, parameterized SereneDB queries for log evidence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Sequence

from .db import ConnectionFactory
from .vm_performance import build_vm_spawn_plan, execute_vm_spawn_plan
from .spikes import build_spike_plan, execute_spike_plan
from .ingestion import (
    BGL_SEARCH_INDEX,
    BGL_TABLE,
    OPENSTACK_SEARCH_INDEX,
    OPENSTACK_TABLE,
)
from .ownership import (
    OWNERSHIP_MAPPING_REPOSITORY_PATH,
    OWNERSHIP_PROVENANCE,
    OWNERSHIP_TABLE,
    OWNERSHIP_VERSION,
    UNMAPPED_TEAM,
)

DEFAULT_SEARCH_LIMIT = 20
MAX_SEARCH_LIMIT = 50
DEFAULT_SUMMARY_LIMIT = 20
MAX_SUMMARY_LIMIT = 100
DEFAULT_NEARBY_EVENTS = 10
MAX_NEARBY_EVENTS = 50

# Code-owned SQL only: the query text always remains a bound parameter.
SEARCH_PREDICATES: Mapping[str, str] = {
    "any": "message @@ %(text)s",
    "phrase": "message @@ ts_phrase(%(text)s)",
    "exact": "message = %(text)s",
    "all": "message @@ plainto_tsquery(%(text)s)",
}


@dataclass(frozen=True)
class DatasetSpec:
    """Fixed database objects for an allowlisted dataset."""

    name: str
    table: str
    index_relation: str
    scope_label: str
    id_expression: str = "line_id"
    time_column: str = "event_time"
    severity_column: str = "severity"
    event_expression: str = "event_id"
    ownership_table: str = OWNERSHIP_TABLE
    ownership_file: str = OWNERSHIP_MAPPING_REPOSITORY_PATH
    ownership_version: str = OWNERSHIP_VERSION
    development_sample: bool = True
    supports_openstack_tracing: bool = False
    supports_bgl_spikes: bool = False

    @property
    def record_columns(self) -> str:
        event = (
            "event_id" if self.event_expression == "event_id"
            else f"{self.event_expression} AS event_id"
        )
        severity = (
            "severity" if self.severity_column == "severity"
            else f"{self.severity_column} AS severity"
        )
        return (
            f"{self.id_expression} AS record_id,\n    {event},\n    "
            f"{self.time_column} AS timestamp,\n    component,\n    "
            f"{severity},\n    message"
        )


DATASET_SPECS: Mapping[str, DatasetSpec] = {
    "bgl": DatasetSpec(
        name="bgl",
        table=BGL_TABLE,
        index_relation=BGL_SEARCH_INDEX,
        scope_label="public Loghub BGL 2,000-row development sample",
    ),
    "openstack": DatasetSpec(
        name="openstack",
        table=OPENSTACK_TABLE,
        index_relation=OPENSTACK_SEARCH_INDEX,
        scope_label="public Loghub OpenStack 2,000-row development sample",
    ),
}

# Preserve the full-ingestion ID convention: abnormal=1, normal1=2, normal2=3.
# Only known source names are mapped. The read-only readiness check rejects other
# names, nonpositive/out-of-range line numbers, and duplicate source keys.
OPENSTACK_SQL_ID = """(CASE source_file
    WHEN 'openstack_abnormal' THEN CAST(1000000000 AS BIGINT)
    WHEN 'openstack_normal1' THEN CAST(2000000000 AS BIGINT)
    WHEN 'openstack_normal2' THEN CAST(3000000000 AS BIGINT)
    ELSE NULL END + line_no)"""

SQL_DATASET_SPECS: Mapping[str, DatasetSpec] = {
    dataset: DatasetSpec(
        name=dataset,
        table=f"{dataset}_logs",
        index_relation=f"{dataset}_logs_idx",
        scope_label=f"SQL-managed {dataset} corpus; counts reflect currently loaded rows",
        id_expression="line_no" if dataset == "bgl" else OPENSTACK_SQL_ID,
        time_column="ts",
        severity_column="level",
        event_expression="CAST(NULL AS VARCHAR)",
        ownership_table="ownership",
        ownership_file="data/ownership.csv",
        ownership_version="main-a75f858",
        development_sample=False,
        supports_openstack_tracing=dataset == "openstack",
        supports_bgl_spikes=dataset == "bgl",
    )
    for dataset in ("bgl", "openstack")
}

GROUPING_FIELDS: Mapping[str, str] = {
    "component": "component",
    "severity": "severity",
    "event_id": "event_id",
}
TIME_INTERVALS: Mapping[str, str] = {
    "hour": "hour",
    "day": "day",
    "week": "week",
    "month": "month",
}


@dataclass(frozen=True)
class SearchQueryPlan:
    """Validated SQL statements and parameters for one search request."""

    dataset: str
    sql: str
    parameters: Mapping[str, Any]
    count_sql: str
    count_parameters: Mapping[str, Any]
    applied_filters: Mapping[str, Any]
    limit: int
    match_mode: str = "any"


@dataclass(frozen=True)
class QueryResult:
    """Evidence and provenance returned by an executed database query."""

    dataset: str
    sql: str
    parameters: Mapping[str, Any]
    count_sql: str
    count_parameters: Mapping[str, Any]
    applied_filters: Mapping[str, Any]
    rows: Sequence[Mapping[str, Any]]
    returned_record_count: int
    total_corpus_match_count: int
    truncated: bool
    match_mode: str = "any"


@dataclass(frozen=True)
class AggregationQueryPlan:
    """Validated SQL statements for a full-scope aggregation."""

    dataset: str
    aggregation_type: str
    group_by: str
    interval: str | None
    sql: str
    parameters: Mapping[str, Any]
    count_sql: str
    count_parameters: Mapping[str, Any]
    coverage_sql: str | None
    coverage_parameters: Mapping[str, Any]
    applied_filters: Mapping[str, Any]
    limit: int
    scope_label: str
    table: str


@dataclass(frozen=True)
class AggregationResult:
    """Complete-scope count plus a bounded set of aggregate groups."""

    dataset: str
    aggregation_type: str
    group_by: str
    interval: str | None
    sql: str
    parameters: Mapping[str, Any]
    count_sql: str
    count_parameters: Mapping[str, Any]
    coverage_sql: str | None
    coverage_parameters: Mapping[str, Any]
    applied_filters: Mapping[str, Any]
    groups: Sequence[Mapping[str, Any]]
    returned_group_count: int
    total_matching_record_count: int
    group_limit: int
    truncated: bool
    scope_label: str
    table: str
    mapping_coverage: Mapping[str, Any] | None
    ownership_provenance: Mapping[str, Any] | None
    development_sample: bool = True


@dataclass(frozen=True)
class NearbyQueryPlan:
    """Validated anchor and chronological-neighbor query statements."""

    dataset: str
    record_id: int
    before: int
    after: int
    same_component: bool
    anchor_sql: str
    anchor_parameters: Mapping[str, Any]
    before_sql: str
    after_sql: str
    scope_label: str
    table: str


@dataclass(frozen=True)
class NearbyEventsResult:
    """Anchor lookup plus chronological context and query provenance."""

    dataset: str
    found: bool
    anchor: Mapping[str, Any] | None
    before_events: Sequence[Mapping[str, Any]]
    after_events: Sequence[Mapping[str, Any]]
    requested_before: int
    requested_after: int
    same_component: bool
    component_filter: str | None
    executed_sql: Mapping[str, str]
    parameters: Mapping[str, Mapping[str, Any]]
    scope_label: str
    table: str
    development_sample: bool = True


def build_search_query_plan(
    *,
    dataset: str,
    text: str | None = None,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    component: str | None = None,
    severity: str | None = None,
    limit: int = DEFAULT_SEARCH_LIMIT,
    match_mode: str = "any",
    request_id: str | None = None,
    instance_id: str | None = None,
    _dataset_specs: Mapping[str, DatasetSpec] = DATASET_SPECS,
) -> SearchQueryPlan:
    """Validate a request and build SQL using only fixed relation names.

    Dataset selection chooses from a code-owned allowlist. Every caller-supplied
    value remains a psycopg parameter and is never interpolated into SQL.
    """

    normalized_dataset = _normalize_dataset(dataset, _dataset_specs)
    normalized_mode = _normalize_match_mode(match_mode)
    spec = _dataset_specs[normalized_dataset]
    normalized_request = _optional_text(request_id, "request_id")
    normalized_instance = _optional_text(instance_id, "instance_id")
    has_identifiers = normalized_request is not None or normalized_instance is not None
    if has_identifiers and (normalized_dataset != "openstack" or not spec.supports_openstack_tracing):
        raise ValueError(
            "request_id and instance_id filters require dataset='openstack' with "
            "the SQL backend (MCP_DATASET_BACKEND=sql); BGL and sample tables do not support them"
        )
    if text is None and not has_identifiers and normalized_dataset != "bgl":
        raise ValueError("query is required unless request_id or instance_id is supplied for SQL-backed OpenStack")
    normalized_text = None if text is None else _search_text(text, normalized_mode)
    normalized_component = _optional_text(component, "component")
    normalized_severity = _optional_text(severity, "severity")
    _validate_timestamp_range(start_time, end_time)
    _validate_limit(limit, MAX_SEARCH_LIMIT)
    if normalized_text is None and normalized_dataset == "bgl" and (
        start_time is None or end_time is None
        or (normalized_component is None and normalized_severity is None)
    ):
        raise ValueError(
            "query is required unless BGL filter-only search includes both start_time "
            "and end_time plus component or severity"
        )

    predicates = [] if normalized_text is None else [SEARCH_PREDICATES[normalized_mode]]
    # An analyzed index is for token matching. Raw equality must compare the
    # complete stored message, including case, punctuation and whitespace.
    relation = spec.table if normalized_mode == "exact" or normalized_text is None else spec.index_relation
    if normalized_text is not None and normalized_mode != "exact" and (
        start_time is not None or end_time is not None or spec.supports_openstack_tracing
    ):
        # SereneDB 26.09's indexed timestamp range can include out-of-range rows.
        # Keep full-text matching in the index but apply time/filter predicates
        # to base-table candidates. Use this same plan for records AND counts.
        relation = spec.table
        predicates = [_indexed_message_candidates(spec, normalized_mode)]
    count_parameters: dict[str, Any] = {} if normalized_text is None else {"text": normalized_text}
    applied_filters: dict[str, Any] = {
        "full_text_query": normalized_text,
        "match_mode": normalized_mode,
        "start_time": start_time,
        "end_time": end_time,
        "component": normalized_component,
        "severity": normalized_severity,
        "timestamp_bounds": "start inclusive, end exclusive",
        "result_limit": limit,
    }
    for field, value in (("request_id", normalized_request), ("instance_id", normalized_instance)):
        if value is not None:
            predicates.append(f"{field} = %({field})s")
            count_parameters[field] = value
            applied_filters[field] = value

    if start_time is not None:
        # 26.09's inverted-index path rejects psycopg binary datetime values.
        # %t binds the same typed value in text wire format; never interpolate it.
        predicates.append(f"{spec.time_column} >= %(start_time)t")
        count_parameters["start_time"] = start_time
    if end_time is not None:
        predicates.append(f"{spec.time_column} < %(end_time)t")
        count_parameters["end_time"] = end_time
    if normalized_component is not None:
        predicates.append("component = %(component)s")
        count_parameters["component"] = normalized_component
    if normalized_severity is not None:
        predicates.append(f"{spec.severity_column} = %(severity)s")
        count_parameters["severity"] = normalized_severity

    where_clause = " AND\n    ".join(predicates)
    columns = spec.record_columns
    if spec.supports_openstack_tracing:
        # These fields exist on the SQL base table; latency_s/is_anomaly are not
        # projected by its inverted relation. Preserve SQL NULL values as-is.
        columns += ",\n    request_id,\n    instance_id,\n    service,\n    http_status,\n    latency_s,\n    is_anomaly"
    sql = f"""SELECT
    {columns}
FROM {relation}
WHERE {where_clause}
ORDER BY {spec.time_column}, {spec.id_expression}
LIMIT %(fetch_limit)s"""
    count_sql = f"""SELECT COUNT(*) AS total_corpus_match_count
FROM {relation}
WHERE {where_clause}"""

    parameters = dict(count_parameters)
    parameters["fetch_limit"] = limit + 1
    return SearchQueryPlan(
        dataset=normalized_dataset,
        sql=sql,
        parameters=parameters,
        count_sql=count_sql,
        count_parameters=count_parameters,
        applied_filters=applied_filters,
        limit=limit,
        match_mode=normalized_mode,
    )


def build_aggregation_query_plan(
    *,
    dataset: str,
    group_by: str,
    interval: str | None = None,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    component: str | None = None,
    severity: str | None = None,
    limit: int = DEFAULT_SUMMARY_LIMIT,
    query: str | None = None,
    match_mode: str = "any",
    sort_direction: str = "asc",
    _dataset_specs: Mapping[str, DatasetSpec] = DATASET_SPECS,
) -> AggregationQueryPlan:
    """Build an allowlisted grouped-count or time-bucket aggregation."""

    normalized_dataset = _normalize_dataset(dataset, _dataset_specs)
    normalized_mode = _normalize_match_mode(match_mode)
    normalized_query = None if query is None else _search_text(query, normalized_mode)
    normalized_group_by = _normalize_group_by(group_by)
    normalized_interval = _normalize_interval(interval)
    if not isinstance(sort_direction, str) or sort_direction.strip().lower() not in {"asc", "desc"}:
        raise ValueError("sort_direction must be one of: asc, desc")
    normalized_direction = sort_direction.strip().lower()
    normalized_component = _optional_text(component, "component")
    normalized_severity = _optional_text(severity, "severity")
    _validate_timestamp_range(start_time, end_time)
    _validate_limit(limit, MAX_SUMMARY_LIMIT)

    if normalized_group_by == "time":
        if normalized_interval is None:
            raise ValueError("interval is required when group_by is time")
        aggregation_type = "time_bucket_count"
    else:
        if normalized_interval is not None:
            raise ValueError("interval is only valid when group_by is time")
        if normalized_direction != "asc":
            raise ValueError("sort_direction is only valid when group_by is time")
        aggregation_type = "grouped_count"

    spec = _dataset_specs[normalized_dataset]
    predicates: list[str] = []
    count_parameters: dict[str, Any] = {}
    applied_filters: dict[str, Any] = {
        "start_time": start_time,
        "end_time": end_time,
        "component": normalized_component,
        "severity": normalized_severity,
        "timestamp_bounds": "start inclusive, end exclusive",
        "group_limit": limit,
    }
    if normalized_direction != "asc":
        applied_filters["sort_direction"] = normalized_direction
    if normalized_query is not None:
        # Always aggregate base-table rows. The index only selects message IDs;
        # timestamp ranges must never be evaluated on the inverted relation.
        predicates.append(
            SEARCH_PREDICATES[normalized_mode] if normalized_mode == "exact"
            else _indexed_message_candidates(spec, normalized_mode)
        )
        count_parameters["text"] = normalized_query
        applied_filters.update(full_text_query=normalized_query, match_mode=normalized_mode)
    # Keep the existing no-query SQL unchanged. Indexed message candidates use
    # the same text-wire datetime binding as search_logs on SereneDB 26.09.
    time_format = "t" if normalized_query is not None else "s"
    if start_time is not None:
        predicates.append(f"{spec.time_column} >= %(start_time){time_format}")
        count_parameters["start_time"] = start_time
    if end_time is not None:
        predicates.append(f"{spec.time_column} < %(end_time){time_format}")
        count_parameters["end_time"] = end_time
    if normalized_component is not None:
        predicates.append("component = %(component)s")
        count_parameters["component"] = normalized_component
    if normalized_severity is not None:
        predicates.append(f"{spec.severity_column} = %(severity)s")
        count_parameters["severity"] = normalized_severity

    base_where_clause = " AND\n    ".join(predicates) if predicates else "1 = 1"
    where_clause = base_where_clause
    coverage_sql: str | None = None
    coverage_parameters: dict[str, Any] = {}
    if normalized_group_by == "time":
        bucket_expression = f"date_trunc('{normalized_interval}', {spec.time_column})"
        select_expression = f"{bucket_expression} AS bucket_start"
        group_expression = bucket_expression
        order_clause = "bucket_start" if normalized_direction == "asc" else "bucket_start DESC"
        from_clause = spec.table
    elif normalized_group_by == "team":
        if normalized_query is None:
            team_source = spec.table
            where_clause = " AND\n    ".join("l." + p for p in predicates) if predicates else "1 = 1"
        else:
            # Filter before the ownership join, reusing exactly the count scope.
            # This also avoids qualifying OpenStack's composite ID expression
            # or accidentally qualifying columns inside the index subquery.
            team_source = f"(SELECT * FROM {spec.table}\nWHERE {base_where_clause})"
            where_clause = "1 = 1"
        team_expression = f"COALESCE(o.team, '{UNMAPPED_TEAM}')"
        select_expression = f"{team_expression} AS team"
        group_expression = team_expression
        order_clause = "record_count DESC, team"
        from_clause = f"""{team_source} AS l
LEFT JOIN {spec.ownership_table} AS o
    ON o.dataset = %(ownership_dataset)s
    AND o.component = l.component"""
        coverage_sql = f"""SELECT
    COUNT(*) AS total_record_count,
    COUNT(o.component) AS mapped_record_count,
    COUNT(DISTINCT l.component) AS total_component_count,
    COUNT(DISTINCT o.component) AS mapped_component_count
FROM {from_clause}
WHERE {where_clause}"""
        coverage_parameters = dict(count_parameters)
        coverage_parameters["ownership_dataset"] = normalized_dataset
    else:
        group_column = {
            "component": "component",
            "severity": spec.severity_column,
            "event_id": spec.event_expression,
        }[normalized_group_by]
        select_expression = (
            group_column if group_column == normalized_group_by
            else f"{group_column} AS {normalized_group_by}"
        )
        group_expression = group_column
        order_clause = f"record_count DESC, {normalized_group_by}"
        from_clause = spec.table

    # SereneDB's derived-table + ownership-join plan expects LIMIT in text wire
    # format as well (binary int binds are misread as strings on this path).
    limit_format = "t" if normalized_query is not None and normalized_group_by == "team" else "s"
    sql = f"""SELECT
    {select_expression},
    COUNT(*) AS record_count
FROM {from_clause}
WHERE {where_clause}
GROUP BY {group_expression}
ORDER BY {order_clause}
LIMIT %(fetch_limit){limit_format}"""
    count_sql = f"""SELECT COUNT(*) AS total_matching_record_count
FROM {spec.table}
WHERE {base_where_clause}"""
    parameters = dict(count_parameters)
    if normalized_group_by == "team":
        parameters["ownership_dataset"] = normalized_dataset
    parameters["fetch_limit"] = limit + 1

    return AggregationQueryPlan(
        dataset=normalized_dataset,
        aggregation_type=aggregation_type,
        group_by=normalized_group_by,
        interval=normalized_interval,
        sql=sql,
        parameters=parameters,
        count_sql=count_sql,
        count_parameters=count_parameters,
        coverage_sql=coverage_sql,
        coverage_parameters=coverage_parameters,
        applied_filters=applied_filters,
        limit=limit,
        scope_label=spec.scope_label,
        table=spec.table,
    )


def build_nearby_query_plan(
    *,
    dataset: str,
    record_id: int,
    before: int = DEFAULT_NEARBY_EVENTS,
    after: int = DEFAULT_NEARBY_EVENTS,
    same_component: bool = False,
    _dataset_specs: Mapping[str, DatasetSpec] = DATASET_SPECS,
) -> NearbyQueryPlan:
    """Build safe anchor and neighboring-event SQL for one dataset."""

    normalized_dataset = _normalize_dataset(dataset, _dataset_specs)
    _validate_record_id(record_id)
    _validate_neighbor_count("before", before)
    _validate_neighbor_count("after", after)
    if not isinstance(same_component, bool):
        raise ValueError("same_component must be a boolean")

    spec = _dataset_specs[normalized_dataset]
    columns = spec.record_columns
    anchor_sql = f"""SELECT
    {columns}
FROM {spec.table}
WHERE {spec.id_expression} = %(record_id)s"""
    component_predicate = (
        "\n    AND component = %(component)s" if same_component else ""
    )
    before_sql = f"""SELECT
    {columns}
FROM {spec.table}
WHERE (
    {spec.time_column} < %(anchor_time)s
    OR ({spec.time_column} = %(anchor_time)s AND {spec.id_expression} < %(record_id)s)
){component_predicate}
ORDER BY {spec.time_column} DESC, {spec.id_expression} DESC
LIMIT %(neighbor_limit)s"""
    after_sql = f"""SELECT
    {columns}
FROM {spec.table}
WHERE (
    {spec.time_column} > %(anchor_time)s
    OR ({spec.time_column} = %(anchor_time)s AND {spec.id_expression} > %(record_id)s)
){component_predicate}
ORDER BY {spec.time_column}, {spec.id_expression}
LIMIT %(neighbor_limit)s"""
    return NearbyQueryPlan(
        dataset=normalized_dataset,
        record_id=record_id,
        before=before,
        after=after,
        same_component=same_component,
        anchor_sql=anchor_sql,
        anchor_parameters={"record_id": record_id},
        before_sql=before_sql,
        after_sql=after_sql,
        scope_label=spec.scope_label,
        table=spec.table,
    )


class QueryService:
    """Own all allowlisted retrieval and aggregation queries."""

    def __init__(
        self,
        connections: ConnectionFactory,
        dataset_specs: Mapping[str, DatasetSpec] = DATASET_SPECS,
    ) -> None:
        self._connections = connections
        self._dataset_specs = dataset_specs

    def detect_log_spikes(
        self, *, dataset: str = "bgl", start_time: datetime | None = None, end_time: datetime | None = None,
        component: str | None = None, interval: str = "hour", signal: str = "severity_volume",
        severities: list[str] | None = None, baseline_scope: str = "corpus",
        baseline_start_time: datetime | None = None, baseline_end_time: datetime | None = None,
        baseline_quantile: float = 0.99, threshold_multiplier: float = 1.5,
        min_bucket_count: int = 20, min_baseline_buckets: int = 100, max_findings: int = 10,
        patterns_per_finding: int = 3, evidence_per_pattern: int = 1,
    ) -> dict[str, Any]:
        spec = self._dataset_specs[_normalize_dataset(dataset, self._dataset_specs)]
        plan = build_spike_plan(spec, start_time=start_time, end_time=end_time, component=component,
            interval=interval, signal=signal, severities=severities, baseline_scope=baseline_scope,
            baseline_start_time=baseline_start_time, baseline_end_time=baseline_end_time,
            baseline_quantile=baseline_quantile, threshold_multiplier=threshold_multiplier,
            min_bucket_count=min_bucket_count, min_baseline_buckets=min_baseline_buckets,
            max_findings=max_findings, patterns_per_finding=patterns_per_finding, evidence_per_pattern=evidence_per_pattern)
        return execute_spike_plan(self._connections, plan)

    def analyze_vm_spawn_performance(
        self, *, dataset: str = "openstack", start_time: datetime | None = None,
        end_time: datetime | None = None, slow_threshold_seconds: float | None = None,
        baseline_multiplier: float = 1.25, baseline_scope: str = "corpus",
        interval: str | None = "day", top_limit: int = 10, bucket_limit: int = 100,
        sort_direction: str = "desc", include_anomaly_labels: bool = False,
    ) -> dict[str, Any]:
        spec = self._dataset_specs[_normalize_dataset(dataset, self._dataset_specs)]
        plan = build_vm_spawn_plan(
            spec, start_time=start_time, end_time=end_time,
            slow_threshold_seconds=slow_threshold_seconds, baseline_multiplier=baseline_multiplier,
            baseline_scope=baseline_scope, interval=interval, top_limit=top_limit,
            bucket_limit=bucket_limit, sort_direction=sort_direction,
            include_anomaly_labels=include_anomaly_labels,
        )
        return execute_vm_spawn_plan(self._connections, plan)

    def search_logs(
        self,
        *,
        dataset: str,
        text: str | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        component: str | None = None,
        severity: str | None = None,
        limit: int = DEFAULT_SEARCH_LIMIT,
        match_mode: str = "any",
        request_id: str | None = None,
        instance_id: str | None = None,
    ) -> QueryResult:
        """Retrieve bounded message matches and the complete filtered count."""

        plan = build_search_query_plan(
            dataset=dataset,
            text=text,
            start_time=start_time,
            end_time=end_time,
            component=component,
            severity=severity,
            limit=limit,
            match_mode=match_mode,
            request_id=request_id,
            instance_id=instance_id,
            _dataset_specs=self._dataset_specs,
        )

        with self._connections() as connection:
            with connection.cursor() as cursor:
                cursor.execute(plan.sql, plan.parameters)
                fetched_rows = cursor.fetchall()
                cursor.execute(plan.count_sql, plan.count_parameters)
                count_row = cursor.fetchone()

        if count_row is None:
            raise RuntimeError("SereneDB did not return a message match count")

        truncated = len(fetched_rows) > plan.limit
        rows = fetched_rows[: plan.limit]
        return QueryResult(
            dataset=plan.dataset,
            sql=plan.sql,
            parameters=plan.parameters,
            count_sql=plan.count_sql,
            count_parameters=plan.count_parameters,
            applied_filters=plan.applied_filters,
            rows=rows,
            returned_record_count=len(rows),
            total_corpus_match_count=int(count_row["total_corpus_match_count"]),
            truncated=truncated,
            match_mode=plan.match_mode,
        )

    def get_nearby_events(
        self,
        *,
        dataset: str,
        record_id: int,
        before: int = DEFAULT_NEARBY_EVENTS,
        after: int = DEFAULT_NEARBY_EVENTS,
        same_component: bool = False,
    ) -> NearbyEventsResult:
        """Retrieve deterministic chronological context around a source LineId."""

        plan = build_nearby_query_plan(
            dataset=dataset,
            record_id=record_id,
            before=before,
            after=after,
            same_component=same_component,
            _dataset_specs=self._dataset_specs,
        )
        executed_sql: dict[str, str] = {"anchor": plan.anchor_sql}
        executed_parameters: dict[str, Mapping[str, Any]] = {
            "anchor": plan.anchor_parameters
        }

        with self._connections() as connection:
            with connection.cursor() as cursor:
                cursor.execute(plan.anchor_sql, plan.anchor_parameters)
                anchor = cursor.fetchone()
                if anchor is None:
                    return NearbyEventsResult(
                        dataset=plan.dataset,
                        found=False,
                        anchor=None,
                        before_events=(),
                        after_events=(),
                        requested_before=plan.before,
                        requested_after=plan.after,
                        same_component=plan.same_component,
                        component_filter=None,
                        executed_sql=executed_sql,
                        parameters=executed_parameters,
                        scope_label=plan.scope_label,
                        table=plan.table,
                        development_sample=self._dataset_specs[plan.dataset].development_sample,
                    )

                component_filter = anchor["component"] if plan.same_component else None
                common_parameters: dict[str, Any] = {
                    "anchor_time": anchor["timestamp"],
                    "record_id": plan.record_id,
                }
                if component_filter is not None:
                    common_parameters["component"] = component_filter

                before_rows: list[Mapping[str, Any]] = []
                if plan.before > 0:
                    before_parameters = {
                        **common_parameters,
                        "neighbor_limit": plan.before,
                    }
                    cursor.execute(plan.before_sql, before_parameters)
                    before_rows = list(reversed(cursor.fetchall()))
                    executed_sql["before"] = plan.before_sql
                    executed_parameters["before"] = before_parameters

                after_rows: list[Mapping[str, Any]] = []
                if plan.after > 0:
                    after_parameters = {
                        **common_parameters,
                        "neighbor_limit": plan.after,
                    }
                    cursor.execute(plan.after_sql, after_parameters)
                    after_rows = cursor.fetchall()
                    executed_sql["after"] = plan.after_sql
                    executed_parameters["after"] = after_parameters

        enriched = _add_time_gaps(before_rows, anchor, after_rows)
        before_count = len(before_rows)
        return NearbyEventsResult(
            dataset=plan.dataset,
            found=True,
            anchor=enriched[before_count],
            before_events=enriched[:before_count],
            after_events=enriched[before_count + 1 :],
            requested_before=plan.before,
            requested_after=plan.after,
            same_component=plan.same_component,
            component_filter=component_filter,
            executed_sql=executed_sql,
            parameters=executed_parameters,
            scope_label=plan.scope_label,
            table=plan.table,
            development_sample=self._dataset_specs[plan.dataset].development_sample,
        )

    def summarize_logs(
        self,
        *,
        dataset: str,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        group_by: str,
        interval: str | None = None,
        component: str | None = None,
        severity: str | None = None,
        limit: int = DEFAULT_SUMMARY_LIMIT,
        query: str | None = None,
        match_mode: str = "any",
        sort_direction: str = "asc",
    ) -> AggregationResult:
        """Aggregate the complete filtered table scope in SereneDB."""

        plan = build_aggregation_query_plan(
            dataset=dataset,
            group_by=group_by,
            interval=interval,
            start_time=start_time,
            end_time=end_time,
            component=component,
            severity=severity,
            limit=limit,
            query=query,
            match_mode=match_mode,
            sort_direction=sort_direction,
            _dataset_specs=self._dataset_specs,
        )
        with self._connections() as connection:
            with connection.cursor() as cursor:
                cursor.execute(plan.sql, plan.parameters)
                fetched_groups = cursor.fetchall()
                cursor.execute(plan.count_sql, plan.count_parameters)
                count_row = cursor.fetchone()
                coverage_row = None
                if plan.coverage_sql is not None:
                    cursor.execute(plan.coverage_sql, plan.coverage_parameters)
                    coverage_row = cursor.fetchone()

        if count_row is None:
            raise RuntimeError("SereneDB did not return an aggregation scope count")
        if plan.coverage_sql is not None and coverage_row is None:
            raise RuntimeError("SereneDB did not return ownership mapping coverage")

        truncated = len(fetched_groups) > plan.limit
        groups = fetched_groups[: plan.limit]
        mapping_coverage = (
            _mapping_coverage(coverage_row) if coverage_row is not None else None
        )
        ownership_provenance = None
        if plan.group_by == "team":
            ownership_provenance = {
                "fictional": True,
                "authoritative": False,
                "mapping_version": self._dataset_specs[plan.dataset].ownership_version,
                "mapping_table": self._dataset_specs[plan.dataset].ownership_table,
                "mapping_file": self._dataset_specs[plan.dataset].ownership_file,
                "statement": OWNERSHIP_PROVENANCE,
                "join_type": "LEFT JOIN",
                "unmapped_team_label": UNMAPPED_TEAM,
            }
        return AggregationResult(
            dataset=plan.dataset,
            aggregation_type=plan.aggregation_type,
            group_by=plan.group_by,
            interval=plan.interval,
            sql=plan.sql,
            parameters=plan.parameters,
            count_sql=plan.count_sql,
            count_parameters=plan.count_parameters,
            coverage_sql=plan.coverage_sql,
            coverage_parameters=plan.coverage_parameters,
            applied_filters=plan.applied_filters,
            groups=groups,
            returned_group_count=len(groups),
            total_matching_record_count=int(count_row["total_matching_record_count"]),
            group_limit=plan.limit,
            truncated=truncated,
            scope_label=plan.scope_label,
            table=plan.table,
            mapping_coverage=mapping_coverage,
            ownership_provenance=ownership_provenance,
            development_sample=self._dataset_specs[plan.dataset].development_sample,
        )


def _indexed_message_candidates(spec: DatasetSpec, match_mode: str) -> str:
    """Select stable IDs in the index; evaluate other filters on base rows."""

    return (
        f"{spec.id_expression} IN (SELECT {spec.id_expression} "
        f"FROM {spec.index_relation} WHERE {SEARCH_PREDICATES[match_mode]})"
    )


def _normalize_dataset(
    dataset: str,
    dataset_specs: Mapping[str, DatasetSpec] = DATASET_SPECS,
) -> str:
    if not isinstance(dataset, str):
        raise ValueError("dataset must be a string")
    normalized = dataset.strip().lower()
    if normalized not in dataset_specs:
        supported = ", ".join(spec.name for spec in dataset_specs.values())
        raise ValueError(f"dataset must be one of: {supported}")
    return normalized


def _normalize_group_by(group_by: str) -> str:
    if not isinstance(group_by, str):
        raise ValueError("group_by must be a string")
    normalized = group_by.strip().lower()
    if normalized in {"time", "team"}:
        return normalized
    if normalized not in GROUPING_FIELDS:
        supported = ", ".join((*GROUPING_FIELDS.keys(), "team", "time"))
        raise ValueError(f"group_by must be one of: {supported}")
    return normalized


def _normalize_interval(interval: str | None) -> str | None:
    if interval is None:
        return None
    if not isinstance(interval, str):
        raise ValueError("interval must be a string")
    normalized = interval.strip().lower()
    if normalized not in TIME_INTERVALS:
        supported = ", ".join(TIME_INTERVALS)
        raise ValueError(f"interval must be one of: {supported}")
    return TIME_INTERVALS[normalized]


def _normalize_match_mode(match_mode: str) -> str:
    if not isinstance(match_mode, str):
        raise ValueError("match_mode must be a string")
    normalized = match_mode.strip().lower()
    if normalized not in SEARCH_PREDICATES:
        raise ValueError(f"match_mode must be one of: {', '.join(SEARCH_PREDICATES)}")
    return normalized


def _search_text(text: str, match_mode: str) -> str:
    if match_mode != "exact":
        return _required_text(text, "text")
    if not isinstance(text, str):
        raise ValueError("text must be a string")
    if "\x00" in text:
        raise ValueError("text must not contain NUL characters")
    # Empty/whitespace messages are valid raw values. Never strip exact input.
    return text


def _required_text(value: str, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field} must not be empty")
    if "\x00" in normalized:
        raise ValueError(f"{field} must not contain NUL characters")
    return normalized


def _optional_text(value: str | None, field: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, field)


def _validate_timestamp(field: str, value: datetime | None) -> None:
    if value is None:
        return
    if not isinstance(value, datetime):
        raise ValueError(f"{field} must be a datetime")
    if value.tzinfo is not None and value.utcoffset() is not None:
        raise ValueError(
            f"{field} must not include a timezone because the source logs have none"
        )


def _validate_timestamp_range(
    start_time: datetime | None, end_time: datetime | None
) -> None:
    _validate_timestamp("start_time", start_time)
    _validate_timestamp("end_time", end_time)
    if start_time is not None and end_time is not None and start_time >= end_time:
        raise ValueError("start_time must be earlier than end_time")


def _validate_limit(limit: int, maximum: int) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValueError("limit must be an integer")
    if not 1 <= limit <= maximum:
        raise ValueError(f"limit must be between 1 and {maximum}")


def _validate_record_id(record_id: int) -> None:
    if isinstance(record_id, bool) or not isinstance(record_id, int):
        raise ValueError("record_id must be an integer")
    if record_id < 1:
        raise ValueError("record_id must be greater than zero")


def _validate_neighbor_count(field: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer")
    if not 0 <= value <= MAX_NEARBY_EVENTS:
        raise ValueError(f"{field} must be between 0 and {MAX_NEARBY_EVENTS}")


def _add_time_gaps(
    before_rows: Sequence[Mapping[str, Any]],
    anchor: Mapping[str, Any],
    after_rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    timeline = [*before_rows, anchor, *after_rows]
    anchor_time = anchor["timestamp"]
    enriched: list[dict[str, Any]] = []
    previous_time: datetime | None = None
    for row in timeline:
        timestamp = row["timestamp"]
        enriched.append(
            {
                **row,
                "seconds_from_anchor": (timestamp - anchor_time).total_seconds(),
                "gap_from_previous_seconds": (
                    None
                    if previous_time is None
                    else (timestamp - previous_time).total_seconds()
                ),
            }
        )
        previous_time = timestamp
    return enriched


def _mapping_coverage(row: Mapping[str, Any]) -> dict[str, Any]:
    total_records = int(row["total_record_count"])
    mapped_records = int(row["mapped_record_count"])
    total_components = int(row["total_component_count"])
    mapped_components = int(row["mapped_component_count"])
    return {
        "total_record_count": total_records,
        "mapped_record_count": mapped_records,
        "unmapped_record_count": total_records - mapped_records,
        "mapped_record_percentage": (
            round(mapped_records * 100 / total_records, 2) if total_records else 0.0
        ),
        "total_component_count": total_components,
        "mapped_component_count": mapped_components,
        "unmapped_component_count": total_components - mapped_components,
        "mapped_component_percentage": (
            round(mapped_components * 100 / total_components, 2)
            if total_components
            else 0.0
        ),
    }
