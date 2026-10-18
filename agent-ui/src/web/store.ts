import { create } from "zustand";
import { createJSONStorage, persist, type StateStorage } from "zustand/middleware";
import type { AgentSource, RoleId, ServerMessage, ToolCallView } from "../shared/events";

/** One step of an answer, in the order the agent produced it. */
export type TurnItem =
  | { kind: "thought"; text: string }
  | { kind: "text"; text: string }
  | { kind: "tool"; id: string };

export interface Turn {
  id: string;
  role: RoleId;
  question: string;
  items: TurnItem[];
  /** All answer text, for export. */
  text: string;
  toolCallIds: string[];
  done: boolean;
  stopReason: string | null;
  credits: number | null;
  durationMs: number | null;
  error: string | null;
  startedAt: number;
}

export type ConnectionState = "connecting" | "starting" | "ready" | "closed";

interface ChatState {
  role: RoleId;
  connection: ConnectionState;
  agentSource: AgentSource | null;
  turns: Turn[];
  toolCalls: Record<string, ToolCallView & { turnId: string }>;
  /** Kiro session per role, reopened after a page reload. */
  agentSessions: Partial<Record<RoleId, string>>;
  notice: string | null;
  /** Tool call the ledger asked the chat to reveal; the nonce re-triggers repeated clicks. */
  focus: { id: string; nonce: number } | null;
  focusToolCall: (id: string | null) => void;
  setRole: (role: RoleId) => void;
  setConnection: (state: ConnectionState) => void;
  ask: (question: string) => void;
  receive: (message: ServerMessage) => void;
  dismissNotice: () => void;
  clear: () => void;
}

const lastTurn = (turns: Turn[]) => turns[turns.length - 1];

function updateLast(turns: Turn[], patch: (turn: Turn) => Partial<Turn>): Turn[] {
  const last = lastTurn(turns);
  if (!last) return turns;
  return [...turns.slice(0, -1), { ...last, ...patch(last) }];
}

/** Appends streamed text to the previous item of the same kind, or starts a new step. */
function appendChunk(items: TurnItem[], kind: "thought" | "text", delta: string): TurnItem[] {
  const last = items[items.length - 1];
  if (last && last.kind === kind) return [...items.slice(0, -1), { kind, text: last.text + delta }];
  return [...items, { kind, text: delta }];
}

const MAX_SAVED_TURNS = 40;

/** localStorage that never throws: on quota errors it keeps only the newest turns. */
const safeLocalStorage: StateStorage = {
  getItem: (name) => {
    try {
      return localStorage.getItem(name);
    } catch {
      return null;
    }
  },
  setItem: (name, value) => {
    try {
      localStorage.setItem(name, value);
    } catch {
      try {
        const parsed = JSON.parse(value) as { state: Pick<ChatState, "turns" | "toolCalls"> };
        const turns = parsed.state.turns.slice(-5);
        const keep = new Set(turns.flatMap((t) => t.toolCallIds));
        const toolCalls = Object.fromEntries(Object.entries(parsed.state.toolCalls).filter(([id]) => keep.has(id)));
        localStorage.setItem(name, JSON.stringify({ ...parsed, state: { ...parsed.state, turns, toolCalls } }));
      } catch {
        // Storage unavailable (private window, blocked site data): history simply isn't kept.
      }
    }
  },
  removeItem: (name) => {
    try {
      localStorage.removeItem(name);
    } catch {
      // ignore
    }
  },
};

export const useChat = create<ChatState>()(
  persist(
    (set) => ({
      role: "engineer",
      connection: "connecting",
      agentSource: null,
      turns: [],
      toolCalls: {},
      agentSessions: {},
      notice: null,
      focus: null,
      focusToolCall: (id) => set({ focus: id ? { id, nonce: Date.now() } : null }),
      setRole: (role) => set({ role }),
      setConnection: (connection) => set({ connection }),
      dismissNotice: () => set({ notice: null }),
      clear: () => set({ turns: [], toolCalls: {}, agentSessions: {} }),
      ask: (question) =>
        set((state) => ({
          turns: [
            ...state.turns,
            {
              id: crypto.randomUUID(),
              role: state.role,
              question,
              items: [],
              text: "",
              toolCallIds: [],
              done: false,
              stopReason: null,
              credits: null,
              durationMs: null,
              error: null,
              startedAt: Date.now(),
            },
          ],
        })),
      receive: (message) =>
        set((state) => {
          const active = lastTurn(state.turns);
          const answering = active && !active.done;
          switch (message.type) {
            case "session":
              return {
                connection: message.state === "ready" ? "ready" : message.state === "starting" ? "starting" : "closed",
                agentSource: message.agentSource ?? state.agentSource,
                agentSessions: message.sessionId
                  ? { ...state.agentSessions, [message.role]: message.sessionId }
                  : state.agentSessions,
              };
            case "text":
              return answering
                ? { turns: updateLast(state.turns, (t) => ({ text: t.text + message.delta, items: appendChunk(t.items, "text", message.delta) })) }
                : {};
            case "thought":
              return answering
                ? { turns: updateLast(state.turns, (t) => ({ items: appendChunk(t.items, "thought", message.delta) })) }
                : {};
            case "toolCall": {
              const existing = state.toolCalls[message.call.id];
              const turnId = existing?.turnId ?? active?.id;
              if (!turnId) return {};
              return {
                toolCalls: { ...state.toolCalls, [message.call.id]: { ...message.call, turnId } },
                turns: existing
                  ? state.turns
                  : updateLast(state.turns, (t) => ({
                      toolCallIds: [...t.toolCallIds, message.call.id],
                      items: [...t.items, { kind: "tool", id: message.call.id }],
                    })),
              };
            }
            case "usage":
              // The final metering report can arrive just after the turn ends; attach it to the latest turn.
              return {
                turns: updateLast(state.turns, (t) => ({
                  credits: (t.credits ?? 0) + message.credits,
                  durationMs: message.durationMs ?? t.durationMs,
                })),
              };
            case "turnEnd":
              return { turns: updateLast(state.turns, () => ({ done: true, stopReason: message.stopReason })) };
            case "error":
              return answering
                ? { turns: updateLast(state.turns, () => ({ done: true, error: message.message })) }
                : { notice: message.message };
            default:
              return {};
          }
        }),
    }),
    {
      name: "rubin-agent-ui:chat:v1",
      storage: createJSONStorage(() => safeLocalStorage),
      partialize: (state) => {
        const turns = state.turns.slice(-MAX_SAVED_TURNS);
        const keep = new Set(turns.flatMap((t) => t.toolCallIds));
        return {
          role: state.role,
          turns,
          toolCalls: Object.fromEntries(Object.entries(state.toolCalls).filter(([id]) => keep.has(id))),
          agentSessions: state.agentSessions,
        };
      },
      // A reload cuts off any answer that was still streaming.
      merge: (persisted, current) => {
        const saved = (persisted ?? {}) as Partial<ChatState>;
        return {
          ...current,
          ...saved,
          turns: (saved.turns ?? []).map((t) => (t.done ? t : { ...t, done: true, error: t.error ?? "Interrupted by a page reload." })),
        };
      },
    },
  ),
);
