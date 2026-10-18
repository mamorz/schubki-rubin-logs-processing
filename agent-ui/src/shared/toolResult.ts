// Shape-based description of any MCP tool result, so new tools and fields render without new code.
//
// Conventions recognised anywhere in a JSON result (all optional):
//   executed_sql: {name: sql} with sibling parameters: {name: {...}}  → SQL blocks
//   truncated: boolean                                               → truncation badge
//   arrays of flat objects                                            → tables (columns discovered)
//   arrays of {label..., <something>count}                           → bar lists
//   an object next to before_* / after_* arrays                      → one timeline table, object highlighted
//   *warning / *note / *caution strings                              → notes

export type Row = Record<string, unknown>;

export interface SqlQuery {
  name: string;
  sql: string;
  parameters: Row;
}

export interface TableView {
  key: string;
  label: string;
  columns: string[];
  rows: Row[];
  highlightRow: number | null;
}

export interface BarsView {
  key: string;
  label: string;
  countKey: string;
  labelKeys: string[];
  rows: Row[];
}

export interface ResultModel {
  stats: { key: string; label: string; value: string }[];
  tables: TableView[];
  bars: BarsView[];
  sql: SqlQuery[];
  notes: { label: string; text: string }[];
  details: { label: string; values: Row }[];
  truncated: boolean | null;
  headline: string;
}

const isObject = (value: unknown): value is Row =>
  typeof value === "object" && value !== null && !Array.isArray(value);

const isScalar = (value: unknown) => value === null || ["string", "number", "boolean"].includes(typeof value);

export const humanize = (key: string) => key.replace(/_/g, " ").replace(/\s+/g, " ").trim();

const NOTE_KEY = /warning|caution|note|statement/i;
const COUNT_KEY = /(^|_)count$|^count|^total/i;
const SQL_TEXT = /^\s*(SELECT|WITH)\b/i;

function isRowArray(value: unknown): value is Row[] {
  return Array.isArray(value) && value.length > 0 && value.every((row) => isObject(row) && Object.values(row).every((v) => isScalar(v)));
}

function columnsOf(rows: Row[]): string[] {
  const columns: string[] = [];
  for (const row of rows) for (const key of Object.keys(row)) if (!columns.includes(key)) columns.push(key);
  // Drop columns that are empty in every row (e.g. unavailable event IDs).
  return columns.filter((column) => rows.some((row) => row[column] !== null && row[column] !== undefined && row[column] !== ""));
}

function asBars(key: string, rows: Row[]): BarsView | null {
  const columns = columnsOf(rows);
  const numeric = columns.filter((c) => rows.every((r) => typeof r[c] === "number" || r[c] === null));
  const countKey = numeric.find((c) => COUNT_KEY.test(c));
  const labelKeys = columns.filter((c) => c !== countKey);
  if (!countKey || labelKeys.length === 0 || labelKeys.length > 3 || columns.includes("message")) return null;
  return { key, label: humanize(key), countKey, labelKeys, rows };
}

function formatScalar(value: unknown): string {
  if (typeof value === "number") return value.toLocaleString("en-US", { maximumFractionDigits: 3 });
  return String(value);
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
    for (const [name, sql] of Object.entries(executed)) {
      if (typeof sql === "string") out.push({ name, sql, parameters: isObject(parameters[name]) ? parameters[name] : {} });
    }
  }
  for (const [key, child] of Object.entries(value)) {
    if (key === "executed_sql" || (key === "parameters" && executed !== undefined)) continue;
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
    else if (value !== null && value !== undefined) out[name] = Array.isArray(value) ? value.map(String).join(", ") : value;
  }
  return out;
}

export function describeResult(output: unknown): ResultModel | null {
  if (!isObject(output)) return null;
  const sql: SqlQuery[] = [];
  const root = extractSql(output, "", sql) as Row;
  const model: ResultModel = { stats: [], tables: [], bars: [], sql, notes: [], details: [], truncated: null, headline: "" };

  // Timeline convention: an anchor object surrounded by before_* / after_* row arrays.
  const beforeKey = Object.keys(root).find((k) => /^before/.test(k) && Array.isArray(root[k]));
  const afterKey = Object.keys(root).find((k) => /^after/.test(k) && Array.isArray(root[k]));
  const rowObjects = Object.keys(root).filter((k) => isObject(root[k]) && Object.values(root[k] as Row).every(isScalar));
  const anchorKey = beforeKey && afterKey ? (rowObjects.find((k) => /anchor|target|center/i.test(k)) ?? rowObjects[0]) : undefined;
  const consumed = new Set<string>();
  if (beforeKey && afterKey && anchorKey) {
    const before = root[beforeKey] as Row[];
    const after = root[afterKey] as Row[];
    const rows = [...before, root[anchorKey] as Row, ...after];
    model.tables.push({ key: "timeline", label: `timeline around ${humanize(anchorKey)}`, columns: columnsOf(rows), rows, highlightRow: before.length });
    consumed.add(beforeKey).add(afterKey).add(anchorKey);
  }

  for (const [key, value] of Object.entries(root)) {
    if (consumed.has(key)) continue;
    if (key === "truncated" && typeof value === "boolean") {
      model.truncated = value;
    } else if (typeof value === "string" && (NOTE_KEY.test(key) || value.length > 80)) {
      model.notes.push({ label: humanize(key), text: value });
    } else if (isScalar(value)) {
      if (value !== null && value !== "") model.stats.push({ key, label: humanize(key), value: formatScalar(value) });
    } else if (isRowArray(value)) {
      const bars = asBars(key, value);
      if (bars) model.bars.push(bars);
      else model.tables.push({ key, label: humanize(key), columns: columnsOf(value), rows: value, highlightRow: null });
    } else if (Array.isArray(value)) {
      if (value.length === 0) model.stats.push({ key, label: humanize(key), value: "none" });
      else if (value.every(isScalar)) model.stats.push({ key, label: humanize(key), value: value.map(formatScalar).join(", ") });
      else model.details.push({ label: humanize(key), values: { value: JSON.stringify(value) } });
    } else if (isObject(value)) {
      const flat = flatten(value);
      for (const [name, text] of Object.entries(flat)) {
        if (typeof text === "string" && NOTE_KEY.test(name.split(".").pop() ?? "")) {
          model.notes.push({ label: humanize(name), text });
          delete flat[name];
        }
      }
      if (Object.keys(flat).length > 0) model.details.push({ label: humanize(key), values: flat });
    }
  }

  model.headline = headline(model);
  return model;
}

function headline(model: ResultModel): string {
  const counts = model.stats.filter((s) => COUNT_KEY.test(s.key)).slice(0, 2).map((s) => `${s.value} ${s.label}`);
  const parts = counts.length > 0
    ? counts
    : [...model.tables.map((t) => `${t.rows.length} ${t.label}`), ...model.bars.map((b) => `${b.rows.length} ${b.label}`)];
  const status = model.stats.find((s) => s.key === "status");
  if (status && status.value !== "ok") parts.unshift(status.value);
  const text = parts.join(" · ") || `${model.sql.length} quer${model.sql.length === 1 ? "y" : "ies"}`;
  return model.truncated ? `${text} (truncated)` : text;
}
