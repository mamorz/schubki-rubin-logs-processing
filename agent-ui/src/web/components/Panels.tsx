import * as AlertDialog from "@radix-ui/react-alert-dialog";
import * as Collapsible from "@radix-ui/react-collapsible";
import { ArrowUpRight, ChevronRight, Download, ExternalLink, FileSearch, FileText, Folder, KeyRound, Loader2, LogOut, Trash2 } from "lucide-react";
import { useState } from "react";
import { ROLES, type RoleId, type StatusResponse, type ToolCallView } from "../../shared/events";
import { api } from "../api";
import { GemScroll, RubyGem } from "./GemScroll";
import { describeResult } from "../../shared/toolResult";
import { download, reportFilename, turnToMarkdown } from "../exportMarkdown";
import { useChat, type ConnectionState, type Turn } from "../store";
import { TruncationBadge } from "./ToolCallCard";

export function Header({ status, connection, onLogout }: {
  status: StatusResponse | null;
  connection: ConnectionState;
  onLogout: () => void;
}) {
  return (
    <header className="flex items-center gap-3 border-b border-slate-200 bg-white/80 px-4 py-2.5 backdrop-blur dark:border-slate-800 dark:bg-slate-900/80">
      <div className="flex items-center gap-2">
        <span className="grid size-8 place-items-center rounded-lg bg-slate-950 ring-1 ring-white/10">
          <RubyGem size={20} />
        </span>
        <span className="font-semibold">RUBIN Agent</span>
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

export function EvidenceLedger({ onClear }: { onClear: () => void }) {
  const turns = useChat((s) => s.turns);
  const toolCalls = useChat((s) => s.toolCalls);
  const completedOf = (turn: Turn) =>
    turn.toolCallIds.map((id) => toolCalls[id]).filter((c): c is NonNullable<typeof c> => c?.status === "completed");
  const total = turns.reduce((sum, turn) => sum + completedOf(turn).length, 0);
  const current = turns[turns.length - 1];
  const previous = turns.slice(0, -1).filter((turn) => completedOf(turn).length > 0).reverse();
  return (
    <aside className="flex min-h-0 flex-1 flex-col gap-3">
      <div className="flex items-center gap-2">
        <FileSearch className="size-4 text-ruby-600" />
        <h2 className="text-sm font-semibold">Evidence ledger</h2>
        <span className="text-xs text-slate-500">{total} queries</span>
        <div className="ml-auto flex gap-1">
          <ConfirmClear disabled={turns.length === 0} requests={turns.length} onConfirm={onClear} />
        </div>
      </div>
      {total === 0 && (
        <p className="panel p-4 text-xs text-slate-500">
          Every database query the agent runs appears here with its filters, counts and truncation status. Click one to open it in the agent workflow, or export a request's full report.
        </p>
      )}
      <GemScroll className="min-h-0 flex-1" contentClassName="space-y-3" thumb={<FileThumb />} label="Scroll evidence ledger">
        {current && completedOf(current).length > 0 && (
          <CurrentReport key={current.id} turn={current} calls={completedOf(current)} />
        )}
        {previous.length > 0 && (
          <section className="space-y-1.5">
            <h3 className="text-[11px] font-semibold uppercase tracking-wide text-slate-500">Previous requests</h3>
            {previous.map((turn) => <PreviousReport key={turn.id} turn={turn} calls={completedOf(turn)} />)}
          </section>
        )}
      </GemScroll>
    </aside>
  );
}

/** Small ruby-coloured file used as the ledger's scroll handle. */
function FileThumb() {
  return <FileText className="size-[18px] text-ruby-500 drop-shadow-[0_0_5px_rgba(244,63,94,0.75)]" strokeWidth={2.25} />;
}

/** Trash button that asks before wiping the conversation, evidence and saved agent sessions. */
function ConfirmClear({ disabled, requests, onConfirm }: { disabled: boolean; requests: number; onConfirm: () => void }) {
  return (
    <AlertDialog.Root>
      <AlertDialog.Trigger asChild>
        <button type="button" className="btn-ghost px-2 py-1 text-xs" disabled={disabled} title="Delete chat history">
          <Trash2 className="size-3.5" />
        </button>
      </AlertDialog.Trigger>
      <AlertDialog.Portal>
        <AlertDialog.Overlay className="fixed inset-0 z-40 bg-slate-950/60 backdrop-blur-sm" />
        <AlertDialog.Content className="panel fixed top-1/2 left-1/2 z-50 w-[calc(100vw-2rem)] max-w-md -translate-x-1/2 -translate-y-1/2 p-5 shadow-xl">
          <div className="flex items-start gap-3">
            <span className="grid size-9 shrink-0 place-items-center rounded-full bg-red-100 text-red-700 dark:bg-red-950/60 dark:text-red-300">
              <Trash2 className="size-4" />
            </span>
            <div>
              <AlertDialog.Title className="font-semibold">Delete chat history?</AlertDialog.Title>
              <AlertDialog.Description className="mt-1 text-sm text-slate-600 dark:text-slate-400">
                This removes {requests} request{requests === 1 ? "" : "s"} with their answers and evidence from this browser, and the agents start fresh sessions without the earlier context. Export the ledger first if you want to keep the evidence. This can't be undone.
              </AlertDialog.Description>
            </div>
          </div>
          <div className="mt-5 flex justify-end gap-2">
            <AlertDialog.Cancel className="btn-ghost">Cancel</AlertDialog.Cancel>
            <AlertDialog.Action onClick={onConfirm} className="btn bg-red-600 text-white hover:bg-red-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-red-500">
              Delete history
            </AlertDialog.Action>
          </div>
        </AlertDialog.Content>
      </AlertDialog.Portal>
    </AlertDialog.Root>
  );
}

function roleLabel(role: RoleId): string {
  return ROLES.find((r) => r.id === role)?.label ?? role;
}

/** The latest request's evidence: open by default, foldable to a one-line summary. */
function CurrentReport({ turn, calls }: { turn: Turn; calls: ToolCallView[] }) {
  const [open, setOpen] = useState(true);
  const truncated = calls.filter((c) => describeResult(c.output)?.truncated).length;
  return (
    <Collapsible.Root open={open} onOpenChange={setOpen} asChild>
      <section>
        <div className="mb-1.5 flex items-center gap-2">
          <Collapsible.Trigger
            className="flex min-w-0 flex-1 items-center gap-1.5 text-left text-[11px] font-semibold uppercase tracking-wide text-slate-500 hover:text-slate-700 dark:hover:text-slate-300"
            title={turn.question}
          >
            <ChevronRight className={`size-3.5 shrink-0 transition ${open ? "rotate-90" : ""}`} />
            <span className="truncate">Current request · {roleLabel(turn.role)}</span>
            {!open && (
              <span className="shrink-0 font-normal normal-case">
                · {calls.length} quer{calls.length === 1 ? "y" : "ies"}{truncated > 0 ? `, ${truncated} truncated` : ""}
              </span>
            )}
          </Collapsible.Trigger>
          <ExportReport turn={turn} />
        </div>
        <Collapsible.Content>
          <LedgerEntries calls={calls} />
        </Collapsible.Content>
      </section>
    </Collapsible.Root>
  );
}

function PreviousReport({ turn, calls }: { turn: Turn; calls: ToolCallView[] }) {
  const [open, setOpen] = useState(false);
  return (
    <Collapsible.Root open={open} onOpenChange={setOpen} className="panel overflow-hidden">
      <div className="flex items-start hover:bg-slate-50 dark:hover:bg-slate-800/60">
        <Collapsible.Trigger className="flex min-w-0 flex-1 items-start gap-2 px-3 py-2 text-left" title={turn.question}>
          <ChevronRight className={`mt-0.5 size-3.5 shrink-0 text-slate-400 transition ${open ? "rotate-90" : ""}`} />
          <Folder className="mt-0.5 size-3.5 shrink-0 text-ruby-600 dark:text-ruby-400" />
          <span className="min-w-0 flex-1 text-xs">
            <span className="line-clamp-2">Report for request “{turn.question}” for {roleLabel(turn.role)}</span>
            <span className="text-[11px] text-slate-500">{calls.length} quer{calls.length === 1 ? "y" : "ies"}</span>
          </span>
        </Collapsible.Trigger>
        <div className="py-1.5 pr-1.5"><ExportReport turn={turn} /></div>
      </div>
      <Collapsible.Content className="border-t border-slate-100 p-2 dark:border-slate-800">
        <LedgerEntries calls={calls} compact />
      </Collapsible.Content>
    </Collapsible.Root>
  );
}

/** Downloads one request's full report (workflow, results, SQL, answer) as Markdown. */
function ExportReport({ turn }: { turn: Turn }) {
  const toolCalls = useChat((s) => s.toolCalls);
  return (
    <button
      type="button"
      onClick={() => download(reportFilename(turn), turnToMarkdown(turn, toolCalls))}
      disabled={!turn.done}
      className="btn-ghost shrink-0 px-1.5 py-1 text-[11px]"
      title={turn.done ? "Export this request's full report (Markdown)" : "Available when the answer is finished"}
    >
      <Download className="size-3.5" /> Export
    </button>
  );
}

function LedgerEntries({ calls, compact = false }: { calls: ToolCallView[]; compact?: boolean }) {
  const focusToolCall = useChat((s) => s.focusToolCall);
  return (
    <ol className="space-y-2">
      {calls.map((call, index) => {
        const model = describeResult(call.output);
        return (
          <li key={call.id}>
            <button
              type="button"
              onClick={() => focusToolCall(call.id)}
              title="Show this call in the agent workflow"
              className={`group block w-full text-left transition hover:ring-1 hover:ring-ruby-400/60 ${compact ? "rounded-lg bg-slate-50 p-2 dark:bg-slate-800/50" : "panel p-3"}`}
            >
            <div className="flex items-center gap-2">
              <span className="grid size-5 place-items-center rounded-full bg-slate-100 text-[11px] font-semibold dark:bg-slate-800">{index + 1}</span>
              <span className="font-mono text-xs font-medium">{call.tool}</span>
              <span className="ml-auto flex items-center gap-1.5">
                <TruncationBadge model={model} />
                <ArrowUpRight className="size-3.5 text-slate-400 transition group-hover:text-ruby-500" />
              </span>
            </div>
            <p className="mt-1 text-xs text-slate-600 dark:text-slate-400">{model?.headline ?? "no structured result"}</p>
            {call.input && (
              <p className="mt-1 truncate font-mono text-[11px] text-slate-500" title={JSON.stringify(call.input)}>
                {JSON.stringify(call.input)}
              </p>
            )}
            </button>
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
