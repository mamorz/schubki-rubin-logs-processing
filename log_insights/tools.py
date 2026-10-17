"""MCP tool definitions and structured response serialization."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Mapping

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from .queries import (
    DEFAULT_NEARBY_EVENTS,
    DEFAULT_SEARCH_LIMIT,
    DEFAULT_SUMMARY_LIMIT,
    AggregationResult,
    NearbyEventsResult,
    QueryResult,
    QueryService,
)


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
)


def register_tools(server: MCPServer, queries: QueryService) -> None:
    """Register health, search, aggregation, and chronological-context tools."""

    @server.tool()
    def health_check() -> dict[str, str]:
        """Confirm that the MCP server is running without querying SereneDB."""

        return {
            "status": "ok",
            "database": "not checked",
            "log_analysis_tools": "search_logs,summarize_logs,get_nearby_events",
        }

    @server.tool()
    def search_logs(
        dataset: str,
        query: str,
        start_time: str | None = None,
        end_time: str | None = None,
        component: str | None = None,
        severity: str | None = None,
        limit: int = DEFAULT_SEARCH_LIMIT,
    ) -> dict[str, Any]:
        """Search BGL or OpenStack messages using optional exact filters.

        Timestamps must be ISO 8601 values without timezone information. Start is
        inclusive and end is exclusive. Results default to 20 and are capped at 50.
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
            )
        except ValueError as error:
            raise ToolError(str(error)) from error
        return _serialize_query_result(result)

    @server.tool()
    def summarize_logs(
        dataset: str,
        group_by: str,
        interval: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
        component: str | None = None,
        severity: str | None = None,
        limit: int = DEFAULT_SUMMARY_LIMIT,
    ) -> dict[str, Any]:
        """Count logs by component, fictional owner team, severity, event_id, or time.

        Use group_by='team' for a LEFT JOIN to the configured fictional ownership map;
        unmatched components remain visible as UNMAPPED. Use group_by='time' with
        interval='hour' or 'day'. Counts cover the complete filtered configured
        corpus; limit only bounds returned groups. Timestamps are naive ISO 8601
        values with inclusive start and exclusive end.
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
            )
        except ValueError as error:
            raise ToolError(str(error)) from error
        return _serialize_aggregation_result(result)

    @server.tool()
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
        evidence of sequence only and does not establish causation.
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
