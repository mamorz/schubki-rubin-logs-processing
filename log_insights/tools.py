"""MCP tool definitions and structured response serialization."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from functools import wraps
import logging
from typing import Any, Callable, Literal, Mapping, ParamSpec, TypeVar

import psycopg
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import StrictBool, StrictFloat, StrictInt

from .queries import (
    DEFAULT_NEARBY_EVENTS,
    DEFAULT_SEARCH_LIMIT,
    DEFAULT_SUMMARY_LIMIT,
    AggregationResult,
    NearbyEventsResult,
    QueryResult,
    QueryService,
)

logger = logging.getLogger(__name__)
_P = ParamSpec("_P")
_R = TypeVar("_R")


def _database_errors(function: Callable[_P, _R]) -> Callable[_P, _R]:
    """Expose safe expected failures while retaining diagnostics in server logs."""

    @wraps(function)
    def wrapped(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        try:
            return function(*args, **kwargs)
        except psycopg.Error as error:
            if isinstance(error, psycopg.errors.QueryCanceled):
                message = "SQL execution timed out or was cancelled. Narrow the filters or time range and retry."
                category = "timeout"
            elif isinstance(error, psycopg.InterfaceError) or (
                isinstance(error, psycopg.OperationalError) and (
                    error.sqlstate is None
                    or error.sqlstate.startswith(("08", "28"))
                    or error.sqlstate in {"57P01", "57P02", "57P03", "53300"}
                )
            ):
                message = "Database connection unavailable. Check that SereneDB is running and reachable, then retry."
                category = "connection"
            else:
                message = "Database query failed. Retry; if it persists, ask the operator to check the server logs."
                category = "execution"
            logger.warning("Database failure in %s (%s, SQLSTATE=%s)",
                           function.__name__, category, error.sqlstate, exc_info=True)
            raise ToolError(message) from error

    return wrapped


@dataclass(frozen=True)
class PlannedTool:
    name: str
    purpose: str


PLANNED_TOOLS: tuple[PlannedTool, ...] = (
    PlannedTool(
        "search_logs", "Find bounded full-text log evidence with explicit filters."
    ),
    PlannedTool("get_nearby_events", "Retrieve events around a selected log entry."),
    PlannedTool("summarize_logs", "Calculate complete-scope counts and time trends."),
    PlannedTool("analyze_vm_spawn_performance", "Measure the complete OpenStack spawn-duration population and discover slow events."),
    PlannedTool("detect_log_spikes", "Screen complete BGL component bucket populations for elevated log volumes."),
)


def register_tools(server: MCPServer, queries: QueryService) -> None:
    """Register health, search, aggregation, and chronological-context tools."""

    @server.tool()
    @_database_errors
    def health_check() -> dict[str, str]:
        """Confirm that the MCP server is running without querying SereneDB."""

        return {
            "status": "ok",
            "database": "not checked",
            "log_analysis_tools": "search_logs,summarize_logs,get_nearby_events",
        }

    @server.tool()
    @_database_errors
    def search_logs(
        dataset: str,
        query: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
        component: str | None = None,
        severity: str | None = None,
        limit: int = DEFAULT_SEARCH_LIMIT,
        match_mode: Literal["any", "phrase", "exact", "all"] = "any",
        request_id: str | None = None,
        instance_id: str | None = None,
    ) -> dict[str, Any]:
        """Search BGL or OpenStack messages with optional filters and match_mode.

        any (default): existing OR-based full-text search, matching any query token.
        phrase: consecutive tokens in order anywhere in the message, using the
        index dictionary's normalization (case/punctuation/spacing may differ).
        exact: equality with the ENTIRE stored raw message, preserving case,
        punctuation, and all whitespace; not a substring or token phrase. Copy a
        returned message verbatim for exact lookup; empty messages are allowed.
        all: every query token must occur, in any order, without adjacency.

        SQL-backed OpenStack also supports literal request_id and instance_id
        equality filters, combined with AND. Omit query only when at least one
        identifier is supplied; a supplied query additionally filters messages.
        These filters are unavailable for BGL and sample tables. OpenStack SQL
        records include request_id, instance_id, service, http_status, latency_s,
        and is_anomaly; missing values remain null.

        instance_id usually covers compute records only: API URLs can mention
        the VM without setting this column. For message mentions, use a separate
        phrase query for the full UUID and inspect its evidence. Neither lookup
        guarantees a complete VM lifecycle. Background request contexts can span
        unrelated VMs; do not expand them into a VM trace. is_anomaly is a stored
        injected-VM label, not a detector; slow boots can lack error messages.

        Timestamps must be ISO 8601 values without timezone information. Start is
        inclusive and end is exclusive. Results default to 20 and are capped at 50.
        total_corpus_match_count covers all filtered matches, independently of
        limit. Returns match_mode, executed SQL, bound parameters and truncation.
        """

        try:
            result = queries.search_logs(
                dataset=dataset,
                text=query,
                start_time=_parse_timestamp("start_time", start_time),
                end_time=_parse_timestamp("end_time", end_time),
                component=component,
                severity=severity,
                limit=limit,
                match_mode=match_mode,
                request_id=request_id,
                instance_id=instance_id,
            )
        except ValueError as error:
            raise ToolError(str(error)) from error
        return _serialize_query_result(result)

    @server.tool()
    @_database_errors
    def summarize_logs(
        dataset: str,
        group_by: str,
        interval: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
        component: str | None = None,
        severity: str | None = None,
        limit: int = DEFAULT_SUMMARY_LIMIT,
        query: str | None = None,
        match_mode: Literal["any", "phrase", "exact", "all"] = "any",
        sort_direction: Literal["asc", "desc"] = "asc",
    ) -> dict[str, Any]:
        """Count logs by component, fictional owner team, severity, event_id, or time.

        Use group_by='team' for a LEFT JOIN to the configured fictional ownership map;
        unmatched components remain visible as UNMAPPED. Use group_by='time' with
        interval='hour', 'day', 'week' (Monday start), or 'month' (calendar start).
        Time buckets default to ascending order (earliest first); use
        sort_direction='desc' for newest first. A truncated time series omits
        periods beyond limit; narrow the time range or increase limit to see more.
        Counts cover the complete filtered configured
        corpus; limit only bounds returned groups. Timestamps are naive ISO 8601
        values with inclusive start and exclusive end.

        Optional query searches messages with the same semantics as search_logs:
        any (default) matches any token; all requires every token in any order;
        phrase matches consecutive tokens in order using index normalization;
        exact compares the ENTIRE stored raw message, including case, punctuation,
        and whitespace. Omit query to aggregate all filtered logs. Empty query is
        valid only for exact. Totals and fictional ownership coverage use every
        matching record, independently of limit; truncated flags omitted groups.
        """

        try:
            result = queries.summarize_logs(
                dataset=dataset,
                group_by=group_by,
                interval=interval,
                start_time=_parse_timestamp("start_time", start_time),
                end_time=_parse_timestamp("end_time", end_time),
                component=component,
                severity=severity,
                limit=limit,
                query=query,
                match_mode=match_mode,
                sort_direction=sort_direction,
            )
        except ValueError as error:
            raise ToolError(str(error)) from error
        return _serialize_aggregation_result(result)

    @server.tool()
    @_database_errors
    def analyze_vm_spawn_performance(
        dataset: Literal["openstack"] = "openstack",
        start_time: str | None = None, end_time: str | None = None,
        slow_threshold_seconds: StrictFloat | None = None,
        baseline_multiplier: StrictFloat = 1.25,
        baseline_scope: Literal["corpus", "analysis"] = "corpus",
        interval: Literal["hour", "day"] | None = "day",
        top_limit: StrictInt = 10, bucket_limit: StrictInt = 100,
        sort_direction: Literal["asc", "desc"] = "desc",
        include_anomaly_labels: StrictBool = False,
    ) -> dict[str, Any]:
        """Discover slow OpenStack VM spawns without knowing an instance ID.

        SQL-backed OpenStack only. Exact count, mean, median, P95, P99, min and
        max cover ALL valid measured spawn-duration log events, independently of
        output limits. Events are not deduplicated by VM; unique VM counts are
        separate. This measures hypervisor spawn duration, not total boot time.

        Slow means unrounded duration strictly greater than slow_threshold_seconds
        when supplied; otherwise baseline P95 * baseline_multiplier (default 1.25).
        baseline_scope='corpus' uses all loaded spawn events even when the analysis
        is time-filtered; 'analysis' uses that range. No anomaly labels are excluded
        from the baseline. include_anomaly_labels only adds stored is_anomaly
        metadata; labels never affect detection or ranking.

        Times are naive ISO 8601, start inclusive/end exclusive. Hour/day buckets
        use completion-log time and the same threshold; empty periods are omitted.
        Newest buckets come first by default. top_limit is 1..50, bucket_limit
        1..100. Malformed durations are counted and excluded from statistics.
        Missing/zero baselines disable relative detection explicitly. Returns
        complete counts, separate truncation flags, SQL and bound parameters.
        """
        try:
            return queries.analyze_vm_spawn_performance(
                dataset=dataset, start_time=_parse_timestamp("start_time", start_time),
                end_time=_parse_timestamp("end_time", end_time),
                slow_threshold_seconds=slow_threshold_seconds, baseline_multiplier=baseline_multiplier,
                baseline_scope=baseline_scope, interval=interval, top_limit=top_limit,
                bucket_limit=bucket_limit, sort_direction=sort_direction,
                include_anomaly_labels=include_anomaly_labels,
            )
        except ValueError as error:
            raise ToolError(str(error)) from error

    @server.tool()
    @_database_errors
    def detect_log_spikes(
        dataset: Literal["bgl"] = "bgl",
        start_time: str | None = None, end_time: str | None = None,
        component: str | None = None, interval: Literal["hour", "day"] = "hour",
        signal: Literal["severity_volume", "total_volume"] = "severity_volume",
        severities: list[str] | None = None,
        baseline_scope: Literal["corpus", "reference"] = "corpus",
        baseline_start_time: str | None = None, baseline_end_time: str | None = None,
        baseline_quantile: StrictFloat = 0.99, threshold_multiplier: StrictFloat = 1.5,
        min_bucket_count: StrictInt = 20, min_baseline_buckets: StrictInt = 100,
        max_findings: StrictInt = 10, patterns_per_finding: StrictInt = 3, evidence_per_pattern: StrictInt = 1,
    ) -> dict[str, Any]:
        """Discover BGL log-volume spikes without specifying suspicious windows.

        SQL-backed BGL only. Default severity_volume includes WARNING, ERROR,
        SEVERE, FAILURE and FATAL; severities optionally selects BGL levels.
        total_volume includes every level and rejects severities. Each component
        baseline uses whole component-active hour/day buckets; INFO-only buckets
        are valid zero adverse observations. Missing periods are never zero-filled.

        Elevated means selected count >= min_bucket_count AND strictly greater
        than the baseline's empirical quantile count * threshold_multiplier
        (default P99 * 1.5), with >=100 baseline buckets by default.
        This is a screening heuristic, not a calibrated anomaly probability.
        Labels never affect detection. Insufficient history remains explicit.

        corpus baselines are retrospective and include the analysis. reference
        requires explicit disjoint analysis and baseline ranges. Timestamps are
        naive ISO 8601, start inclusive/end exclusive. Partial boundary buckets
        and globally mixed rounded source-clock offsets are excluded. Contiguous
        elevated buckets merge only within the same component and never across
        missing, excluded or non-elevated buckets. Boundaries are log windows,
        not incident onset/resolution. Spikes do not prove outages or root causes.

        Complete counts precede limits. max_findings is 1..20; patterns_per_finding
        1..5; evidence_per_pattern 1..3. Exact message/severity contributors carry
        full counts, percentages, omitted-message remainders and original record
        references. Long messages are explicitly truncated at 1000 characters.
        Returns SQL, bound parameters, coverage diagnostics and truncation flags.
        """
        try:
            return queries.detect_log_spikes(dataset=dataset,
                start_time=_parse_timestamp("start_time", start_time), end_time=_parse_timestamp("end_time", end_time),
                component=component, interval=interval, signal=signal, severities=severities,
                baseline_scope=baseline_scope,
                baseline_start_time=_parse_timestamp("baseline_start_time", baseline_start_time),
                baseline_end_time=_parse_timestamp("baseline_end_time", baseline_end_time),
                baseline_quantile=baseline_quantile, threshold_multiplier=threshold_multiplier,
                min_bucket_count=min_bucket_count, min_baseline_buckets=min_baseline_buckets,
                max_findings=max_findings, patterns_per_finding=patterns_per_finding, evidence_per_pattern=evidence_per_pattern)
        except ValueError as error:
            raise ToolError(str(error)) from error

    @server.tool()
    @_database_errors
    def get_nearby_events(
        dataset: str,
        record_id: int,
        before: int = DEFAULT_NEARBY_EVENTS,
        after: int = DEFAULT_NEARBY_EVENTS,
        same_component: bool = False,
    ) -> dict[str, Any]:
        """Return chronological context around a record_id returned by search_logs.

        Neighbors are ordered by timestamp and record_id. Set same_component to true
        to restrict context to the anchor's component. Temporal proximity is
        evidence of sequence only and does not establish causation. This is global
        chronological context, not a request or VM trace; unrelated API polls may
        appear. Use search_logs identifier filters for literal OpenStack lookups.
        """

        try:
            result = queries.get_nearby_events(
                dataset=dataset,
                record_id=record_id,
                before=before,
                after=after,
                same_component=same_component,
            )
        except ValueError as error:
            raise ToolError(str(error)) from error
        return _serialize_nearby_events(result)


def _parse_timestamp(field: str, value: str | None) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty ISO 8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError as error:
        raise ValueError(
            f"{field} must be an ISO 8601 timestamp without timezone information"
        ) from error
    if parsed.tzinfo is not None and parsed.utcoffset() is not None:
        raise ValueError(
            f"{field} must not include a timezone because the source logs have none"
        )
    return parsed


def _serialize_query_result(result: QueryResult) -> dict[str, Any]:
    return {
        "match_mode": result.match_mode,
        "records": [_json_mapping(row) for row in result.rows],
        "returned_record_count": result.returned_record_count,
        "total_corpus_match_count": result.total_corpus_match_count,
        "truncated": result.truncated,
        "provenance": {
            "dataset": result.dataset,
            "applied_filters": _json_mapping(result.applied_filters),
            "executed_sql": {
                "records": result.sql,
                "total_count": result.count_sql,
            },
            "parameters": {
                "records": _json_mapping(result.parameters),
                "total_count": _json_mapping(result.count_parameters),
            },
        },
    }


def _serialize_aggregation_result(result: AggregationResult) -> dict[str, Any]:
    response = {
        "dataset": result.dataset,
        "aggregation_type": result.aggregation_type,
        "group_by": result.group_by,
        "interval": result.interval,
        "applied_filters": _json_mapping(result.applied_filters),
        "groups": [_json_mapping(group) for group in result.groups],
        "returned_group_count": result.returned_group_count,
        "total_matching_record_count": result.total_matching_record_count,
        "group_limit": result.group_limit,
        "truncated": result.truncated,
        "scope": {
            "source": result.scope_label,
            "table": result.table,
            "complete_filtered_scope_aggregated": True,
            "count_unit": "log records, not distinct incidents",
            "development_sample": result.development_sample,
        },
        "provenance": {
            "executed_sql": {
                "aggregation": result.sql,
                "total_count": result.count_sql,
            },
            "parameters": {
                "aggregation": _json_mapping(result.parameters),
                "total_count": _json_mapping(result.count_parameters),
            },
        },
    }
    if result.coverage_sql is not None:
        response["mapping_coverage"] = _json_mapping(result.mapping_coverage or {})
        response["provenance"]["fictional_ownership"] = _json_mapping(
            result.ownership_provenance or {}
        )
        response["provenance"]["executed_sql"]["mapping_coverage"] = (
            result.coverage_sql
        )
        response["provenance"]["parameters"]["mapping_coverage"] = _json_mapping(
            result.coverage_parameters
        )
    return response


def _serialize_nearby_events(result: NearbyEventsResult) -> dict[str, Any]:
    returned_before = len(result.before_events)
    returned_after = len(result.after_events)
    return {
        "status": "ok" if result.found else "anchor_not_found",
        "dataset": result.dataset,
        "anchor": _json_mapping(result.anchor) if result.anchor is not None else None,
        "before_events": [_json_mapping(row) for row in result.before_events],
        "after_events": [_json_mapping(row) for row in result.after_events],
        "counts": {
            "requested_before": result.requested_before,
            "returned_before": returned_before,
            "requested_after": result.requested_after,
            "returned_after": returned_after,
            "insufficient_before": returned_before < result.requested_before,
            "insufficient_after": returned_after < result.requested_after,
        },
        "applied_filters": {
            "same_component": result.same_component,
            "component": result.component_filter,
        },
        "ordering": ["timestamp ASC", "record_id ASC"],
        "gap_semantics": {
            "seconds_from_anchor": "signed timestamp difference from the anchor",
            "gap_from_previous_seconds": (
                "timestamp difference from the previous returned timeline event"
            ),
        },
        "scope": {
            "source": result.scope_label,
            "table": result.table,
            "development_sample": result.development_sample,
            "anchor_identifier": (
                "record_id is source LineId, not EventId" if result.development_sample
                else "BGL: line_no; OpenStack: file ordinal * 1000000000 + line_no "
                "(abnormal=1, normal1=2, normal2=3); use IDs returned by search_logs"
            ),
        },
        "causation_warning": (
            "Temporal proximity shows sequence; it does not prove causation."
        ),
        "provenance": {
            "executed_sql": dict(result.executed_sql),
            "parameters": {
                name: _json_mapping(parameters)
                for name, parameters in result.parameters.items()
            },
        },
    }


def _json_mapping(values: Mapping[str, Any]) -> dict[str, Any]:
    return {key: _json_value(value) for key, value in values.items()}


def _json_value(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value
