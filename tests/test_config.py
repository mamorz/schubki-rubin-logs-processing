import pytest

from log_insights.config import Settings


def test_settings_do_not_invent_a_database_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SERENEDB_DSN", raising=False)

    settings = Settings.from_env()

    assert settings.serenedb_dsn is None
    with pytest.raises(RuntimeError, match="SERENEDB_DSN"):
        settings.require_serenedb_dsn()


def test_http_transport_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_TRANSPORT", "streamable-http")
    monkeypatch.setenv("MCP_PORT", "9000")
    monkeypatch.setenv("MCP_PATH", "/logs-mcp")

    settings = Settings.from_env()

    assert settings.mcp_transport == "streamable-http"
    assert settings.mcp_port == 9000
    assert settings.mcp_path == "/logs-mcp"
