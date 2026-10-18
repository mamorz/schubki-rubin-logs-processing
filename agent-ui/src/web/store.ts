import { create } from "zustand";
import type { AgentSource, RoleId, ServerMessage, ToolCallView } from "../shared/events";

export interface Turn {
  id: string;
  role: RoleId;
  question: string;
  text: string;
  thought: string;
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
  notice: string | null;
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

export const useChat = create<ChatState>((set) => ({
  role: "engineer",
  connection: "connecting",
  agentSource: null,
  turns: [],
  toolCalls: {},
  notice: null,
  setRole: (role) => set({ role }),
  setConnection: (connection) => set({ connection }),
  dismissNotice: () => set({ notice: null }),
  clear: () => set({ turns: [], toolCalls: {} }),
  ask: (question) =>
    set((state) => ({
      turns: [
        ...state.turns,
        {
          id: crypto.randomUUID(),
          role: state.role,
          question,
          text: "",
          thought: "",
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
          };
        case "text":
          return answering ? { turns: updateLast(state.turns, (t) => ({ text: t.text + message.delta })) } : {};
        case "thought":
          return answering ? { turns: updateLast(state.turns, (t) => ({ thought: t.thought + message.delta })) } : {};
        case "toolCall": {
          const existing = state.toolCalls[message.call.id];
          const turnId = existing?.turnId ?? active?.id;
          if (!turnId) return {};
          return {
            toolCalls: { ...state.toolCalls, [message.call.id]: { ...message.call, turnId } },
            turns: existing
              ? state.turns
              : updateLast(state.turns, (t) => ({ toolCallIds: [...t.toolCallIds, message.call.id] })),
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
}));
