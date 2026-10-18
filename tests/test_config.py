import pytest
from pathlib import Path
import runpy

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


def test_sql_timeout_default_and_override(monkeypatch):
    monkeypatch.delenv("MCP_SQL_TIMEOUT_SECONDS", raising=False)
    assert Settings.from_env().sql_timeout_seconds == 15
    monkeypatch.setenv("MCP_SQL_TIMEOUT_SECONDS", "0.125")
    assert Settings.from_env().sql_timeout_seconds == .125


@pytest.mark.parametrize("value", ["", "bad", "0", "-1", "nan", "inf", "0.0001", "3601"])
def test_invalid_sql_timeout_fails_configuration(monkeypatch, value):
    monkeypatch.setenv("MCP_SQL_TIMEOUT_SECONDS", value)
    with pytest.raises(ValueError, match="MCP_SQL_TIMEOUT_SECONDS"):
        Settings.from_env()


@pytest.mark.parametrize("value", [True, None, "15", float("nan"), 0])
def test_direct_settings_also_validate_timeout(value):
    with pytest.raises(ValueError, match="MCP_SQL_TIMEOUT_SECONDS"):
        Settings(None, sql_timeout_seconds=value)


def test_entrypoint_loads_only_this_worktrees_env(monkeypatch):
    entrypoint = Path(__file__).resolve().parents[1] / "server.py"
    loaded_paths = []
    started = []
    monkeypatch.setattr("dotenv.load_dotenv", lambda path: loaded_paths.append(path))
    monkeypatch.setattr("log_insights.mcp_server.main", lambda: started.append(True))
    runpy.run_path(str(entrypoint), run_name="__main__")
    assert loaded_paths == [entrypoint.with_name(".env")]
    assert started == [True]
