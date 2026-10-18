import type { ToolCall, ToolCallUpdate } from "@agentclientprotocol/sdk";
import type { ToolCallStatus, ToolCallView } from "../shared/events.js";

const TITLE_PATTERN = /@([^/\s]+)\/(\S+)/;

/** Accumulates ACP tool_call / tool_call_update notifications into UI views. */
export class ToolCallTracker {
  private readonly calls = new Map<string, ToolCallView>();

  apply(update: ToolCall | ToolCallUpdate): ToolCallView {
    const previous = this.calls.get(update.toolCallId);
    const title = update.title ?? previous?.title ?? "tool call";
    const meta = kiroMeta(update);
    const fromTitle = TITLE_PATTERN.exec(title);
    const output = "rawOutput" in update && update.rawOutput !== undefined
      ? extractOutput(update.rawOutput)
      : null;
    const view: ToolCallView = {
      id: update.toolCallId,
      title,
      server: meta?.mcpServerName ?? previous?.server ?? fromTitle?.[1] ?? null,
      tool: meta?.toolName ?? previous?.tool ?? fromTitle?.[2] ?? null,
      status: isToolError(output?.text) ? "failed" : (update.status as ToolCallStatus | null | undefined) ?? previous?.status ?? "pending",
      input: cleanInput(update.rawInput) ?? previous?.input ?? null,
      output: output ? output.json : previous?.output ?? null,
      outputText: output ? output.text : previous?.outputText ?? null,
    };
    this.calls.set(view.id, view);
    return view;
  }
}

/** Kiro reports MCP tool errors as completed calls whose text is the server's error message. */
function isToolError(text: string | null | undefined): boolean {
  return typeof text === "string" && /^Error executing tool\b/.test(text.trim());
}

function kiroMeta(update: { _meta?: Record<string, unknown> | null }): { toolName?: string; mcpServerName?: string } | null {
  const kiro = update._meta?.kiro;
  return kiro && typeof kiro === "object" ? (kiro as { toolName?: string; mcpServerName?: string }) : null;
}

function cleanInput(raw: unknown): Record<string, unknown> | null {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  // Kiro adds a model-written purpose string; keep it out of the tool arguments.
  const { __tool_use_purpose: _purpose, ...rest } = raw as Record<string, unknown>;
  return rest;
}

/** Kiro wraps MCP results as {items:[{Json:{content:[{type:"text",text}]}}]}. */
export function extractOutput(raw: unknown): { json: unknown; text: string | null } {
  const texts = collectTexts(raw);
  if (texts.length === 0) return { json: raw, text: null };
  const text = texts.join("\n");
  try {
    return { json: JSON.parse(text), text: null };
  } catch {
    return { json: null, text };
  }
}

function collectTexts(raw: unknown): string[] {
  if (typeof raw === "string") return [raw];
  if (!raw || typeof raw !== "object") return [];
  const value = raw as Record<string, unknown>;
  if (Array.isArray(value.items)) {
    return value.items.flatMap((item) => collectTexts((item as Record<string, unknown>)?.Json ?? (item as Record<string, unknown>)?.Text ?? item));
  }
  if (Array.isArray(value.content)) {
    return value.content.flatMap((part) => {
      const p = part as Record<string, unknown>;
      return p?.type === "text" && typeof p.text === "string" ? [p.text] : [];
    });
  }
  return [];
}
