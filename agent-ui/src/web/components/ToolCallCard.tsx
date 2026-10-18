import * as Collapsible from "@radix-ui/react-collapsible";
import { AlertTriangle, Check, ChevronRight, Copy, Info, Loader2, Wrench, X } from "lucide-react";
import { Highlight, themes } from "prism-react-renderer";
import { useEffect, useRef, useState } from "react";
import type { ToolCallView } from "../../shared/events";
import { useChat } from "../store";
import { describeResult, humanize, type BarsView, type ResultModel, type Row, type TableView } from "../../shared/toolResult";

let handledFocus = 0;

/** Renders any MCP tool call from the shape of its result; no per-tool code. */
export function ToolCallCard({ call, defaultOpen = false }: { call: ToolCallView; defaultOpen?: boolean }) {
  const [open, setOpen] = useState(defaultOpen);
  const [flash, setFlash] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const focus = useChat((s) => s.focus);
  const model = call.status === "completed" ? describeResult(call.output) : null;

  // Revealed from the evidence ledger: expand, bring into view and pulse once.
  useEffect(() => {
    if (!focus || focus.id !== call.id || focus.nonce === handledFocus) return;
    handledFocus = focus.nonce; // a remounted card must not jump again for the same click
    setOpen(true);
    setFlash(true);
    const frame = requestAnimationFrame(() => ref.current?.scrollIntoView({ behavior: "smooth", block: "start" }));
    const timer = window.setTimeout(() => setFlash(false), 1800);
    return () => {
      cancelAnimationFrame(frame);
      window.clearTimeout(timer);
      setFlash(false);
    };
  }, [focus, call.id]);

  return (
    <Collapsible.Root
      ref={ref}
      id={`tool-${call.id}`}
      open={open}
      onOpenChange={setOpen}
      className={`panel scroll-mt-4 overflow-hidden transition-shadow duration-500 ${flash ? "ring-2 ring-ruby-500 shadow-[0_0_18px_rgba(244,63,94,0.45)]" : ""}`}
    >
      <Collapsible.Trigger className="flex w-full items-center gap-3 px-3 py-2 text-left hover:bg-slate-50 dark:hover:bg-slate-800/60">
        <ChevronRight className={`size-4 shrink-0 text-slate-400 transition ${open ? "rotate-90" : ""}`} />
        <Wrench className="size-4 shrink-0 text-ruby-600 dark:text-ruby-400" />
        <span className="font-mono text-sm font-medium">{call.tool ?? call.title}</span>
        <span className="hidden min-w-0 flex-1 truncate font-mono text-xs text-slate-500 sm:block">{formatArgs(call.input)}</span>
        <span className="ml-auto flex shrink-0 items-center gap-2">
          <TruncationBadge model={model} />
          <StatusIcon status={call.status} />
        </span>
      </Collapsible.Trigger>
      {model?.headline && (
        <p className="border-t border-slate-100 px-3 py-1.5 text-xs text-slate-600 dark:border-slate-800 dark:text-slate-400">{model.headline}</p>
      )}
      <Collapsible.Content className="space-y-3 border-t border-slate-100 p-3 dark:border-slate-800">
        {call.input && <KeyValues title="Arguments" values={call.input} />}
        {model ? <ResultView model={model} /> : call.status === "completed" && <RawOutput call={call} />}
        {model && call.outputText && <RawOutput call={call} />}
      </Collapsible.Content>
    </Collapsible.Root>
  );
}

function ResultView({ model }: { model: ResultModel }) {
  return (
    <>
      {model.stats.length > 0 && <Stats items={model.stats} />}
      {model.bars.map((bars) => <BarList key={bars.key} bars={bars} />)}
      {model.tables.map((table) => <DataTable key={table.key} table={table} />)}
      {model.notes.map((note) => (
        <p key={note.label} className="flex items-start gap-1.5 text-xs text-amber-700 dark:text-amber-400">
          <AlertTriangle className="mt-0.5 size-3 shrink-0" />
          <span><span className="font-medium">{note.label}:</span> {note.text}</span>
        </p>
      ))}
      {model.details.map((detail) => <KeyValues key={detail.label} title={detail.label} values={detail.values} />)}
      {model.sql.map((query) => <SqlBlock key={query.name} name={query.name} sql={query.sql} parameters={query.parameters} />)}
    </>
  );
}

function RawOutput({ call }: { call: ToolCallView }) {
  return (
    <pre className="max-h-64 overflow-auto rounded-lg bg-slate-100 p-2 font-mono text-xs dark:bg-slate-800">
      {call.outputText ?? JSON.stringify(call.output, null, 2)}
    </pre>
  );
}

function StatusIcon({ status }: { status: ToolCallView["status"] }) {
  if (status === "completed") return <Check className="size-4 text-emerald-600" aria-label="completed" />;
  if (status === "failed") return <X className="size-4 text-red-600" aria-label="failed" />;
  return <Loader2 className="size-4 animate-spin text-slate-400" aria-label="running" />;
}

export function TruncationBadge({ model }: { model: ResultModel | null }) {
  if (!model || model.truncated === null) return null;
  return model.truncated ? (
    <span className="chip bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-300">
      <AlertTriangle className="size-3" /> truncated
    </span>
  ) : (
    <span className="chip bg-emerald-100 text-emerald-800 dark:bg-emerald-900/40 dark:text-emerald-300">complete</span>
  );
}

function formatArgs(input: Record<string, unknown> | null): string {
  if (!input) return "";
  return Object.entries(input)
    .filter(([, v]) => v !== null && v !== undefined)
    .map(([k, v]) => `${k}=${typeof v === "string" ? JSON.stringify(v) : String(v)}`)
    .join("  ");
}

function KeyValues({ title, values }: { title: string; values: Row }) {
  const entries = Object.entries(values).filter(([, v]) => v !== null && v !== undefined);
  if (entries.length === 0) return null;
  return (
    <div>
      <h4 className="mb-1 flex items-center gap-1 text-xs font-semibold uppercase tracking-wide text-slate-500">
        <Info className="size-3" /> {title}
      </h4>
      <dl className="grid grid-cols-[max-content_1fr] gap-x-3 gap-y-0.5 font-mono text-xs">
        {entries.map(([k, v]) => (
          <div key={k} className="contents">
            <dt className="text-slate-500">{k}</dt>
            <dd className="break-all">{typeof v === "object" ? JSON.stringify(v) : String(v)}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

const SEVERITY_STYLES: Record<string, string> = {
  FATAL: "bg-red-100 text-red-800 dark:bg-red-900/40 dark:text-red-300",
  FAILURE: "bg-red-100 text-red-800 dark:bg-red-900/40 dark:text-red-300",
  CRITICAL: "bg-red-100 text-red-800 dark:bg-red-900/40 dark:text-red-300",
  SEVERE: "bg-orange-100 text-orange-800 dark:bg-orange-900/40 dark:text-orange-300",
  ERROR: "bg-orange-100 text-orange-800 dark:bg-orange-900/40 dark:text-orange-300",
  WARNING: "bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-300",
  WARN: "bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-300",
};

const BADGE_COLUMN = /severity|level|status/i;
const TIME_COLUMN = /timestamp|time$|^ts$|_at$/i;
const WIDE_COLUMN = /message|text|description/i;
const OFFSET_COLUMN = /seconds|offset|gap|latency/i;

function Cell({ column, value }: { column: string; value: unknown }) {
  if (value === null || value === undefined) return <span className="text-slate-300 dark:text-slate-600">–</span>;
  if (BADGE_COLUMN.test(column) && typeof value === "string") {
    return <span className={`chip ${SEVERITY_STYLES[value.toUpperCase()] ?? "bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300"}`}>{value}</span>;
  }
  if (TIME_COLUMN.test(column) && typeof value === "string") return <span className="whitespace-nowrap">{value.replace("T", " ")}</span>;
  if (typeof value === "number") {
    const signed = OFFSET_COLUMN.test(column) && value > 0 ? "+" : "";
    return <>{signed}{value.toLocaleString("en-US", { maximumFractionDigits: 3 })}</>;
  }
  if (typeof value === "boolean") return <>{value ? "yes" : "no"}</>;
  return <>{String(value)}</>;
}

function DataTable({ table }: { table: TableView }) {
  const numeric = new Set(table.columns.filter((c) => table.rows.every((r) => r[c] === null || typeof r[c] === "number")));
  return (
    <div>
      <h4 className="mb-1 text-xs font-semibold uppercase tracking-wide text-slate-500">
        {table.label} <span className="font-normal normal-case">({table.rows.length})</span>
      </h4>
      <div className="max-h-80 overflow-auto rounded-lg border border-slate-100 dark:border-slate-800">
        <table className="w-full text-xs">
          <thead className="sticky top-0 bg-slate-50 text-left text-slate-500 dark:bg-slate-900">
            <tr>
              {table.columns.map((c) => (
                <th key={c} className={`px-2 py-1 font-medium whitespace-nowrap ${numeric.has(c) ? "text-right" : ""}`}>{humanize(c)}</th>
              ))}
            </tr>
          </thead>
          <tbody className="font-mono">
            {table.rows.map((row, i) => (
              <tr
                key={i}
                className={`border-t border-slate-100 align-top dark:border-slate-800 ${i === table.highlightRow ? "bg-ruby-50 dark:bg-ruby-900/20" : ""}`}
              >
                {table.columns.map((c) => (
                  <td
                    key={c}
                    className={`px-2 py-1 ${numeric.has(c) ? "text-right tabular-nums" : ""} ${WIDE_COLUMN.test(c) ? "min-w-64 font-sans" : ""}`}
                  >
                    <Cell column={c} value={row[c]} />
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function BarList({ bars }: { bars: BarsView }) {
  const max = Math.max(1, ...bars.rows.map((r) => Number(r[bars.countKey] ?? 0)));
  return (
    <div>
      <h4 className="mb-1 text-xs font-semibold uppercase tracking-wide text-slate-500">
        {bars.label} <span className="font-normal normal-case">by {humanize(bars.countKey)}</span>
      </h4>
      <ul className="space-y-1">
        {bars.rows.map((row, i) => {
          const count = Number(row[bars.countKey] ?? 0);
          const label = bars.labelKeys.map((k) => (row[k] === null ? "∅" : String(row[k]))).join(" · ");
          return (
            <li key={i} className="grid grid-cols-[minmax(8rem,14rem)_1fr_auto] items-center gap-2 text-xs">
              <span className="truncate font-mono" title={label}>{label}</span>
              <span className="h-2 rounded-full bg-slate-100 dark:bg-slate-800">
                <span className="block h-2 rounded-full bg-ruby-500" style={{ width: `${Math.max(1, (count / max) * 100)}%` }} />
              </span>
              <span className="text-right font-mono tabular-nums">{count.toLocaleString("en-US")}</span>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

function Stats({ items }: { items: ResultModel["stats"] }) {
  return (
    <dl className="grid grid-cols-2 gap-2 sm:grid-cols-4">
      {items.map((item) => (
        <div key={item.key} className="rounded-lg bg-slate-50 px-2 py-1.5 dark:bg-slate-800/60">
          <dt className="truncate text-[11px] uppercase tracking-wide text-slate-500" title={item.label}>{item.label}</dt>
          <dd className="truncate font-mono text-sm" title={item.value}>{item.value}</dd>
        </div>
      ))}
    </dl>
  );
}

export function SqlBlock({ name, sql, parameters }: { name: string; sql: string; parameters: Row }) {
  const [copied, setCopied] = useState(false);
  const dark = document.documentElement.classList.contains("dark");
  const copy = async () => {
    await navigator.clipboard.writeText(`${sql}\n-- parameters: ${JSON.stringify(parameters)}`);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1200);
  };
  return (
    <div className="overflow-hidden rounded-lg border border-slate-200 dark:border-slate-700">
      <div className="flex items-center justify-between bg-slate-50 px-2 py-1 text-xs dark:bg-slate-800">
        <span className="font-semibold uppercase tracking-wide text-slate-500">SQL · {humanize(name)}</span>
        <button type="button" onClick={copy} className="btn-ghost px-1.5 py-0.5 text-xs" aria-label="Copy SQL">
          {copied ? <Check className="size-3" /> : <Copy className="size-3" />} {copied ? "copied" : "copy"}
        </button>
      </div>
      <Highlight code={sql.trim()} language="sql" theme={dark ? themes.vsDark : themes.github}>
        {({ tokens, getLineProps, getTokenProps }) => (
          <pre className="overflow-x-auto p-2 font-mono text-xs leading-relaxed">
            {tokens.map((line, i) => (
              <div key={i} {...getLineProps({ line })}>
                {line.map((token, key) => <span key={key} {...getTokenProps({ token })} />)}
              </div>
            ))}
          </pre>
        )}
      </Highlight>
      {Object.keys(parameters).length > 0 && (
        <div className="border-t border-slate-100 bg-slate-50/60 px-2 py-1 font-mono text-[11px] text-slate-600 dark:border-slate-800 dark:bg-slate-800/40 dark:text-slate-400">
          parameters: {JSON.stringify(parameters)}
        </div>
      )}
    </div>
  );
}
