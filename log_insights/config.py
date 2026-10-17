"""Environment-based configuration without embedded credentials or schema details."""

from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Literal, cast

McpTransport = Literal["stdio", "streamable-http"]
_SUPPORTED_TRANSPORTS = {"stdio", "streamable-http"}


@dataclass(frozen=True)
class Settings:
    """Runtime settings loaded from environment variables."""

    serenedb_dsn: str | None
    mcp_transport: McpTransport = "stdio"
    mcp_host: str = "127.0.0.1"
    mcp_port: int = 8000
    mcp_path: str = "/mcp"
    dataset_backend: str = "sql"

    @classmethod
    def from_env(cls) -> "Settings":
        backend = os.getenv("MCP_DATASET_BACKEND", "sql")
        if backend not in {"sql", "sample"}:
            raise ValueError("MCP_DATASET_BACKEND must be sql or sample")
        transport = os.getenv("MCP_TRANSPORT", "stdio")
        if transport not in _SUPPORTED_TRANSPORTS:
            supported = ", ".join(sorted(_SUPPORTED_TRANSPORTS))
            raise ValueError(f"MCP_TRANSPORT must be one of: {supported}")

        port = int(os.getenv("MCP_PORT", "8000"))
        if not 1 <= port <= 65535:
            raise ValueError("MCP_PORT must be between 1 and 65535")

        path = os.getenv("MCP_PATH", "/mcp")
        if not path.startswith("/"):
            raise ValueError("MCP_PATH must start with '/'")

        return cls(
            serenedb_dsn=os.getenv("SERENEDB_DSN") or None,
            mcp_transport=cast(McpTransport, transport),
            mcp_host=os.getenv("MCP_HOST", "127.0.0.1"),
            mcp_port=port,
            mcp_path=path,
            dataset_backend=backend,
        )

    def require_serenedb_dsn(self) -> str:
        """Return the supplied DSN or fail with an actionable setup message."""

        if not self.serenedb_dsn:
            raise RuntimeError(
                "SERENEDB_DSN is not configured. Copy .env.example to .env and "
                "insert the connection string supplied by the organizers."
            )
        return self.serenedb_dsn
