import * as Collapsible from "@radix-ui/react-collapsible";
import { AlertTriangle, BarChart3, Check, ChevronRight, Clock, Copy, Database, Loader2, Search, X } from "lucide-react";
import { Highlight, themes } from "prism-react-renderer";
import { useState } from "react";
import type { ToolCallView } from "../../shared/events";
import {
  classifyRubinResult,
  executedQueries,
  resultHeadline,
  type AggregationResult,
  type NearbyResult,
  type RubinResult,
  type SearchResult,
} from "../../shared/rubin";

const TOOL_ICONS: Record<string, typeof Search> = {
  search_logs: Search,
  summarize_logs: BarChart3,
  get_nearby_events: Clock,
};

export function ToolCallCard({ call, defaultOpen = false }: { call: ToolCallView; defaultOpen?: boolean }) {
  const [open, setOpen] = useState(defaultOpen);
  const result = call.status === "completed" ? classifyRubinResult(call.tool, call.output) : null;
  const Icon = TOOL_ICONS[call.tool ?? ""] ?? Database;
  return (
    <Collapsible.Root open={open} onOpenChange={setOpen} className="panel overflow-hidden">
      <Collapsible.Trigger className="flex w-full items-center gap-3 px-3 py-2 text-left hover:bg-slate-50 dark:hover:bg-slate-800/60">
        <ChevronRight className={`size-4 shrink-0 text-slate-400 transition ${open ? "rotate-90" : ""}`} />
        <Icon className="size-4 shrink-0 text-ruby-600 dark:text-ruby-400" />
        <span className="font-mono text-sm font-medium">{call.tool ?? call.title}</span>
        <span className="hidden min-w-0 flex-1 truncate font-mono text-xs text-slate-500 sm:block">{formatArgs(call.input)}</span>
        <span className="ml-auto flex shrink-0 items-center gap-2">
          {result && result.kind !== "unknown" && <TruncationBadge result={result} />}
          <StatusIcon status={call.status} />
        </span>
      </Collapsible.Trigger>
      {result && result.kind !== "unknown" && (
        <p className="border-t border-slate-100 px-3 py-1.5 text-xs text-slate-600 dark:border-slate-800 dark:text-slate-400">
          {resultHeadline(result)}
        </p>
      )}
      <Collapsible.Content className="space-y-3 border-t border-slate-100 p-3 dark:border-slate-800">
        {call.input && <KeyValues title="Arguments" values={call.input} />}
        {result?.kind === "search" && <SearchView data={result.data} />}
        {result?.kind === "aggregation" && <AggregationView data={result.data} />}
        {result?.kind === "nearby" && <NearbyView data={result.data} />}
        {result && executedQueries(result).map((q) => <SqlBlock key={q.name} name={q.name} sql={q.sql} parameters={q.parameters} />)}
        {(result?.kind === "unknown" || call.outputText) && (
          <pre className="max-h-64 overflow-auto rounded-lg bg-slate-100 p-2 font-mono text-xs dark:bg-slate-800">
            {call.outputText ?? JSON.stringify(call.output, null, 2)}
          </pre>
        )}
      </Collapsible.Content>
    </Collapsible.Root>
  );
}

function StatusIcon({ status }: { status: ToolCallView["status"] }) {
  if (status === "completed") return <Check className="size-4 text-emerald-600" aria-label="completed" />;
  if (status === "failed") return <X className="size-4 text-red-600" aria-label="failed" />;
  return <Loader2 className="size-4 animate-spin text-slate-400" aria-label="running" />;
}

export function TruncationBadge({ result }: { result: RubinResult }) {
  if (result.kind === "unknown" || result.kind === "nearby") return null;
  return result.data.truncated ? (
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

function KeyValues({ title, values }: { title: string; values: Record<string, unknown> }) {
  const entries = Object.entries(values).filter(([, v]) => v !== null && v !== undefined);
  if (entries.length === 0) return null;
  return (
    <div>
      <h4 className="mb-1 text-xs font-semibold uppercase tracking-wide text-slate-500">{title}</h4>
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
  SEVERE: "bg-orange-100 text-orange-800 dark:bg-orange-900/40 dark:text-orange-300",
  ERROR: "bg-orange-100 text-orange-800 dark:bg-orange-900/40 dark:text-orange-300",
  WARNING: "bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-300",
};

function Severity({ value }: { value: unknown }) {
  const text = String(value ?? "");
  return <span className={`chip ${SEVERITY_STYLES[text.toUpperCase()] ?? "bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300"}`}>{text}</span>;
}

function RecordsTable({ rows, anchorId }: { rows: Record<string, unknown>[]; anchorId?: unknown }) {
  if (rows.length === 0) return <p className="text-xs text-slate-500">No records.</p>;
  const hasOffset = rows.some((r) => "seconds_from_anchor" in r);
  return (
    <div className="max-h-80 overflow-auto rounded-lg border border-slate-100 dark:border-slate-800">
      <table className="w-full text-xs">
        <thead className="sticky top-0 bg-slate-50 text-left text-slate-500 dark:bg-slate-900">
          <tr>
            <th className="px-2 py-1 font-medium">record_id</th>
            <th className="px-2 py-1 font-medium">timestamp</th>
            {hasOffset && <th className="px-2 py-1 text-right font-medium">Δs</th>}
            <th className="px-2 py-1 font-medium">component</th>
            <th className="px-2 py-1 font-medium">severity</th>
            <th className="px-2 py-1 font-medium">message</th>
          </tr>
        </thead>
        <tbody className="font-mono">
          {rows.map((row, i) => (
            <tr
              key={`${String(row.record_id)}-${i}`}
              className={`border-t border-slate-100 align-top dark:border-slate-800 ${row.record_id === anchorId ? "bg-ruby-50 dark:bg-ruby-900/20" : ""}`}
            >
              <td className="px-2 py-1 tabular-nums">{String(row.record_id)}</td>
              <td className="whitespace-nowrap px-2 py-1 tabular-nums">{String(row.timestamp ?? "").replace("T", " ")}</td>
              {hasOffset && <td className="px-2 py-1 text-right tabular-nums">{formatOffset(row.seconds_from_anchor)}</td>}
              <td className="px-2 py-1">{String(row.component ?? "")}</td>
              <td className="px-2 py-1"><Severity value={row.severity} /></td>
              <td className="min-w-64 px-2 py-1 font-sans">{String(row.message ?? "")}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function formatOffset(value: unknown): string {
  return typeof value === "number" ? `${value > 0 ? "+" : ""}${value.toFixed(3)}` : "";
}

function SearchView({ data }: { data: SearchResult }) {
  return (
    <div className="space-y-2">
      <Stats
        items={[
          ["dataset", data.provenance.dataset],
          ["match mode", data.match_mode ?? "any"],
          ["returned", data.returned_record_count.toLocaleString("en-US")],
          ["total matches in corpus", data.total_corpus_match_count.toLocaleString("en-US")],
        ]}
      />
      <RecordsTable rows={data.records} />
    </div>
  );
}

function groupLabel(group: Record<string, unknown>): string {
  return Object.entries(group)
    .filter(([k]) => k !== "record_count")
    .map(([, v]) => (v === null ? "∅" : String(v)))
    .join(" · ");
}

function AggregationView({ data }: { data: AggregationResult }) {
  const max = Math.max(1, ...data.groups.map((g) => Number(g.record_count ?? 0)));
  const coverage = data.mapping_coverage;
  return (
    <div className="space-y-2">
      <Stats
        items={[
          ["dataset", data.dataset],
          ["group by", [data.group_by, data.interval].filter(Boolean).join(" / ") || data.aggregation_type],
          ["records in scope", data.total_matching_record_count.toLocaleString("en-US")],
          ["unit", data.scope.count_unit],
        ]}
      />
      <ul className="space-y-1">
        {data.groups.map((group, i) => {
          const count = Number(group.record_count ?? 0);
          return (
            <li key={i} className="grid grid-cols-[minmax(8rem,14rem)_1fr_auto] items-center gap-2 text-xs">
              <span className="truncate font-mono" title={groupLabel(group)}>{groupLabel(group)}</span>
              <span className="h-2 rounded-full bg-slate-100 dark:bg-slate-800">
                <span className="block h-2 rounded-full bg-ruby-500" style={{ width: `${Math.max(1, (count / max) * 100)}%` }} />
              </span>
              <span className="text-right font-mono tabular-nums">{count.toLocaleString("en-US")}</span>
            </li>
          );
        })}
      </ul>
      {coverage && <KeyValues title="Ownership mapping coverage" values={coverage} />}
      {data.provenance.fictional_ownership && (
        <p className="text-xs text-slate-500">Team ownership is a fictional hackathon mapping, not an organisational fact.</p>
      )}
    </div>
  );
}

function NearbyView({ data }: { data: NearbyResult }) {
  if (data.status !== "ok" || !data.anchor) return <p className="text-xs text-amber-700">Anchor record not found.</p>;
  const rows = [...data.before_events, data.anchor, ...data.after_events];
  return (
    <div className="space-y-2">
      <RecordsTable rows={rows} anchorId={data.anchor.record_id} />
      <p className="flex items-center gap-1 text-xs text-amber-700 dark:text-amber-400">
        <AlertTriangle className="size-3" /> {data.causation_warning}
      </p>
    </div>
  );
}

function Stats({ items }: { items: [string, string][] }) {
  return (
    <dl className="grid grid-cols-2 gap-2 sm:grid-cols-4">
      {items.map(([label, value]) => (
        <div key={label} className="rounded-lg bg-slate-50 px-2 py-1.5 dark:bg-slate-800/60">
          <dt className="text-[11px] uppercase tracking-wide text-slate-500">{label}</dt>
          <dd className="truncate font-mono text-sm" title={value}>{value}</dd>
        </div>
      ))}
    </dl>
  );
}

export function SqlBlock({ name, sql, parameters }: { name: string; sql: string; parameters: Record<string, unknown> }) {
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
        <span className="font-semibold uppercase tracking-wide text-slate-500">SQL · {name.replace(/_/g, " ")}</span>
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
