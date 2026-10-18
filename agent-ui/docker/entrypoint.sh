#!/bin/sh
# Seeds the container's Kiro config on every start; the login itself lives in the home volume.
set -eu
mkdir -p "$HOME/.kiro/settings" "$HOME/.kiro/agents"

# The rubin MCP server as seen from inside the Compose network (the host uses 127.0.0.1:18000).
cat > "$HOME/.kiro/settings/mcp.json" <<JSON
{
  "mcpServers": {
    "${RUBIN_MCP_SERVER_NAME:-rubin-log-insights}": {
      "url": "${RUBIN_MCP_URL}",
      "disabled": false
    }
  }
}
JSON

# Bundled role agents; teammates' agents in /workspace/.kiro/agents take precedence.
cp -R /opt/agent-ui/fallback-agents/. "$HOME/.kiro/agents/"

kiro-cli settings app.disableAutoupdates true >/dev/null 2>&1 || true
kiro-cli settings telemetry.enabled false >/dev/null 2>&1 || true

exec "$@"
