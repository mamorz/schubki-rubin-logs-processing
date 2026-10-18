// Wire protocol between the browser and the agent-ui server.

export const ROLES = [
  {
    id: "engineer",
    agent: "engineer",
    label: "Engineer",
    summary: "What failed, where, and what happened just before and after.",
  },
  {
    id: "team-lead",
    agent: "team-lead",
    label: "Team lead",
    summary: "Which problems recur, how often, and which components or teams are involved.",
  },
  {
    id: "manager",
    agent: "manager",
    label: "Manager",
    summary: "Which observed problems deserve attention first, and what should happen next.",
  },
] as const;

export type RoleId = (typeof ROLES)[number]["id"];

export function isRoleId(value: unknown): value is RoleId {
  return ROLES.some((role) => role.id === value);
}

export type ToolCallStatus = "pending" | "in_progress" | "completed" | "failed";

export interface ToolCallView {
  id: string;
  title: string;
  server: string | null;
  tool: string | null;
  status: ToolCallStatus;
  input: Record<string, unknown> | null;
  /** Parsed JSON result when the tool returned JSON text. */
  output: unknown;
  /** Raw text result when it was not JSON. */
  outputText: string | null;
}

export type ClientMessage =
  | { type: "prompt"; text: string }
  | { type: "cancel" }
  /** Starts the role's agent; `resume` reopens a saved Kiro session after a page reload. */
  | { type: "role"; role: RoleId; resume?: string };

export type ServerMessage =
  | {
      type: "session";
      role: RoleId;
      state: "starting" | "ready" | "closed";
      agentSource?: AgentSource;
      sessionId?: string;
      resumed?: boolean;
    }
  | { type: "text"; delta: string }
  | { type: "thought"; delta: string }
  | { type: "toolCall"; call: ToolCallView }
  | { type: "usage"; credits: number; durationMs: number | null }
  | { type: "turnEnd"; stopReason: string }
  | { type: "error"; message: string };

export type AgentSource = "workspace" | "fallback";

export interface AgentInfo {
  role: RoleId;
  agent: string;
  source: AgentSource;
}

export interface AuthState {
  loggedIn: boolean;
  method: string | null;
  account: string | null;
  pending: { url: string; code: string } | null;
  error: string | null;
  apiKeyConfigured: boolean;
}

export interface StatusResponse {
  auth: AuthState;
  mcp: { ok: boolean; url: string; detail: string | null };
  kiro: { version: string | null };
  agents: AgentInfo[];
}
