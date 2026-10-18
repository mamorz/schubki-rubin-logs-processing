import { homedir } from "node:os";
import { join } from "node:path";

export const config = {
  host: process.env.HOST ?? "0.0.0.0",
  port: Number(process.env.PORT ?? 8080),
  kiroBin: process.env.KIRO_CLI_BIN ?? "kiro-cli",
  /** Session cwd; workspace agents are read from <workspace>/.kiro/agents. */
  workspace: process.env.AGENT_WORKSPACE ?? "/workspace",
  globalAgentsDir: join(homedir(), ".kiro", "agents"),
  mcpServerName: process.env.RUBIN_MCP_SERVER_NAME ?? "rubin-log-insights",
  mcpUrl: process.env.RUBIN_MCP_URL ?? "http://mcp-server:8000/mcp",
  staticDir: process.env.STATIC_DIR ?? new URL("../../web", import.meta.url).pathname,
  /** Idle agent processes are stopped after this many milliseconds. */
  idleTimeoutMs: Number(process.env.AGENT_IDLE_TIMEOUT_MS ?? 30 * 60_000),
};

export function mcpHealthUrl(mcpUrl: string): string {
  const url = new URL(mcpUrl);
  url.pathname = url.pathname.replace(/\/mcp\/?$/, "") + "/healthz";
  return url.toString();
}
