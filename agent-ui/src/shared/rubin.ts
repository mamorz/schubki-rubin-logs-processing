// Parsers for rubin-log-insights tool results (see log_insights/tools.py serializers).
import { z } from "zod";

const sqlMap = z.record(z.string(), z.string());
const paramMap = z.record(z.string(), z.unknown());
const row = z.record(z.string(), z.unknown());

export const searchResult = z.looseObject({
  match_mode: z.string().optional(),
  records: z.array(row),
  returned_record_count: z.number(),
  total_corpus_match_count: z.number(),
  truncated: z.boolean(),
  provenance: z.looseObject({
    dataset: z.string(),
    applied_filters: paramMap,
    executed_sql: sqlMap,
    parameters: z.record(z.string(), paramMap),
  }),
});

export const aggregationResult = z.looseObject({
  dataset: z.string(),
  aggregation_type: z.string(),
  group_by: z.string().nullable(),
  interval: z.string().nullable(),
  applied_filters: paramMap,
  groups: z.array(row),
  returned_group_count: z.number(),
  total_matching_record_count: z.number(),
  group_limit: z.number(),
  truncated: z.boolean(),
  scope: z.looseObject({ source: z.string(), table: z.string(), count_unit: z.string() }),
  mapping_coverage: paramMap.optional(),
  provenance: z.looseObject({
    executed_sql: sqlMap,
    parameters: z.record(z.string(), paramMap),
    fictional_ownership: paramMap.optional(),
  }),
});

export const nearbyResult = z.looseObject({
  status: z.string(),
  dataset: z.string(),
  anchor: row.nullable(),
  before_events: z.array(row),
  after_events: z.array(row),
  counts: z.looseObject({
    requested_before: z.number(),
    returned_before: z.number(),
    requested_after: z.number(),
    returned_after: z.number(),
  }),
  causation_warning: z.string(),
  provenance: z.looseObject({
    executed_sql: sqlMap,
    parameters: z.record(z.string(), paramMap),
  }),
});

export type SearchResult = z.infer<typeof searchResult>;
export type AggregationResult = z.infer<typeof aggregationResult>;
export type NearbyResult = z.infer<typeof nearbyResult>;

export type RubinResult =
  | { kind: "search"; data: SearchResult }
  | { kind: "aggregation"; data: AggregationResult }
  | { kind: "nearby"; data: NearbyResult }
  | { kind: "unknown"; data: unknown };

export function classifyRubinResult(tool: string | null, output: unknown): RubinResult {
  const candidates = [
    { kind: "search", schema: searchResult, tool: "search_logs" },
    { kind: "aggregation", schema: aggregationResult, tool: "summarize_logs" },
    { kind: "nearby", schema: nearbyResult, tool: "get_nearby_events" },
  ] as const;
  for (const candidate of candidates) {
    if (tool !== null && tool !== candidate.tool) continue;
    const parsed = candidate.schema.safeParse(output);
    if (parsed.success) return { kind: candidate.kind, data: parsed.data } as RubinResult;
  }
  return { kind: "unknown", data: output };
}

/** Every executed SQL statement with its bound parameters, keyed by purpose. */
export function executedQueries(result: RubinResult): { name: string; sql: string; parameters: Record<string, unknown> }[] {
  if (result.kind === "unknown") return [];
  const { executed_sql, parameters } = result.data.provenance;
  return Object.entries(executed_sql).map(([name, sql]) => ({
    name,
    sql,
    parameters: parameters[name] ?? {},
  }));
}

/** One-line measured summary used by cards and the evidence ledger. */
export function resultHeadline(result: RubinResult): string {
  switch (result.kind) {
    case "search": {
      const d = result.data;
      return `${d.returned_record_count} of ${d.total_corpus_match_count.toLocaleString("en-US")} matching records returned${d.truncated ? " (truncated)" : ""}`;
    }
    case "aggregation": {
      const d = result.data;
      return `${d.returned_group_count} groups over ${d.total_matching_record_count.toLocaleString("en-US")} records${d.truncated ? " (truncated)" : ""}`;
    }
    case "nearby": {
      const c = result.data.counts;
      return result.data.status === "ok"
        ? `${c.returned_before} before / ${c.returned_after} after the anchor`
        : "anchor record not found";
    }
    default:
      return "unrecognised result";
  }
}
