import * as Collapsible from "@radix-ui/react-collapsible";
import { AlertTriangle, Check, ChevronRight, Copy, Info, Loader2, Wrench, X } from "lucide-react";
import { Highlight, themes } from "prism-react-renderer";
import { Fragment, useEffect, useRef, useState } from "react";
import type { ToolCallView } from "../../shared/events";
import { useChat } from "../store";
import { describeResult, humanize, type BarsView, type ResultModel, type Row, type SeriesView, type TableView } from "../../shared/toolResult";

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
          <StatusIcon status={model?.error ? "failed" : call.status} />
        </span>
      </Collapsible.Trigger>
      {model?.headline && (
        <p className="border-t border-slate-100 px-3 py-1.5 text-xs text-slate-600 dark:border-slate-800 dark:text-slate-400">{model.headline}</p>
      )}
      <Collapsible.Content className="space-y-3 border-t border-slate-100 p-3 dark:border-slate-800">
        {call.input && <KeyValues title="Arguments" values={call.input} />}
        {call.status === "failed" && <ErrorNote text={call.outputText ?? (call.output ? JSON.stringify(call.output) : "The tool call failed.")} />}
        {model ? <ResultView model={model} /> : call.status === "completed" && <RawOutput call={call} />}
        {model && call.outputText && <RawOutput call={call} />}
      </Collapsible.Content>
    </Collapsible.Root>
  );
}

function ResultView({ model }: { model: ResultModel }) {
  const seriesLabels = new Set(model.series.map((series) => series.label));
  return (
    <>
      {model.error && <ErrorNote text={model.error} />}
      {model.stats.length > 0 && <Stats items={model.stats} />}
      {model.statGroups.map((group) => (
        <div key={group.key}>
          <h4 className="mb-1 text-xs font-semibold uppercase tracking-wide text-slate-500">{group.label}</h4>
          <Stats items={group.stats} />
        </div>
      ))}
      {model.series.map((series) => <SeriesChart key={series.key + series.label} series={series} />)}
      {model.bars.map((bars) => <BarList key={bars.key + bars.label} bars={bars} />)}
      {model.tables.map((table) =>
        seriesLabels.has(table.label) ? (
          <Collapsible.Root key={table.key}>
            <Collapsible.Trigger className="group flex items-center gap-1 text-xs font-medium text-slate-500 hover:text-slate-700 dark:hover:text-slate-300">
              <ChevronRight className="size-3 transition group-data-[state=open]:rotate-90" /> Table view · {table.label}
            </Collapsible.Trigger>
            <Collapsible.Content className="mt-2"><DataTable table={table} /></Collapsible.Content>
          </Collapsible.Root>
        ) : (
          <DataTable key={table.key} table={table} />
        ),
      )}
      {model.notes.map((note, i) => (
        <p key={i} className="flex items-start gap-1.5 text-xs text-amber-700 dark:text-amber-400">
          <AlertTriangle className="mt-0.5 size-3 shrink-0" />
          <span><span className="font-medium">{note.label}:</span> {note.text}</span>
        </p>
      ))}
      {model.details.map((detail) => <KeyValues key={detail.label} title={detail.label} values={detail.values} />)}
      {model.sql.map((query, i) => <SqlBlock key={`${query.name}-${i}`} name={query.name} sql={query.sql} parameters={query.parameters} />)}
    </>
  );
}

function ErrorNote({ text }: { text: string }) {
  return (
    <p className="flex items-start gap-1.5 rounded-lg bg-red-50 px-2.5 py-2 text-xs text-red-700 dark:bg-red-950/40 dark:text-red-300">
      <X className="mt-0.5 size-3.5 shrink-0" /> <span className="font-mono">{text}</span>
    </p>
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

/** Full chip by default; `compact` (narrow rows) shows only a small icon when truncated and nothing when complete. */
export function TruncationBadge({ model, compact = false }: { model: ResultModel | null; compact?: boolean }) {
  if (!model || model.truncated === null || model.error) return null;
  const sections = [...new Set(model.truncatedSections)].join(", ");
  const title = sections ? `Truncated: ${sections}` : "Not every matching row was returned";
  if (compact) {
    return model.truncated ? <AlertTriangle className="size-3.5 shrink-0 text-amber-600 dark:text-amber-400" aria-label={title}><title>{title}</title></AlertTriangle> : null;
  }
  return model.truncated ? (
    <span className="chip bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-300" title={title}>
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

const BADGE_COLUMN = /severity|level|^status$/i;
const HTTP_COLUMN = /http_status|status_code/i;
const FLAG_COLUMN = /^(is|has)_/i;
const BAD_FLAG = /anomal|error|fail|slow|timeout/i;
const TIME_COLUMN = /timestamp|time$|^ts$|_at$|^bucket|_start$|_end$|^date$|^period$|^observed/i;
const WIDE_COLUMN = /message|text|description/i;
/** Signed values relative to something (anchor, previous event); durations stay unsigned. */
const OFFSET_COLUMN = /from_anchor|offset|^gap|_gap|delta/i;

function httpStyle(code: number): string {
  if (code >= 500) return SEVERITY_STYLES.ERROR!;
  if (code >= 400) return SEVERITY_STYLES.WARNING!;
  if (code >= 200 && code < 300) return "bg-emerald-100 text-emerald-800 dark:bg-emerald-900/40 dark:text-emerald-300";
  return "bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300";
}

function Cell({ column, value }: { column: string; value: unknown }) {
  if (value === null || value === undefined) return <span className="text-slate-300 dark:text-slate-600">–</span>;
  if (HTTP_COLUMN.test(column) && typeof value === "number") return <span className={`chip ${httpStyle(value)}`}>{value}</span>;
  if (typeof value === "boolean") {
    if (!FLAG_COLUMN.test(column)) return <>{value ? "yes" : "no"}</>;
    const tone = !value
      ? "bg-slate-100 text-slate-500 dark:bg-slate-800 dark:text-slate-400"
      : BAD_FLAG.test(column) ? SEVERITY_STYLES.ERROR! : "bg-sky-100 text-sky-800 dark:bg-sky-900/40 dark:text-sky-300";
    return <span className={`chip ${tone}`}>{value ? humanize(column.replace(FLAG_COLUMN, "")) : "no"}</span>;
  }
  if (BADGE_COLUMN.test(column) && typeof value === "string") {
    return <span className={`chip ${SEVERITY_STYLES[value.toUpperCase()] ?? "bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300"}`}>{value}</span>;
  }
  if (TIME_COLUMN.test(column) && typeof value === "string") return <span className="whitespace-nowrap">{value.replace("T", " ")}</span>;
  if (typeof value === "number") {
    const signed = OFFSET_COLUMN.test(column) && value > 0 ? "+" : "";
    return <>{signed}{value.toLocaleString("en-US", { maximumFractionDigits: 3 })}</>;
  }
  return <>{String(value)}</>;
}

/** Rows that deserve attention in any table: flagged anomalies/slow events or error severities. */
function isFlagged(row: Row): boolean {
  return Object.entries(row).some(
    ([k, v]) => (FLAG_COLUMN.test(k) && BAD_FLAG.test(k) && v === true) || (BADGE_COLUMN.test(k) && typeof v === "string" && /^(ERROR|FATAL|FAILURE|CRITICAL|SEVERE)$/i.test(v)),
  );
}

function formatGap(ms: number): string {
  const s = ms / 1000;
  if (s < 1) return `+${s.toFixed(3)} s`;
  if (s < 120) return `+${s.toFixed(1)} s`;
  if (s < 7200) return `+${(s / 60).toFixed(1)} min`;
  return `+${(s / 3600).toFixed(1)} h`;
}

function DataTable({ table }: { table: TableView }) {
  const numeric = new Set(table.columns.filter((c) => table.rows.every((r) => r[c] === null || typeof r[c] === "number")));
  const timeKey = table.columns.find((c) => /timestamp|^ts$|_at$/i.test(c));
  const times = timeKey ? table.rows.map((r) => Date.parse(String(r[timeKey]))) : [];
  const showGap = table.chronological && !table.columns.some((c) => /gap/i.test(c));
  const [expanded, setExpanded] = useState<Set<number>>(() => new Set());
  const hasNested = table.nested.length > 0;
  const span = table.columns.length + (table.chronological ? 1 : 0) + (showGap ? 1 : 0) + (hasNested ? 1 : 0);
  const toggle = (i: number) =>
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(i)) next.delete(i);
      else next.add(i);
      return next;
    });
  return (
    <div>
      <h4 className="mb-1 text-xs font-semibold uppercase tracking-wide text-slate-500">
        {table.label} <span className="font-normal normal-case">({table.rows.length}{table.chronological ? ", in time order" : ""})</span>
      </h4>
      <div className={`${hasNested ? "max-h-[36rem]" : "max-h-80"} overflow-auto rounded-lg border border-slate-100 dark:border-slate-800`}>
        <table className="w-full text-xs">
          <thead className="sticky top-0 z-10 bg-slate-50 text-left text-slate-500 dark:bg-slate-900">
            <tr>
              {hasNested && <th className="w-6 px-0" aria-label="details" />}
              {table.chronological && <th className="w-6 px-0" aria-label="timeline" />}
              {table.columns.map((c) => (
                <th key={c} className={`px-2 py-1 font-medium whitespace-nowrap ${numeric.has(c) ? "text-right" : ""}`}>{humanize(c)}</th>
              ))}
              {showGap && <th className="px-2 py-1 text-right font-medium whitespace-nowrap">Δ prev</th>}
            </tr>
          </thead>
          <tbody className="font-mono">
            {table.rows.map((row, i) => {
              const flagged = isFlagged(row);
              const anchor = i === table.highlightRow;
              const isOpen = expanded.has(i);
              return (
                <Fragment key={i}>
                <tr
                  className={`border-t border-slate-100 align-top dark:border-slate-800 ${anchor ? "bg-ruby-50 dark:bg-ruby-900/20" : flagged ? "bg-red-50/40 dark:bg-red-950/15" : ""}`}
                >
                  {hasNested && (
                    <td className="w-6 px-0 py-0.5 text-center">
                      <button
                        type="button"
                        onClick={() => toggle(i)}
                        aria-expanded={isOpen}
                        title={`${isOpen ? "Hide" : "Show"} ${table.nested.map(humanize).join(", ")}`}
                        className="rounded p-0.5 text-slate-400 hover:bg-slate-100 hover:text-ruby-600 dark:hover:bg-slate-800"
                      >
                        <ChevronRight className={`size-3.5 transition ${isOpen ? "rotate-90" : ""}`} />
                      </button>
                    </td>
                  )}
                  {table.chronological && (
                    <td
                      aria-hidden="true"
                      className="w-6 bg-linear-to-b from-slate-200 to-slate-200 bg-[length:1px_100%] bg-center bg-no-repeat px-0 py-1 dark:from-slate-700 dark:to-slate-700"
                    >
                      <span className={`mx-auto mt-1 block size-2 rounded-full ring-2 ring-white dark:ring-slate-900 ${anchor || flagged ? "bg-ruby-600 dark:bg-ruby-500" : "bg-slate-400"}`} />
                    </td>
                  )}
                  {table.columns.map((c) => (
                    <td key={c} className={`px-2 py-1 ${numeric.has(c) ? "text-right tabular-nums" : ""} ${WIDE_COLUMN.test(c) ? "min-w-64 font-sans" : ""} ${/(^|_)id$/i.test(c) ? "whitespace-nowrap" : ""}`}>
                      <Cell column={c} value={row[c]} />
                    </td>
                  ))}
                  {showGap && (
                    <td className="px-2 py-1 text-right whitespace-nowrap text-slate-500 tabular-nums">
                      {i > 0 && !Number.isNaN(times[i]!) ? formatGap(times[i]! - times[i - 1]!) : ""}
                    </td>
                  )}
                </tr>
                {isOpen && (
                  <tr className="bg-slate-50/70 dark:bg-slate-900/60">
                    <td colSpan={span} className="px-3 py-2 font-sans">
                      <div className="space-y-3 border-l-2 border-ruby-500/50 pl-3">
                        {table.nested.map((key) => {
                          const model = describeResult({ [key]: row[key] });
                          return model ? <ResultView key={key} model={model} /> : null;
                        })}
                      </div>
                    </td>
                  </tr>
                )}
                </Fragment>
              );
            })}
          </tbody>
        </table>
      </div>
      {table.unavailable.length > 0 && (
        <p className="mt-1 text-[11px] text-slate-500">Not available in any returned row: {table.unavailable.map(humanize).join(", ")}</p>
      )}
    </div>
  );
}

const compact = new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 });

/** Small multiples: one column chart per metric, each on its own scale, hover synced across charts. */
function SeriesChart({ series }: { series: SeriesView }) {
  const [hover, setHover] = useState<number | null>(null);
  // Plot oldest → newest even when the tool sorted buckets newest first.
  const first = Date.parse(String(series.rows[0]?.[series.xKey]));
  const last = Date.parse(String(series.rows[series.rows.length - 1]?.[series.xKey]));
  const rows = first > last ? [...series.rows].reverse() : series.rows;
  const W = 320;
  const H = 88;
  const top = 14;
  const slot = W / rows.length;
  const barW = Math.max(2, Math.min(24, slot - 2));
  const label = (row: Row | undefined) => String(row?.[series.xKey] ?? "").replace("T", " ").replace(/:00(\.0+)?$/, "");
  const active = hover === null ? null : rows[hover];
  return (
    <div>
      <h4 className="mb-1 text-xs font-semibold uppercase tracking-wide text-slate-500">
        {series.label} <span className="font-normal normal-case">· {rows.length} buckets by {humanize(series.xKey)}</span>
      </h4>
      <div className="grid gap-3 sm:grid-cols-2">
        {series.metrics.map((metric) => {
          const values = rows.map((r) => (typeof r[metric] === "number" ? (r[metric] as number) : null));
          const max = Math.max(0, ...values.map((v) => v ?? 0));
          const peak = values.indexOf(max);
          return (
            <figure key={metric} className="rounded-lg bg-slate-50 p-2 dark:bg-slate-800/50">
              <figcaption className="flex items-baseline justify-between gap-2 text-[11px] text-slate-600 dark:text-slate-300">
                <span className="truncate font-medium">{humanize(metric)}</span>
                <span className="shrink-0 font-mono text-slate-500 tabular-nums">max {compact.format(max)}</span>
              </figcaption>
              <svg
                viewBox={`0 0 ${W} ${H}`}
                className="mt-1 block h-24 w-full"
                preserveAspectRatio="none"
                role="img"
                aria-label={`${humanize(metric)} per ${humanize(series.xKey)}, peak ${compact.format(max)} at ${label(rows[peak])}`}
                onPointerLeave={() => setHover(null)}
              >
                <line x1="0" x2={W} y1={H - 0.5} y2={H - 0.5} className="stroke-slate-200 dark:stroke-slate-700" strokeWidth="1" vectorEffect="non-scaling-stroke" />
                {values.map((v, i) => {
                  const x = i * slot + (slot - barW) / 2;
                  const h = v === null || max === 0 ? 0 : Math.max(1, (v / max) * (H - top));
                  const r = Math.min(4, barW / 2, h);
                  const y = H - h;
                  return (
                    <g key={i}>
                      {h > 0 && (
                        <path
                          d={`M${x},${H} V${y + r} Q${x},${y} ${x + r},${y} H${x + barW - r} Q${x + barW},${y} ${x + barW},${y + r} V${H} Z`}
                          className={hover === null || hover === i ? "fill-ruby-600 dark:fill-ruby-500" : "fill-ruby-600/35 dark:fill-ruby-500/35"}
                        />
                      )}
                      <rect x={i * slot} y={0} width={slot} height={H} fill="transparent" onPointerEnter={() => setHover(i)} />
                    </g>
                  );
                })}
              </svg>
              <div className="mt-0.5 flex justify-between font-mono text-[10px] text-slate-400">
                <span>{label(rows[0])}</span>
                <span>{label(rows[rows.length - 1])}</span>
              </div>
            </figure>
          );
        })}
      </div>
      <p className="mt-1 min-h-4 font-mono text-[11px] text-slate-600 dark:text-slate-400" aria-live="polite">
        {active
          ? `${label(active)} · ${series.metrics.map((m) => `${humanize(m)} ${active[m] === null ? "–" : formatNumber(active[m])}`).join(" · ")}`
          : "Hover a column to compare all metrics for that bucket."}
      </p>
    </div>
  );
}

function formatNumber(value: unknown): string {
  return typeof value === "number" ? value.toLocaleString("en-US", { maximumFractionDigits: 3 }) : String(value);
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
