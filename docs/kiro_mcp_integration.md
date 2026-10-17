# Kiro MCP integration

The RUBIN Log Insights server exposes MCP Streamable HTTP at `/mcp`. It has no
application-level authentication, so Docker Compose publishes it to loopback only
by default. Never expose the container port directly to the public internet.

## Start the server

Copy `.env.example` to `.env` and set `SERENEDB_DOCKER_DSN` to the connection
string that works from a container. On macOS, a host-local SereneDB instance
normally uses `host.docker.internal` rather than `127.0.0.1` in this value.

```bash
docker compose up --build -d
docker compose ps
docker compose logs --tail=100 mcp-server
curl --fail http://127.0.0.1:8000/healthz
python -m scripts.verify_mcp_http --url http://127.0.0.1:8000/mcp
```

The default publication is exactly `127.0.0.1:8000:8000`. Override the host-side
port with `MCP_PUBLISHED_PORT`; keep `MCP_PUBLISH_HOST=127.0.0.1` unless a reviewed
private-network and authentication design is already active. `/healthz` checks
only process and HTTP readiness. The MCP `health_check` tool behaves similarly;
the HTTP verification command also runs read-only log queries and therefore
verifies the SereneDB connection.

Compose uses `restart: unless-stopped`, an HTTP health check, a read-only root
filesystem, a temporary `/tmp`, a non-root image user, and `no-new-privileges`.
Inspect health with:

```bash
docker inspect --format '{{.State.Health.Status}}' \
  schubki-rubin-logs-processing-mcp-server-1
```

## Kiro configuration on the same computer

Kiro's workspace configuration is `.kiro/settings/mcp.json`; its user-level
configuration is `~/.kiro/settings/mcp.json`. Copy
`examples/kiro/mcp.local.example.json` to one of those locations, or add this
entry to an existing file:

```json
{
  "mcpServers": {
    "rubin-log-insights": {
      "url": "http://127.0.0.1:8000/mcp",
      "disabled": false,
      "autoApprove": []
    }
  }
}
```

Leave `autoApprove` empty initially so Kiro asks before tool use. After saving,
open Kiro's MCP servers panel and confirm that `rubin-log-insights` connects and
lists `health_check`, `search_logs`, `summarize_logs`, and `get_nearby_events`.
Kiro accepts plain HTTP for localhost; non-local remote endpoints should use HTTPS.

## Recommended connection from another computer

Use an SSH local-forward tunnel. This keeps MCP and SereneDB off the public
network and requires only SSH access to the computer running Docker:

```bash
ssh -N -L 18000:127.0.0.1:8000 USER@MCP_HOST
```

On the teammate's computer, use the same Kiro JSON but set:

```json
"url": "http://127.0.0.1:18000/mcp"
```

Keep the SSH process running while Kiro uses the server. Prefer SSH keys, restrict
which accounts can log in, and limit the host firewall to expected SSH sources.
No SereneDB port needs to be exposed: only the MCP host connects to the database.

## Alternative protected remote deployment

If an SSH tunnel is unsuitable, first put the MCP service behind a trusted HTTPS
reverse proxy or private access gateway. The gateway must authenticate requests
using OAuth, mTLS, or a strong bearer token; restrict source networks where
possible; rate-limit requests; preserve MCP streaming; and keep both container
port 8000 and SereneDB private. A Kiro entry for a gateway that enforces bearer
authentication has this shape:

```json
{
  "mcpServers": {
    "rubin-log-insights": {
      "url": "https://PRIVATE_MCP_HOST.example/mcp",
      "headers": {
        "Authorization": "Bearer ${RUBIN_MCP_TOKEN}"
      },
      "disabled": false,
      "autoApprove": []
    }
  }
}
```

This example is not usable until the gateway, DNS, trusted TLS certificate,
authentication, firewall, and token distribution are configured. Kiro requires
approval before expanding a referenced environment variable. Do not place a real
token in a repository file.

Kiro's current remote-server format and security guidance are documented in its
[MCP configuration](https://kiro.dev/docs/mcp/configuration/) and
[MCP security](https://kiro.dev/docs/mcp/security/) guides.

## Troubleshooting

- `curl /healthz` succeeds but MCP queries fail: inspect the container DSN host and
  verify that SereneDB is reachable from inside Docker.
- Kiro cannot connect through SSH: verify the tunnel is still running and that the
  configured local port matches the `-L` argument.
- Port 8000 is already in use: set `MCP_PUBLISHED_PORT=18000` in `.env` and update
  the local Kiro URL.
- A direct remote HTTP URL is rejected: use the localhost SSH tunnel or configure
  a trusted HTTPS gateway; do not weaken transport security.
