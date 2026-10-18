import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import type { Stream } from "@agentclientprotocol/sdk";
import { AgentSession, type AgentTransport } from "../src/server/acpBridge.js";
import type { ServerMessage, ToolCallView } from "../src/shared/events.js";
import { describeResult } from "../src/shared/toolResult.js";

type Wire = { id?: number | string; method?: string; result?: unknown; params?: unknown };

const fixture: Wire[] = readFileSync(new URL("./fixtures/kiro-acp-session.ndjson", import.meta.url), "utf8")
  .split("\n")
  .filter(Boolean)
  .map((line) => JSON.parse(line) as Wire);

/** Replays recorded Kiro traffic: the k-th client request gets the k-th recorded response,
 * preceded by the notifications Kiro sent before that response. */
function replayTransport(): AgentTransport {
  const batches: { notifications: Wire[]; result: unknown }[] = [];
  let notifications: Wire[] = [];
  for (const message of fixture) {
    if (message.method) notifications.push(message);
    else {
      batches.push({ notifications, result: message.result });
      notifications = [];
    }
  }
  const requests: Wire[] = [];
  let push: (message: Wire) => void = () => undefined;
  const readable = new ReadableStream({
    start(controller) {
      push = (message) => controller.enqueue(message);
    },
  });
  const writable = new WritableStream({
    write(message: Wire) {
      if (message.id === undefined || !message.method) return; // responses/notifications from the client
      requests.push(message);
      const batch = batches[requests.length - 1];
      if (!batch) return;
      for (const n of batch.notifications) push(n);
      push({ jsonrpc: "2.0", id: message.id, result: batch.result } as Wire);
    },
  });
  return {
    stream: { readable, writable } as unknown as Stream,
    exited: new Promise(() => undefined),
    close: () => undefined,
  };
}

async function runRecordedSession(): Promise<ServerMessage[]> {
  const sent: ServerMessage[] = [];
  const session = new AgentSession({
    role: "engineer",
    agent: "engineer",
    cwd: "/workspace",
    mcpServerName: "rubin-log-insights",
    send: (message) => sent.push(message),
    transport: replayTransport,
  });
  await session.start();
  await session.prompt("recorded question");
  // Kiro's final metering notification may be handled just after the prompt response.
  await new Promise((resolve) => setTimeout(resolve, 20));
  return sent;
}

describe("AgentSession with recorded Kiro ACP traffic", () => {
  it("streams answer text, ends the turn and reports credits", async () => {
    const sent = await runRecordedSession();
    const text = sent.flatMap((m) => (m.type === "text" ? [m.delta] : [])).join("");
    expect(text.length).toBeGreaterThan(20);
    expect(sent).toContainEqual({ type: "turnEnd", stopReason: "end_turn" });
    const usage = sent.filter((m) => m.type === "usage");
    expect(usage.length).toBeGreaterThan(0);
  });

  it("maps all three rubin tool calls with parsed JSON results", async () => {
    const sent = await runRecordedSession();
    const finals = new Map<string, ToolCallView>();
    for (const m of sent) if (m.type === "toolCall") finals.set(m.call.id, m.call);
    const calls = [...finals.values()];
    expect(calls.map((c) => c.tool)).toEqual(["search_logs", "get_nearby_events", "summarize_logs"]);
    for (const call of calls) {
      expect(call.server).toBe("rubin-log-insights");
      expect(call.status).toBe("completed");
      expect(call.input).not.toHaveProperty("__tool_use_purpose");
      const model = describeResult(call.output);
      expect(model).not.toBeNull();
      expect(model!.sql.length).toBeGreaterThan(0);
    }
  });
});

describe("AgentSession resume", () => {
  function resumeTransport(loadFails: boolean): AgentTransport {
    let push: (message: Wire) => void = () => undefined;
    const readable = new ReadableStream({ start: (c) => { push = (m) => c.enqueue(m); } });
    const writable = new WritableStream({
      write(message: Wire) {
        if (message.id === undefined || !message.method) return;
        if (message.method === "initialize") {
          push({ jsonrpc: "2.0", id: message.id, result: { protocolVersion: 1, agentCapabilities: { loadSession: true } } } as Wire);
        } else if (message.method === "session/load") {
          // Kiro replays the earlier conversation before answering session/load.
          push({ jsonrpc: "2.0", method: "session/update", params: { sessionId: "old", update: { sessionUpdate: "agent_message_chunk", content: { type: "text", text: "replayed" } } } } as Wire);
          push(loadFails
            ? ({ jsonrpc: "2.0", id: message.id, error: { code: -32002, message: "not found" } } as Wire)
            : ({ jsonrpc: "2.0", id: message.id, result: {} } as Wire));
        } else if (message.method === "session/new") {
          push({ jsonrpc: "2.0", id: message.id, result: { sessionId: "fresh" } } as Wire);
        }
      },
    });
    return { stream: { readable, writable } as unknown as Stream, exited: new Promise(() => undefined), close: () => undefined };
  }

  async function start(loadFails: boolean) {
    const sent: ServerMessage[] = [];
    const session = new AgentSession({
      role: "manager", agent: "manager", cwd: "/workspace", mcpServerName: "rubin-log-insights",
      send: (m) => sent.push(m), transport: () => resumeTransport(loadFails),
    });
    const resumed = await session.start("old");
    await new Promise((resolve) => setTimeout(resolve, 10));
    return { resumed, id: session.id, sent };
  }

  it("reopens the saved session without re-sending replayed history", async () => {
    const result = await start(false);
    expect(result).toMatchObject({ resumed: true, id: "old" });
    expect(result.sent.filter((m) => m.type === "text")).toEqual([]);
  });

  it("falls back to a fresh session when the saved one is gone", async () => {
    expect(await start(true)).toMatchObject({ resumed: false, id: "fresh" });
  });
});
