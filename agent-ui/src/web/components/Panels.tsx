import * as Collapsible from "@radix-ui/react-collapsible";
import * as Tabs from "@radix-ui/react-tabs";
import { BarChart3, ChevronRight, Download, ExternalLink, FileSearch, Folder, KeyRound, Loader2, LogOut, ShieldCheck, Trash2, Wrench } from "lucide-react";
import { useState } from "react";
import { ROLES, type AgentInfo, type RoleId, type StatusResponse, type ToolCallView } from "../../shared/events";
import { api } from "../api";
import { classifyRubinResult, resultHeadline } from "../../shared/rubin";
import { download, sessionToMarkdown } from "../exportMarkdown";
import { useChat, type ConnectionState, type Turn } from "../store";
import { TruncationBadge } from "./ToolCallCard";

const ROLE_ICONS: Record<RoleId, typeof Wrench> = { engineer: Wrench, "team-lead": BarChart3, manager: ShieldCheck };

export function Header({ status, connection, onLogout }: {
  status: StatusResponse | null;
  connection: ConnectionState;
  onLogout: () => void;
}) {
  return (
    <header className="flex items-center gap-3 border-b border-slate-200 bg-white/80 px-4 py-2.5 backdrop-blur dark:border-slate-800 dark:bg-slate-900/80">
      <div className="flex items-center gap-2">
        <span className="grid size-8 place-items-center rounded-lg bg-ruby-700 font-bold text-white">R</span>
        <div className="leading-tight">
          <div className="font-semibold">RUBIN Log Agent</div>
          <div className="text-[11px] text-slate-500">BGL · OpenStack · SereneDB</div>
        </div>
      </div>
      <div className="ml-auto flex items-center gap-2">
        <Health label="MCP" ok={status?.mcp.ok ?? null} title={status?.mcp.detail ?? status?.mcp.url} />
        <Health label="Agent" ok={status?.auth.loggedIn ? connection === "ready" : null} busy={connection === "starting" || connection === "connecting"} title={`kiro-cli ${status?.kiro.version ?? "?"}`} />
        {status?.auth.loggedIn && (
          <>
            <span className="hidden text-xs text-slate-500 md:inline">{status.auth.account ?? status.auth.method}</span>
            <button type="button" onClick={onLogout} className="btn-ghost px-2 py-1" title="Sign out of Kiro">
              <LogOut className="size-4" />
            </button>
          </>
        )}
      </div>
    </header>
  );
}

function Health({ label, ok, busy, title }: { label: string; ok: boolean | null; busy?: boolean; title?: string | null }) {
  const color = busy ? "bg-amber-400" : ok === null ? "bg-slate-300 dark:bg-slate-600" : ok ? "bg-emerald-500" : "bg-red-500";
  return (
    <span className="chip bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300" title={title ?? undefined}>
      <span className={`size-2 rounded-full ${color}`} /> {label}
    </span>
  );
}

export function RoleSwitcher({ role, agents, onChange }: { role: RoleId; agents: AgentInfo[]; onChange: (role: RoleId) => void }) {
  return (
    <Tabs.Root value={role} onValueChange={(value) => onChange(value as RoleId)}>
      <Tabs.List className="grid grid-cols-3 gap-1 rounded-xl bg-slate-100 p-1 dark:bg-slate-800/70" aria-label="Role">
        {ROLES.map((r) => {
          const Icon = ROLE_ICONS[r.id];
          const source = agents.find((a) => a.role === r.id)?.source;
          return (
            <Tabs.Trigger
              key={r.id}
              value={r.id}
              title={`${r.summary}${source ? ` (agent: ${source})` : ""}`}
              className="flex items-center justify-center gap-2 rounded-lg px-2 py-1.5 text-sm font-medium text-slate-600 transition data-[state=active]:bg-white data-[state=active]:text-ruby-700 data-[state=active]:shadow-sm dark:text-slate-300 dark:data-[state=active]:bg-slate-900 dark:data-[state=active]:text-ruby-400"
            >
              <Icon className="size-4" /> <span>{r.label}</span>
            </Tabs.Trigger>
          );
        })}
      </Tabs.List>
    </Tabs.Root>
  );
}

export function EvidenceLedger() {
  const turns = useChat((s) => s.turns);
  const toolCalls = useChat((s) => s.toolCalls);
  const clear = useChat((s) => s.clear);
  const completedOf = (turn: Turn) =>
    turn.toolCallIds.map((id) => toolCalls[id]).filter((c): c is NonNullable<typeof c> => c?.status === "completed");
  const total = turns.reduce((sum, turn) => sum + completedOf(turn).length, 0);
  const current = turns[turns.length - 1];
  const previous = turns.slice(0, -1).filter((turn) => completedOf(turn).length > 0).reverse();
  return (
    <aside className="flex min-h-0 flex-col gap-3">
      <div className="flex items-center gap-2">
        <FileSearch className="size-4 text-ruby-600" />
        <h2 className="text-sm font-semibold">Evidence ledger</h2>
        <span className="text-xs text-slate-500">{total} queries</span>
        <div className="ml-auto flex gap-1">
          <button
            type="button"
            className="btn-ghost px-2 py-1 text-xs"
            disabled={turns.length === 0}
            onClick={() => download(`rubin-investigation-${new Date().toISOString().slice(0, 19)}.md`, sessionToMarkdown(turns, toolCalls))}
            title="Export the session as a worked investigation (Markdown)"
          >
            <Download className="size-3.5" /> Export
          </button>
          <button type="button" className="btn-ghost px-2 py-1 text-xs" disabled={turns.length === 0} onClick={clear} title="Clear conversation view">
            <Trash2 className="size-3.5" />
          </button>
        </div>
      </div>
      {total === 0 && (
        <p className="panel p-4 text-xs text-slate-500">
          Every database query the agent runs appears here with its filters, counts and truncation status. Export it to keep the evidence behind each answer.
        </p>
      )}
      <div className="min-h-0 space-y-3 overflow-y-auto pr-1">
        {current && completedOf(current).length > 0 && (
          <section>
            <h3 className="mb-1.5 truncate text-[11px] font-semibold uppercase tracking-wide text-slate-500" title={current.question}>
              Current request · {roleLabel(current.role)}
            </h3>
            <LedgerEntries calls={completedOf(current)} />
          </section>
        )}
        {previous.length > 0 && (
          <section className="space-y-1.5">
            <h3 className="text-[11px] font-semibold uppercase tracking-wide text-slate-500">Previous requests</h3>
            {previous.map((turn) => <PreviousReport key={turn.id} turn={turn} calls={completedOf(turn)} />)}
          </section>
        )}
      </div>
    </aside>
  );
}

function roleLabel(role: RoleId): string {
  return ROLES.find((r) => r.id === role)?.label ?? role;
}

function PreviousReport({ turn, calls }: { turn: Turn; calls: ToolCallView[] }) {
  const [open, setOpen] = useState(false);
  return (
    <Collapsible.Root open={open} onOpenChange={setOpen} className="panel overflow-hidden">
      <Collapsible.Trigger className="flex w-full items-start gap-2 px-3 py-2 text-left hover:bg-slate-50 dark:hover:bg-slate-800/60" title={turn.question}>
        <ChevronRight className={`mt-0.5 size-3.5 shrink-0 text-slate-400 transition ${open ? "rotate-90" : ""}`} />
        <Folder className="mt-0.5 size-3.5 shrink-0 text-ruby-600 dark:text-ruby-400" />
        <span className="min-w-0 flex-1 text-xs">
          <span className="line-clamp-2">Report for request “{turn.question}” for {roleLabel(turn.role)}</span>
          <span className="text-[11px] text-slate-500">{calls.length} quer{calls.length === 1 ? "y" : "ies"}</span>
        </span>
      </Collapsible.Trigger>
      <Collapsible.Content className="border-t border-slate-100 p-2 dark:border-slate-800">
        <LedgerEntries calls={calls} compact />
      </Collapsible.Content>
    </Collapsible.Root>
  );
}

function LedgerEntries({ calls, compact = false }: { calls: ToolCallView[]; compact?: boolean }) {
  return (
    <ol className="space-y-2">
      {calls.map((call, index) => {
        const result = classifyRubinResult(call.tool, call.output);
        return (
          <li key={call.id} className={compact ? "rounded-lg bg-slate-50 p-2 dark:bg-slate-800/50" : "panel p-3"}>
            <div className="flex items-center gap-2">
              <span className="grid size-5 place-items-center rounded-full bg-slate-100 text-[11px] font-semibold dark:bg-slate-800">{index + 1}</span>
              <span className="font-mono text-xs font-medium">{call.tool}</span>
              <span className="ml-auto"><TruncationBadge result={result} /></span>
            </div>
            <p className="mt-1 text-xs text-slate-600 dark:text-slate-400">{resultHeadline(result)}</p>
            {call.input && (
              <p className="mt-1 truncate font-mono text-[11px] text-slate-500" title={JSON.stringify(call.input)}>
                {JSON.stringify(call.input)}
              </p>
            )}
          </li>
        );
      })}
    </ol>
  );
}

export function SignIn({ status, onRefresh }: { status: StatusResponse | null; onRefresh: () => void }) {
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const pending = status?.auth.pending ?? null;

  const start = async () => {
    setStarting(true);
    setError(null);
    try {
      await api.login();
      onRefresh();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setStarting(false);
    }
  };

  return (
    <div className="grid min-h-full place-items-center p-4">
      <div className="panel w-full max-w-md p-6">
        <div className="flex items-center gap-2">
          <KeyRound className="size-5 text-ruby-600" />
          <h1 className="text-lg font-semibold">Sign in to Kiro</h1>
        </div>
        <p className="mt-2 text-sm text-slate-600 dark:text-slate-400">
          The agent runs inside this container with Kiro CLI. Sign in once with your AWS Builder ID; the login is kept in a local Docker volume.
        </p>
        {!pending ? (
          <button type="button" onClick={start} disabled={starting || !status} className="btn-primary mt-5 w-full">
            {starting && <Loader2 className="size-4 animate-spin" />} Sign in with Kiro
          </button>
        ) : (
          <div className="mt-5 space-y-4">
            <div className="rounded-xl bg-slate-50 p-4 text-center dark:bg-slate-800/60">
              <div className="text-xs uppercase tracking-wide text-slate-500">Your code</div>
              <div className="mt-1 font-mono text-3xl font-bold tracking-widest select-all">{pending.code}</div>
            </div>
            <a href={pending.url} target="_blank" rel="noreferrer" className="btn-primary w-full">
              Open sign-in page <ExternalLink className="size-4" />
            </a>
            <p className="flex items-center justify-center gap-2 text-xs text-slate-500">
              <Loader2 className="size-3 animate-spin" /> Waiting for you to confirm the code in the browser…
            </p>
            <button type="button" onClick={() => api.cancelLogin().finally(onRefresh)} className="btn-ghost w-full text-xs">
              Cancel
            </button>
          </div>
        )}
        {(error ?? status?.auth.error) && <p className="mt-3 text-sm text-red-600">{error ?? status?.auth.error}</p>}
        {status && !status.mcp.ok && (
          <p className="mt-3 text-xs text-amber-700 dark:text-amber-400">
            The rubin MCP server is not reachable yet ({status.mcp.detail ?? status.mcp.url}).
          </p>
        )}
      </div>
    </div>
  );
}
