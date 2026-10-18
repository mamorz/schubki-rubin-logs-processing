# RUBIN agent UI (optional)

A browser front end for the RUBIN log tools. It runs **Kiro CLI inside the container** and drives
it over the Agent Client Protocol (`kiro-cli acp`, JSON-RPC on stdio). The judge only signs in
and picks a role. Every rubin tool call is shown with its arguments, executed SQL, bound
parameters, counts and truncation status. The session can be exported as a Markdown worked
investigation.

This is an addition to the documented MCP setup in the main README, not a replacement.
Claude Code, Codex or Kiro can still connect directly to `http://localhost:18000/mcp`.

## Start

```bash
./scripts/agent-ui-up.sh        # runs jury-up.sh, then builds and starts agent-ui
open http://localhost:18080
```

1. Click **Sign in with Kiro**. The page shows a one-time code and a link
   (`kiro-cli login --use-device-flow`, AWS Builder ID). Confirm the code in any browser.
   No port forwarding or API key is needed.
2. Pick **Engineer**, **Team lead** or **Manager** and ask a question, or click a suggestion.
3. Open the tool cards to see the SQL and results. Ledger entries link to their card, and each
   request's **Export** downloads its full report (workflow, results, SQL, answer) as Markdown.

The login is stored in the `schubki-jury_kiro-home` Docker volume and survives restarts.
The sign-out button in the header runs `kiro-cli logout`. Each answer shows the Kiro credits
it used.

Stop or restart with the usual wrapper: `./scripts/jury-compose.sh --profile agent stop agent-ui`.

## How it works

```
browser ──WS /api/session──▶ Fastify (src/server) ──stdio ACP──▶ kiro-cli acp --agent <role>
                                                                       │ MCP (streamable HTTP)
                                                                       ▼
                                                        mcp-server:8000/mcp ──▶ SereneDB
```

- One `kiro-cli acp` process per browser tab and active role. Idle processes stop after 30 minutes.
- **History survives reloads without a database.** The browser keeps the conversation (last 40
  requests and their tool results) in `localStorage`, together with the Kiro session ID of each role.
  After a reload, an idle stop or a role switch, the bridge reopens that session with ACP
  `session/load`. Kiro keeps sessions in the `kiro-home` volume, so the agent also remembers the
  context. History Kiro replays during the load is not sent to the browser a second time. The
  ledger's clear button wipes both and starts fresh sessions.
- Each answer is a workflow log in the order the agent worked: reasoning steps and tool calls on
  a timeline, followed by the answer text. The log folds automatically after 5 tool calls.
- The bridge (`src/server/acpBridge.ts`) uses the official `@agentclientprotocol/sdk`. It
  offers **no** file system or terminal capabilities to the agent, and its permission
  handler allows only `rubin-log-insights` tools.
- Kiro wraps MCP results as `rawOutput.items[].Json.content[].text`. `src/server/toolCalls.ts`
  unwraps them.
- **Tool cards are generic.** `src/shared/toolResult.ts` describes any JSON result by its
  shape, never by tool name. The conventions, recognised at any depth:

  | In the result | Rendered as |
  |---|---|
  | `truncated` / `*_truncated` booleans | truncation badge; nested flags name the section (e.g. "truncated: ranking") |
  | `executed_sql` (`{name: sql}` or a string) + sibling `parameters` | SQL blocks; parameters are matched by name, or apply to all statements if unkeyed |
  | `error`, or `status: "error"/"failed"` | error card (real MCP tool errors arrive as failed calls and are shown the same way) |
  | nested object of mostly numbers (`counts`, `statistics`, `baseline`, …) | labelled stat-tile group; its text values go to details |
  | array of rows with a time-bucket column (`bucket*`, `*_start`, `period`, …) | small multiples: one column chart per metric (≤ 4, each on its own scale) plus a table view |
  | array of rows with one label and one `*count` | bar list |
  | array of rows ordered by `timestamp` | timeline table with a rail and a "Δ prev" gap column |
  | object between `before_*` / `after_*` arrays | one timeline with that row highlighted |
  | columns empty in every row | hidden, but listed under the table as "not available" |
  | `warnings` / `limitations` lists, `*warning` / `*note` strings | notes |
  | `http_status`, `severity`/`level`, `is_*`/`has_*`, `*_id`, time columns | status-coloured / flag chips, IDs kept on one line, readable dates |

  New MCP tools and new fields render without UI changes as long as they follow these conventions.
  `test/mcpContract.test.ts` checks them against real responses of all five tools
  (`test/fixtures/mcp/`, recorded from `feature/advanced-log-analytics`).
- Credits per turn come from Kiro's `_kiro.dev/metadata` extension notification.

## Role agents: contract with the agent definitions

Agents are looked up by name: `engineer`, `team-lead`, `manager`.

- **Workspace agents (preferred):** put them in the repository's `.kiro/agents/`
  (`engineer.json`, `team-lead.json`, `manager.json`, with prompts referenced as
  `"prompt": "file://./prompts/<role>.md"`). The directory is mounted read-only at
  `/workspace/.kiro/agents`, which is the agent's working directory, so Kiro prefers these over the
  bundled ones. The role tab tooltip and `/api/status` show which source is active.
- **Fallback agents:** `agent-ui/fallback-agents/` is copied to `~/.kiro/agents` on every
  container start.
- **Don't hardcode the MCP URL in agent files.** Use `"includeMcpJson": true` and the
  server name `rubin-log-insights`. The container writes its own `~/.kiro/settings/mcp.json`
  pointing at `http://mcp-server:8000/mcp`, while a developer's local
  `.kiro/settings/mcp.json` points at `http://127.0.0.1:18000/mcp`
  (`examples/kiro/mcp.local.example.json`).
- Restrict tools: `"tools": ["@rubin-log-insights"]`, `"allowedTools": ["@rubin-log-insights"]`.
  The UI rejects any other permission request anyway.

Kiro CLI 2.32.0 is pinned (SHA-256 checked in the Dockerfile), and it uses the default v2
agent engine, which reads the JSON format above.

## Development

Host Node is not required; everything can run in `node:22` containers.

```bash
cd agent-ui
npm ci
npm run typecheck && npm test     # vitest; replays recorded Kiro ACP traffic
npm run build
```

`test/fixtures/kiro-acp-session.ndjson` is a real `kiro-cli acp` session that called all three
rubin tools. Tests replay it through the ACP SDK and the bridge.

For local development, run `npm run dev:server` (needs `kiro-cli` on PATH and `RUBIN_MCP_URL`)
and `npm run dev:web` (Vite proxies `/api` to port 8080).

## Limitations

- Requires a Kiro account (AWS Builder ID device flow). Free or promotional credits work.
  API keys are only available on paid plans and are not used by the interactive session.
- Kiro's billing FAQ permits use through "ACP compatible IDEs" and forbids third-party harnesses
  that route requests outside Kiro's native interfaces. This UI drives the official `kiro-cli`
  over ACP and never calls Kiro's backend directly.
- Agent answers are model output. The SQL and counts in the tool cards are the evidence to check them against.
- The page is bound to `127.0.0.1` and has no authentication of its own. Don't expose the port.
