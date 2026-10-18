import { spawn } from "node:child_process";
import { Readable, Writable } from "node:stream";
import {
  ClientSideConnection,
  ndJsonStream,
  PROTOCOL_VERSION,
  type Client,
  type SessionNotification,
  type Stream,
} from "@agentclientprotocol/sdk";
import type { RoleId, ServerMessage } from "../shared/events.js";
import { decidePermission } from "./permissions.js";
import { ToolCallTracker } from "./toolCalls.js";

export interface AgentTransport {
  stream: Stream;
  close(): void;
  /** Resolves with a human-readable reason when the agent process ends. */
  exited: Promise<string>;
}

export interface AgentSessionOptions {
  role: RoleId;
  agent: string;
  cwd: string;
  mcpServerName: string;
  send: (message: ServerMessage) => void;
  transport: () => AgentTransport;
}

export function spawnKiroTransport(kiroBin: string, agent: string, cwd: string): AgentTransport {
  const child = spawn(kiroBin, ["acp", "--agent", agent], { cwd, stdio: ["pipe", "pipe", "pipe"] });
  let stderr = "";
  child.stderr.on("data", (chunk: Buffer) => {
    stderr = (stderr + chunk.toString()).slice(-2000);
  });
  const exited = new Promise<string>((resolve) => {
    child.on("error", (error) => resolve(`failed to start ${kiroBin}: ${error.message}`));
    child.on("exit", (code, signal) => resolve(`agent exited (${signal ?? code})${stderr ? `: ${stderr.trim()}` : ""}`));
  });
  const stream = ndJsonStream(
    Writable.toWeb(child.stdin) as WritableStream<Uint8Array>,
    Readable.toWeb(child.stdout) as ReadableStream<Uint8Array>,
  );
  return { stream, exited, close: () => child.kill("SIGTERM") };
}

/** One Kiro ACP agent process and session for one browser connection and role. */
export class AgentSession {
  private transport: AgentTransport | null = null;
  private connection: ClientSideConnection | null = null;
  private sessionId: string | null = null;
  private busy = false;
  private closed = false;
  /** True while session/load replays history the browser already shows. */
  private replaying = false;
  private readonly tools = new ToolCallTracker();

  constructor(private readonly options: AgentSessionOptions) {}

  get role(): RoleId {
    return this.options.role;
  }

  get id(): string | null {
    return this.sessionId;
  }

  /** Starts the agent; with `resumeId` it reopens that saved session and returns true on success. */
  async start(resumeId?: string): Promise<boolean> {
    const transport = this.options.transport();
    this.transport = transport;
    void transport.exited.then((reason) => {
      if (!this.closed) {
        this.closed = true;
        this.options.send({ type: "error", message: reason });
        this.options.send({ type: "session", role: this.role, state: "closed" });
      }
    });
    const connection = new ClientSideConnection(() => this.client(), transport.stream);
    this.connection = connection;
    await connection.initialize({
      protocolVersion: PROTOCOL_VERSION,
      // No file system or terminal access is offered to the agent.
      clientCapabilities: { fs: { readTextFile: false, writeTextFile: false }, terminal: false },
      clientInfo: { name: "rubin-agent-ui", version: "0.1.0" },
    });
    if (resumeId) {
      this.replaying = true;
      try {
        await connection.loadSession({ sessionId: resumeId, cwd: this.options.cwd, mcpServers: [] });
        this.sessionId = resumeId;
        return true;
      } catch {
        // Unknown or expired session: fall through to a fresh one.
      } finally {
        this.replaying = false;
      }
    }
    const session = await connection.newSession({ cwd: this.options.cwd, mcpServers: [] });
    this.sessionId = session.sessionId;
    return false;
  }

  async prompt(text: string): Promise<void> {
    if (!this.connection || !this.sessionId || this.closed) throw new Error("agent session is not running");
    if (this.busy) throw new Error("the agent is still answering the previous question");
    this.busy = true;
    try {
      const result = await this.connection.prompt({
        sessionId: this.sessionId,
        prompt: [{ type: "text", text }],
      });
      this.options.send({ type: "turnEnd", stopReason: result.stopReason });
    } finally {
      this.busy = false;
    }
  }

  async cancel(): Promise<void> {
    if (this.connection && this.sessionId && this.busy) {
      await this.connection.cancel({ sessionId: this.sessionId });
    }
  }

  close(): void {
    this.closed = true;
    this.transport?.close();
  }

  private client(): Client {
    return {
      sessionUpdate: (params) => this.onUpdate(params),
      requestPermission: async (request) => decidePermission(request, this.options.mcpServerName),
      extNotification: async (method, params) => this.onExtension(method, params),
    };
  }

  private onUpdate({ update }: SessionNotification): void {
    if (this.replaying) return;
    switch (update.sessionUpdate) {
      case "agent_message_chunk":
        if (update.content.type === "text") this.options.send({ type: "text", delta: update.content.text });
        break;
      case "agent_thought_chunk":
        if (update.content.type === "text") this.options.send({ type: "thought", delta: update.content.text });
        break;
      case "tool_call":
      case "tool_call_update":
        this.options.send({ type: "toolCall", call: this.tools.apply(update) });
        break;
      default:
        break;
    }
  }

  private onExtension(method: string, params: Record<string, unknown>): void {
    // Kiro reports credits per turn in its metadata extension.
    if (this.replaying) return;
    if (!method.endsWith("kiro.dev/metadata") || !Array.isArray(params.meteringUsage)) return;
    const credits = params.meteringUsage.reduce(
      (sum: number, item: unknown) => sum + (typeof (item as { value?: unknown })?.value === "number" ? (item as { value: number }).value : 0),
      0,
    );
    const durationMs = typeof params.turnDurationMs === "number" ? params.turnDurationMs : null;
    this.options.send({ type: "usage", credits, durationMs });
  }
}
