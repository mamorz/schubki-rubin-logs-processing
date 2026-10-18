import * as Collapsible from "@radix-ui/react-collapsible";
import { Brain, ChevronDown, ChevronRight, CircleStop, Coins, ListTree, SendHorizontal, Sparkles } from "lucide-react";
import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { ROLES, type RoleId, type ToolCallView } from "../../shared/events";
import { SUGGESTED_QUESTIONS } from "../questions";
import { useChat, type Turn } from "../store";
import { ToolCallCard } from "./ToolCallCard";

export function ChatThread({ onAsk }: { onAsk: (question: string) => void }) {
  const turns = useChat((s) => s.turns);
  const role = useChat((s) => s.role);
  const endRef = useRef<HTMLDivElement>(null);
  const last = turns[turns.length - 1];

  useEffect(() => {
    endRef.current?.scrollIntoView({ block: "end" });
  }, [turns.length, last?.text, last?.toolCallIds.length]);

  if (turns.length === 0) return <EmptyState role={role} onAsk={onAsk} />;
  return (
    <div className="space-y-8">
      {turns.map((turn) => <TurnView key={turn.id} turn={turn} />)}
      <div ref={endRef} />
    </div>
  );
}

function EmptyState({ role, onAsk }: { role: RoleId; onAsk: (question: string) => void }) {
  const definition = ROLES.find((r) => r.id === role)!;
  return (
    <div className="mx-auto max-w-2xl py-10 text-center">
      <Sparkles className="mx-auto size-8 text-ruby-500" />
      <h2 className="mt-3 text-xl font-semibold">Ask as the {definition.label.toLowerCase()}</h2>
      <p className="mt-1 text-sm text-slate-500">{definition.summary}</p>
      <ul className="mt-6 space-y-2 text-left">
        {SUGGESTED_QUESTIONS[role].map((question) => (
          <li key={question}>
            <button
              type="button"
              onClick={() => onAsk(question)}
              className="panel w-full px-4 py-3 text-left text-sm transition hover:border-ruby-300 hover:shadow dark:hover:border-ruby-700"
            >
              {question}
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}

function TurnView({ turn }: { turn: Turn }) {
  const toolCalls = useChat((s) => s.toolCalls);
  const role = ROLES.find((r) => r.id === turn.role)!;
  const thinking = !turn.done && !turn.text && turn.toolCallIds.length === 0;
  return (
    <article className="space-y-3">
      <div className="flex justify-end">
        <div className="max-w-[85%] rounded-2xl rounded-br-sm bg-ruby-600 px-4 py-2.5 text-sm text-white shadow-sm">
          <div className="mb-0.5 text-[11px] font-semibold uppercase tracking-wide text-ruby-100">{role.label}</div>
          {turn.question}
        </div>
      </div>

      {turn.thought && <Thought text={turn.thought} />}

      {turn.toolCallIds.length > 0 && <EvidenceTrail ids={turn.toolCallIds} toolCalls={toolCalls} />}

      {thinking && (
        <div className="flex items-center gap-2 text-sm text-slate-500">
          <span className="flex gap-1">
            {[0, 1, 2].map((i) => (
              <span key={i} className="size-1.5 animate-bounce rounded-full bg-ruby-400" style={{ animationDelay: `${i * 120}ms` }} />
            ))}
          </span>
          Kiro is thinking…
        </div>
      )}

      {turn.text && (
        <div className="prose-answer">
          <Markdown remarkPlugins={[remarkGfm]}>{turn.text}</Markdown>
        </div>
      )}

      {turn.error && <p className="rounded-lg bg-red-50 px-3 py-2 text-sm text-red-700 dark:bg-red-950/40 dark:text-red-300">{turn.error}</p>}

      {turn.done && (
        <p className="flex flex-wrap items-center gap-3 text-xs text-slate-400">
          <span>{turn.toolCallIds.length} tool call{turn.toolCallIds.length === 1 ? "" : "s"}</span>
          {turn.durationMs !== null && <span>{(turn.durationMs / 1000).toFixed(1)} s</span>}
          {turn.credits !== null && (
            <span className="inline-flex items-center gap-1"><Coins className="size-3" /> {turn.credits.toFixed(3)} credits</span>
          )}
          {turn.stopReason && turn.stopReason !== "end_turn" && <span>stopped: {turn.stopReason}</span>}
        </p>
      )}
    </article>
  );
}

const TRAIL_AUTO_FOLD_AFTER = 5;

/** Tool-call cards of one turn; folds itself once more than five calls have arrived, unless the user toggled it. */
function EvidenceTrail({ ids, toolCalls }: { ids: string[]; toolCalls: Record<string, ToolCallView> }) {
  const [userOpen, setUserOpen] = useState<boolean | null>(null);
  const open = userOpen ?? ids.length <= TRAIL_AUTO_FOLD_AFTER;
  const running = ids.filter((id) => toolCalls[id]?.status !== "completed" && toolCalls[id]?.status !== "failed").length;
  return (
    <Collapsible.Root open={open} onOpenChange={setUserOpen}>
      <Collapsible.Trigger className="mb-2 flex items-center gap-1.5 text-xs font-medium text-slate-500 hover:text-slate-700 dark:hover:text-slate-300">
        <ChevronRight className={`size-3 transition ${open ? "rotate-90" : ""}`} />
        <ListTree className="size-3" /> Evidence trail · {ids.length} tool call{ids.length === 1 ? "" : "s"}
        {running > 0 && <span className="text-amber-600">· {running} running</span>}
      </Collapsible.Trigger>
      <Collapsible.Content className="space-y-2">
        {ids.map((id) => {
          const call = toolCalls[id];
          return call ? <ToolCallCard key={id} call={call} /> : null;
        })}
      </Collapsible.Content>
    </Collapsible.Root>
  );
}

function Thought({ text }: { text: string }) {
  const [open, setOpen] = useState(false);
  return (
    <Collapsible.Root open={open} onOpenChange={setOpen}>
      <Collapsible.Trigger className="flex items-center gap-1.5 text-xs text-slate-500 hover:text-slate-700 dark:hover:text-slate-300">
        <ChevronRight className={`size-3 transition ${open ? "rotate-90" : ""}`} />
        <Brain className="size-3" /> Agent reasoning
      </Collapsible.Trigger>
      <Collapsible.Content className="mt-1 border-l-2 border-slate-200 pl-3 text-xs whitespace-pre-wrap text-slate-500 dark:border-slate-700">
        {text}
      </Collapsible.Content>
    </Collapsible.Root>
  );
}

export function Composer({ disabled, busy, role, onRole, onAsk, onCancel }: {
  disabled: boolean;
  busy: boolean;
  role: RoleId;
  onRole: (role: RoleId) => void;
  onAsk: (question: string) => void;
  onCancel: () => void;
}) {
  const [text, setText] = useState("");
  const submit = () => {
    const question = text.trim();
    if (!question || disabled || busy) return;
    onAsk(question);
    setText("");
  };
  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      submit();
    }
  };
  return (
    <div className="panel flex items-end gap-2 p-2">
      <textarea
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={onKeyDown}
        rows={2}
        placeholder={disabled ? "Waiting for the agent…" : "Ask about the BGL or OpenStack logs…"}
        disabled={disabled}
        className="min-h-12 flex-1 resize-none bg-transparent px-2 py-1.5 text-sm outline-none placeholder:text-slate-400"
      />
      <label className="relative shrink-0">
        <span className="sr-only">Role</span>
        <select
          value={role}
          onChange={(e) => onRole(e.target.value as RoleId)}
          disabled={busy}
          className="appearance-none rounded-lg border border-slate-200 bg-slate-50 py-2 pr-7 pl-2.5 text-sm font-medium text-slate-700 outline-none hover:border-ruby-300 focus-visible:border-ruby-500 disabled:opacity-50 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-200"
          title="Answer as this role"
        >
          {ROLES.map((r) => <option key={r.id} value={r.id}>{r.label}</option>)}
        </select>
        <ChevronDown className="pointer-events-none absolute top-1/2 right-2 size-3.5 -translate-y-1/2 text-slate-400" />
      </label>
      {busy ? (
        <button type="button" onClick={onCancel} className="btn-ghost" aria-label="Stop">
          <CircleStop className="size-4" /> Stop
        </button>
      ) : (
        <button type="button" onClick={submit} disabled={disabled || !text.trim()} className="btn-primary" aria-label="Send">
          <SendHorizontal className="size-4" />
        </button>
      )}
    </div>
  );
}
