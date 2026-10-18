// Shape-based description of any MCP tool result, so new tools and fields render without new code.
//
// Conventions recognised anywhere in a JSON result (all optional):
//   executed_sql: {name: sql} (or a string) with sibling parameters  → SQL blocks
//   truncated / *_truncated booleans, at any depth                  → truncation badge (+ which sections)
//   error / status: "error" | "failed"                              → error card
//   arrays of row objects                                            → tables (columns discovered;
//                                                                      nested lists per row expandable)
//   ... with a time-bucket column and numeric metrics                → small-multiple time series
//   ... with a label and one *count column                           → bar list
//   ... ordered by timestamp                                         → chronological timeline
//   an object next to before_* / after_* arrays                     → one timeline table, object highlighted
//   nested objects of mostly numbers (counts, statistics, …)         → labelled stat groups
//   *warning / *note / *caution strings, warnings/limitations lists  → notes

export type Row = Record<string, unknown>;

export interface SqlQuery {
  name: string;
  sql: string;
  parameters: Row;
}

export interface Stat {
  key: string;
  label: string;
  value: string;
}

export interface TableView {
  key: string;
  label: string;
  columns: string[];
  /** Columns that are empty in every row; hidden but listed so missing data stays visible. */
  unavailable: string[];
  /** Fields holding nested lists/objects per row (e.g. contributors); shown as expandable row details. */
  nested: string[];
  rows: Row[];
  highlightRow: number | null;
  /** Rows are in time order: render as a timeline with gaps between events. */
  chronological: boolean;
}

export interface BarsView {
  key: string;
  label: string;
  countKey: string;
  labelKeys: string[];
  rows: Row[];
}

export interface SeriesView {
  key: string;
  label: string;
  xKey: string;
  /** Up to four metrics, each drawn as its own small chart with its own scale. */
  metrics: string[];
  rows: Row[];
}

export interface ResultModel {
  stats: Stat[];
  statGroups: { key: string; label: string; stats: Stat[] }[];
  series: SeriesView[];
  tables: TableView[];
  bars: BarsView[];
  sql: SqlQuery[];
  notes: { label: string; text: string }[];
  details: { label: string; values: Row }[];
  truncated: boolean | null;
  truncatedSections: string[];
  error: string | null;
  headline: string;
}

const isObject = (value: unknown): value is Row =>
  typeof value === "object" && value !== null && !Array.isArray(value);

const isScalar = (value: unknown) => value === null || ["string", "number", "boolean"].includes(typeof value);

export const humanize = (key: string) => key.replace(/[_.]/g, " ").replace(/\s+/g, " ").trim();

const NOTE_KEY = /warning|caution|note|statement|limitation|caveat/i;
const NOTE_LIST_KEY = /^(warnings?|limitations?|notes?|caveats?)$/i;
const COUNT_KEY = /(^|_)count$|^count|^total/i;
const SECONDARY_COUNT = /invalid|missing|unique|candidate|requested|limit/i;
const SQL_TEXT = /^\s*(SELECT|WITH)\b/i;
const SQL_KEY = /(^|_)sql$/i;
const TRUNCATED_KEY = /(^|_)truncated$/i;
/** A time-bucket column; `observed_start`-style fields describe a row's span, not a bucket. */
const BUCKET_KEY = /^bucket|^period$|^hour$|^day$|^date$|^week$|^month$|^time$/i;
const TIME_LABEL = /time|date|_start$|_end$|^bucket|^period$/i;
const TIMESTAMP_KEY = /timestamp|^ts$|_at$/i;
const ID_KEY = /(^|_)id$/i;
/** Objects describing inputs or method, never headline metrics. */
const CONTEXT_KEY = /filter|parameter|provenance|scope|semantics|ordering|method|coverage_sql/i;

/** Rows are objects with plain values; nested lists/objects inside a row are allowed (rendered per row). */
function isRowArray(value: unknown): value is Row[] {
  return (
    Array.isArray(value) &&
    value.length > 0 &&
    value.every((row) => isObject(row) && Object.values(row).some((v) => isScalar(v) && v !== null))
  );
}

function columnsOf(rows: Row[]): { columns: string[]; unavailable: string[]; nested: string[] } {
  const all: string[] = [];
  for (const row of rows) for (const key of Object.keys(row)) if (!all.includes(key)) all.push(key);
  const nested = all.filter((c) => rows.some((row) => !isScalar(row[c]) && row[c] !== undefined));
  const scalar = all.filter((c) => !nested.includes(c));
  const present = (column: string) => rows.some((row) => row[column] !== null && row[column] !== undefined && row[column] !== "");
  return { columns: scalar.filter(present), unavailable: scalar.filter((c) => !present(c)), nested };
}

function formatScalar(value: unknown): string {
  if (typeof value === "number") return value.toLocaleString("en-US", { maximumFractionDigits: 3 });
  if (typeof value === "boolean") return value ? "yes" : "no";
  return String(value);
}

function stat(key: string, value: unknown): Stat {
  return { key, label: humanize(key), value: formatScalar(value) };
}

/** Collects executed_sql blocks (with matching parameters) and stray SQL strings; returns the object without them. */
function extractSql(value: unknown, path: string, out: SqlQuery[]): unknown {
  if (Array.isArray(value)) return value.map((item, i) => extractSql(item, `${path}[${i}]`, out));
  if (!isObject(value)) {
    if (typeof value === "string" && SQL_TEXT.test(value)) {
      out.push({ name: path || "query", sql: value, parameters: {} });
      return undefined;
    }
    return value;
  }
  const rest: Row = {};
  const executed = value.executed_sql;
  const parameters = isObject(value.parameters) ? value.parameters : {};
  if (typeof executed === "string") {
    out.push({ name: path || "query", sql: executed, parameters });
  } else if (isObject(executed)) {
    const names = Object.keys(executed);
    // Parameters are normally keyed like the statements; otherwise they apply to every statement.
    const keyed = names.some((name) => isObject(parameters[name]));
    for (const [name, sql] of Object.entries(executed)) {
      if (typeof sql !== "string") continue;
      const own = keyed ? parameters[name] : parameters;
      out.push({ name, sql, parameters: isObject(own) ? own : {} });
    }
  }
  for (const [key, child] of Object.entries(value)) {
    if (key === "executed_sql" || (key === "parameters" && executed !== undefined)) continue;
    if (SQL_KEY.test(key) && typeof child === "string" && SQL_TEXT.test(child)) {
      out.push({ name: key, sql: child, parameters: {} });
      continue;
    }
    const cleaned = extractSql(child, path ? `${path}.${key}` : key, out);
    if (cleaned !== undefined) rest[key] = cleaned;
  }
  return rest;
}

function flatten(values: Row, prefix = ""): Row {
  const out: Row = {};
  for (const [key, value] of Object.entries(values)) {
    const name = prefix ? `${prefix}.${key}` : key;
    if (isObject(value)) Object.assign(out, flatten(value, name));
    else if (Array.isArray(value)) {
      if (value.every(isScalar)) out[name] = value.map(String).join(", ");
    } else if (value !== null && value !== undefined) out[name] = value;
  }
  return out;
}

function metricPriority(column: string): number {
  if (COUNT_KEY.test(column) && !SECONDARY_COUNT.test(column)) return 0;
  if (/p95|median|mean|avg|percentage|percent|rate|ratio/i.test(column)) return 1;
  if (COUNT_KEY.test(column)) return 3;
  return 2;
}

function asSeries(key: string, label: string, rows: Row[]): SeriesView | null {
  if (rows.length < 2) return null;
  const { columns } = columnsOf(rows);
  const xKey = columns.find((c) => BUCKET_KEY.test(c) && rows.every((r) => typeof r[c] === "string"));
  if (!xKey) return null;
  // Buckets run in one direction (oldest or newest first); anything else is not a time series.
  const times = rows.map((r) => Date.parse(String(r[xKey])));
  const ascending = times.every((t, i) => !Number.isNaN(t) && (i === 0 || t > times[i - 1]!));
  const descending = times.every((t, i) => !Number.isNaN(t) && (i === 0 || t < times[i - 1]!));
  if (!ascending && !descending) return null;
  const numeric = columns.filter(
    (c) => c !== xKey && !ID_KEY.test(c) && rows.every((r) => r[c] === null || typeof r[c] === "number") && rows.some((r) => r[c] !== 0),
  );
  if (numeric.length === 0) return null;
  const metrics = numeric
    .map((c, i) => ({ c, score: metricPriority(c) * 100 + i }))
    .sort((a, b) => a.score - b.score)
    .slice(0, 4)
    .map((m) => m.c);
  return { key, label, xKey, metrics, rows };
}

function asBars(key: string, label: string, rows: Row[]): BarsView | null {
  const { columns, nested } = columnsOf(rows);
  if (nested.length > 0) return null;
  const numeric = columns.filter((c) => rows.every((r) => typeof r[c] === "number" || r[c] === null));
  const countKey = numeric.find((c) => COUNT_KEY.test(c));
  const labelKeys = columns.filter((c) => c !== countKey);
  if (!countKey || numeric.length > 1 || labelKeys.length === 0 || labelKeys.length > 3 || columns.includes("message")) return null;
  if (labelKeys.some((c) => TIME_LABEL.test(c))) return null; // time ranges read better as a table
  return { key, label, countKey, labelKeys, rows };
}

function isChronological(rows: Row[], columns: string[]): boolean {
  const timeKey = columns.find((c) => TIMESTAMP_KEY.test(c));
  if (!timeKey || rows.length < 2) return false;
  const times = rows.map((r) => Date.parse(String(r[timeKey])));
  return times.every((t, i) => !Number.isNaN(t) && (i === 0 || t >= times[i - 1]!));
}

function table(key: string, label: string, rows: Row[], highlightRow: number | null = null): TableView {
  const { columns, unavailable, nested } = columnsOf(rows);
  return { key, label, columns, unavailable, nested, rows, highlightRow, chronological: isChronological(rows, columns) };
}

/** Per-row `*_truncated` flags (e.g. patterns_truncated) named after the table and the flag. */
function rowTruncation(label: string, rows: Row[]): string[] {
  const flags = new Set<string>();
  for (const row of rows) {
    for (const [key, value] of Object.entries(row)) {
      if (value === true && TRUNCATED_KEY.test(key)) flags.add(`${label} ${humanize(key.replace(TRUNCATED_KEY, ""))}`.trim());
    }
  }
  return [...flags];
}

function errorText(value: unknown): string | null {
  if (typeof value === "string" && value.trim()) return value;
  if (isObject(value)) return String(value.message ?? value.detail ?? JSON.stringify(value));
  return null;
}

export function describeResult(output: unknown): ResultModel | null {
  if (!isObject(output)) return null;
  const sql: SqlQuery[] = [];
  const root = extractSql(output, "", sql) as Row;
  const model: ResultModel = {
    stats: [], statGroups: [], series: [], tables: [], bars: [], sql, notes: [], details: [],
    truncated: null, truncatedSections: [], error: null, headline: "",
  };

  model.error = errorText(root.error);
  if (!model.error && typeof root.status === "string" && /^(error|failed|failure)$/i.test(root.status)) {
    model.error = errorText(root.message) ?? root.status;
  }

  // Timeline convention: an anchor object surrounded by before_* / after_* row arrays.
  const beforeKey = Object.keys(root).find((k) => /^before/.test(k) && Array.isArray(root[k]));
  const afterKey = Object.keys(root).find((k) => /^after/.test(k) && Array.isArray(root[k]));
  const rowObjects = Object.keys(root).filter((k) => isObject(root[k]) && Object.values(root[k] as Row).every(isScalar));
  const anchorKey = beforeKey && afterKey ? (rowObjects.find((k) => /anchor|target|center/i.test(k)) ?? rowObjects[0]) : undefined;
  const consumed = new Set<string>(["error"]);
  if (beforeKey && afterKey && anchorKey) {
    const before = root[beforeKey] as Row[];
    const after = root[afterKey] as Row[];
    model.tables.push(table("timeline", `timeline around ${humanize(anchorKey)}`, [...before, root[anchorKey] as Row, ...after], before.length));
    consumed.add(beforeKey).add(afterKey).add(anchorKey);
  }

  visit(root, "", model, consumed);
  model.truncated = model.truncatedSections.length > 0 ? true : model.truncated;
  model.headline = headline(model);
  return model;
}

function visit(object: Row, parent: string, model: ResultModel, skip: Set<string> = new Set()): void {
  const groupStats: Stat[] = [];
  const nested = parent !== "";
  for (const [key, value] of Object.entries(object)) {
    if (skip.has(key)) continue;
    const label = nested ? `${humanize(parent)} · ${humanize(key)}` : humanize(key);
    if (TRUNCATED_KEY.test(key) && typeof value === "boolean") {
      const section = nested ? humanize(parent) : humanize(key.replace(TRUNCATED_KEY, ""));
      if (value && section) model.truncatedSections.push(section);
      else if (value) model.truncated = true; // plain top-level `truncated`
      else if (model.truncated === null) model.truncated = false;
    } else if (typeof value === "string" && (NOTE_KEY.test(key) || value.length > 80)) {
      model.notes.push({ label, text: value });
    } else if (isScalar(value)) {
      if (value !== null && value !== "") (nested ? groupStats : model.stats).push(stat(key, value));
    } else if (isRowArray(value)) {
      const name = nested ? parent : key;
      model.truncatedSections.push(...rowTruncation(label, value));
      const series = asSeries(key, label, value);
      if (series) {
        model.series.push(series);
        model.tables.push(table(`${name}.${key}`, label, value));
      } else {
        const bars = asBars(key, label, value);
        if (bars) model.bars.push(bars);
        else model.tables.push(table(`${name}.${key}`, label, value));
      }
    } else if (Array.isArray(value)) {
      if (value.length === 0) groupOrRoot(nested, groupStats, model).push({ key, label: humanize(key), value: "none" });
      else if (value.every((v) => typeof v === "string") && (NOTE_LIST_KEY.test(key) || value.some((v) => String(v).length > 80))) {
        for (const text of value as string[]) model.notes.push({ label: humanize(key).replace(/s$/, ""), text });
      } else if (value.every(isScalar)) groupOrRoot(nested, groupStats, model).push(stat(key, value.map(formatScalar).join(", ")));
      else model.details.push({ label, values: { value: JSON.stringify(value) } });
    } else if (isObject(value)) {
      const values = Object.values(value);
      const scalarsOnly = values.every(isScalar);
      const numbers = values.filter((v) => typeof v === "number").length;
      const filled = values.filter((v) => v !== null && v !== "").length;
      if (CONTEXT_KEY.test(key) || (scalarsOnly && numbers * 2 < filled)) {
        // Inputs, scope and method descriptions: key/value details, minus notes and truncation flags.
        const flat = flatten(value);
        for (const [name, text] of Object.entries(flat)) {
          const leaf = name.split(".").pop() ?? "";
          if (typeof text === "string" && NOTE_KEY.test(leaf)) {
            model.notes.push({ label: humanize(name), text });
            delete flat[name];
          } else if (TRUNCATED_KEY.test(leaf) && typeof text === "boolean") {
            if (text) model.truncatedSections.push(humanize(key));
            delete flat[name];
          }
        }
        if (Object.keys(flat).length > 0) model.details.push({ label, values: flat });
      } else {
        // Metrics (counts, statistics, baseline, detection) or a section holding rows: recurse.
        visit(value, nested ? `${parent}.${key}` : key, model);
      }
    }
  }
  if (!nested) return;
  // Numbers become tiles; text and flags in the same section (method, order, interval, …) are context.
  const numeric = groupStats.filter((st) => typeof object[st.key] === "number");
  const context = groupStats.filter((st) => typeof object[st.key] !== "number");
  if (numeric.length > 0) model.statGroups.push({ key: parent, label: humanize(parent), stats: numeric });
  if (context.length > 0) {
    model.details.push({ label: humanize(parent), values: Object.fromEntries(context.map((st) => [st.key, st.value])) });
  }
}

function groupOrRoot(nested: boolean, group: Stat[], model: ResultModel): Stat[] {
  return nested ? group : model.stats;
}

/** Counts that describe findings come before bookkeeping counts in the one-line summary. */
function headlinePriority(key: string): number {
  if (/spike|finding|window/i.test(key)) return 0;
  if (/slow|error|fail|anomal|fatal/i.test(key)) return 1;
  if (/elevated|match/i.test(key)) return 2;
  return 3;
}

function headline(model: ResultModel): string {
  if (model.error) return `error: ${model.error}`;
  const candidates = [...model.stats, ...model.statGroups.flatMap((g) => g.stats)].filter(
    (s) => COUNT_KEY.test(s.key) && !SECONDARY_COUNT.test(s.key) && !/returned/i.test(s.key),
  );
  const seen = new Set<string>();
  const counts = candidates
    .filter((s) => !seen.has(s.key) && seen.add(s.key))
    .map((s, i) => ({ s, rank: headlinePriority(s.key) * 1000 + i }))
    .sort((a, b) => a.rank - b.rank)
    .slice(0, 2)
    .map(({ s }) => `${s.value} ${s.label}`);
  const parts = counts.length > 0
    ? counts
    : [...model.tables.map((t) => `${t.rows.length} ${t.label}`), ...model.bars.map((b) => `${b.rows.length} ${b.label}`)];
  const status = model.stats.find((s) => s.key === "status");
  if (status && !/^(ok|ready)$/i.test(status.value)) parts.unshift(status.value);
  const text = parts.join(" · ") || `${model.sql.length} quer${model.sql.length === 1 ? "y" : "ies"}`;
  if (!model.truncated) return text;
  return `${text} (truncated${model.truncatedSections.length > 0 ? `: ${[...new Set(model.truncatedSections)].join(", ")}` : ""})`;
}
