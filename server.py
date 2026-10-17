"""Compatibility entry point for the log-insights MCP server."""

from pathlib import Path

from dotenv import load_dotenv

from log_insights.mcp_server import main


if __name__ == "__main__":
    load_dotenv(Path(__file__).resolve().with_name(".env"))
    main()
