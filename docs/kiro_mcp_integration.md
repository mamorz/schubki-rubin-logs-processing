# Kiro MCP integration

The RUBIN Log Insights server exposes MCP Streamable HTTP at `/mcp`. It has no
application-level authentication, so Docker Compose publishes it to loopback only
by default. Never expose the container port directly to the public internet.

## Start the server

Follow the [jury setup in README](../README.md). The guarded setup initializes
only an empty application database and reuses a complete corpus on later runs.
Compose connects MCP only to its own `serenedb` service; external DSNs are ignored.
No credentials need to reach Kiro, and Tailscale is not required locally.

```bash
./scripts/jury-up.sh
./scripts/jury-compose.sh ps
./scripts/jury-compose.sh logs --tail=100 mcp-server
curl --fail http://127.0.0.1:18000/healthz
./scripts/jury-compose.sh exec -T mcp-server python -m scripts.verify_mcp_http --url http://127.0.0.1:8000/mcp
```

Every Compose operation uses the explicit `schubki-jury` project through the
wrapper. The publication is `127.0.0.1:18000:8000`. `/healthz` and `health_check`
check process readiness; HTTP verification also runs read-only database queries.
The server uses a read-only filesystem, a non-root user, and an HTTP health check.

## Kiro configuration on the same computer

Kiro's workspace configuration is `.kiro/settings/mcp.json`; its user-level
configuration is `~/.kiro/settings/mcp.json`. Copy
`examples/kiro/mcp.local.example.json` to one of those locations, or add this
entry to an existing file:

```json
{
  "mcpServers": {
    "rubin-log-insights": {
      "url": "http://127.0.0.1:18000/mcp",
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
ssh -N -L 127.0.0.1:28000:127.0.0.1:18000 USER@MCP_HOST
```

On the teammate's computer, use the same Kiro JSON but set:

```json
"url": "http://127.0.0.1:28000/mcp"
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
- Port 18000 is already in use: select another `MCP_PUBLISHED_PORT` in `.env` and update
  the local Kiro URL.
- A direct remote HTTP URL is rejected: use the localhost SSH tunnel or configure
  a trusted HTTPS gateway; do not weaken transport security.
