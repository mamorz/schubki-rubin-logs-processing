"""MCP server composition and transport startup."""

from __future__ import annotations

from typing import Mapping

from mcp.server import MCPServer
from starlette.requests import Request
from starlette.responses import JSONResponse

from .config import Settings
from .db import connection_factory
from .queries import DATASET_SPECS, SQL_DATASET_SPECS, DatasetSpec, QueryService
from .tools import register_tools


def build_server(
    settings: Settings | None = None,
    dataset_specs: Mapping[str, DatasetSpec] | None = None,
) -> MCPServer:
    """Build the server without opening a database connection."""

    resolved_settings = settings or Settings.from_env()
    server = MCPServer(
        "RUBIN Log Insights",
        title="RUBIN Log Insights",
        description="Read-only search and aggregation over BGL and OpenStack logs.",
        instructions=(
            "Use search_logs for bounded evidence, summarize_logs for aggregate "
            "counts, and get_nearby_events for chronological context. Team ownership "
            "is explicitly fictional and unmapped components remain visible. "
            "Use analyze_vm_spawn_performance to discover slow VM spawns and "
            "calculate exact fleet duration baselines without knowing a VM ID."
            " Use detect_log_spikes to screen BGL components for elevated log "
            "volumes with baseline comparisons and original-record evidence."
        ),
        version="1.0.0",
    )

    @server.custom_route(
        "/healthz",
        methods=["GET"],
        name="transport-health",
        include_in_schema=False,
    )
    async def transport_health(_: Request) -> JSONResponse:
        """Report process readiness without opening a database connection."""

        return JSONResponse(
            {
                "status": "ok",
                "service": "rubin-log-insights-mcp",
                "database": "not checked",
            }
        )

    queries = QueryService(
        connection_factory(resolved_settings),
        dataset_specs=(
            dataset_specs if dataset_specs is not None else
            SQL_DATASET_SPECS if resolved_settings.dataset_backend == "sql" else DATASET_SPECS
        ),
    )
    register_tools(server, queries)
    return server


def main() -> None:
    """Start the configured MCP transport."""

    settings = Settings.from_env()
    server = build_server(settings)

    if settings.mcp_transport == "stdio":
        server.run(transport="stdio")
        return

    server.run(
        transport="streamable-http",
        host=settings.mcp_host,
        port=settings.mcp_port,
        streamable_http_path=settings.mcp_path,
    )
