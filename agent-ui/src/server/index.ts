import Fastify from "fastify";
import fastifyStatic from "@fastify/static";
import fastifyWebsocket from "@fastify/websocket";
import type { WebSocket } from "ws";
import { ROLES, isRoleId, type ClientMessage, type RoleId, type ServerMessage, type StatusResponse } from "../shared/events.js";
import { AgentSession, spawnKiroTransport } from "./acpBridge.js";
import { listAgents } from "./agents.js";
import { KiroAuth } from "./auth.js";
import { config, mcpHealthUrl } from "./config.js";

const app = Fastify({ logger: { level: process.env.LOG_LEVEL ?? "info" } });
const auth = new KiroAuth(config.kiroBin);

await app.register(fastifyWebsocket, { options: { maxPayload: 64 * 1024 } });
await app.register(fastifyStatic, { root: config.staticDir, wildcard: false });

async function mcpHealth(): Promise<StatusResponse["mcp"]> {
  try {
    const response = await fetch(mcpHealthUrl(config.mcpUrl), { signal: AbortSignal.timeout(3000) });
    return { ok: response.ok, url: config.mcpUrl, detail: response.ok ? null : `HTTP ${response.status}` };
  } catch (error) {
    return { ok: false, url: config.mcpUrl, detail: (error as Error).message };
  }
}

app.get("/api/status", async (): Promise<StatusResponse> => {
  const [authState, mcp, version, agents] = await Promise.all([
    auth.state(),
    mcpHealth(),
    auth.version(),
    listAgents(config.workspace),
  ]);
  return { auth: authState, mcp, kiro: { version }, agents };
});

app.post("/api/auth/login", async (_request, reply) => {
  try {
    await auth.startLogin();
  } catch (error) {
    return reply.code(502).send({ error: (error as Error).message });
  }
  return auth.state();
});

app.post("/api/auth/cancel", async () => {
  auth.cancelLogin();
  return auth.state();
});

app.post("/api/auth/logout", async () => {
  await auth.logout();
  return auth.state();
});

app.get("/healthz", async () => ({ status: "ok", service: "rubin-agent-ui" }));

app.get("/api/session", { websocket: true }, (socket: WebSocket) => {
  let session: AgentSession | null = null;
  let lastRole: RoleId | null = null;
  let idle: NodeJS.Timeout | undefined;
  const send = (message: ServerMessage) => {
    if (socket.readyState === socket.OPEN) socket.send(JSON.stringify(message));
  };
  const touch = () => {
    clearTimeout(idle);
    idle = setTimeout(() => {
      send({ type: "error", message: "Agent stopped after being idle; send a question to start again." });
      session?.close();
      session = null;
    }, config.idleTimeoutMs);
  };

  const startRole = async (role: RoleId) => {
    lastRole = role;
    session?.close();
    const definition = ROLES.find((r) => r.id === role)!;
    const agents = await listAgents(config.workspace);
    send({ type: "session", role, state: "starting", agentSource: agents.find((a) => a.role === role)?.source });
    const next = new AgentSession({
      role,
      agent: definition.agent,
      cwd: config.workspace,
      mcpServerName: config.mcpServerName,
      send,
      transport: () => spawnKiroTransport(config.kiroBin, definition.agent, config.workspace),
    });
    session = next;
    try {
      await next.start();
      if (session === next) send({ type: "session", role, state: "ready" });
    } catch (error) {
      send({ type: "error", message: `Could not start the ${definition.label} agent: ${(error as Error).message}` });
      next.close();
      if (session === next) session = null;
    }
  };

  socket.on("message", async (raw) => {
    touch();
    let message: ClientMessage;
    try {
      message = JSON.parse(raw.toString()) as ClientMessage;
    } catch {
      return send({ type: "error", message: "invalid message" });
    }
    try {
      if (message.type === "role" && isRoleId(message.role)) {
        await startRole(message.role);
      } else if (message.type === "prompt" && typeof message.text === "string" && message.text.trim()) {
        if (!session && lastRole) await startRole(lastRole);
        if (!session) return send({ type: "error", message: "Pick a role first." });
        await session.prompt(message.text.slice(0, 8000));
      } else if (message.type === "cancel") {
        await session?.cancel();
      } else {
        send({ type: "error", message: "unsupported message" });
      }
    } catch (error) {
      send({ type: "error", message: (error as Error).message });
    }
  });

  socket.on("close", () => {
    clearTimeout(idle);
    session?.close();
    session = null;
  });
});

app.setNotFoundHandler((request, reply) => {
  if (request.url.startsWith("/api/")) return reply.code(404).send({ error: "not found" });
  return reply.sendFile("index.html");
});

const shutdown = async () => {
  auth.cancelLogin();
  await app.close();
  process.exit(0);
};
process.on("SIGTERM", shutdown);
process.on("SIGINT", shutdown);

await app.listen({ host: config.host, port: config.port });
