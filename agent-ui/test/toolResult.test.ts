import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { extractOutput } from "../src/server/toolCalls.js";
import { describeResult } from "../src/shared/toolResult.js";

type Update = { sessionUpdate?: string; rawOutput?: unknown; _meta?: { kiro?: { toolName?: string } }; toolCallId?: string };

// Real rubin results recorded from Kiro, keyed by tool name.
const outputs = new Map<string, unknown>();
const names = new Map<string, string>();
for (const line of readFileSync(new URL("./fixtures/kiro-acp-session.ndjson", import.meta.url), "utf8").split("\n").filter(Boolean)) {
  const update = (JSON.parse(line) as { params?: { update?: Update } }).params?.update;
  if (update?.sessionUpdate === "tool_call" && update.toolCallId) names.set(update.toolCallId, update._meta?.kiro?.toolName ?? "");
  if (update?.sessionUpdate === "tool_call_update" && update.rawOutput && update.toolCallId) {
    outputs.set(names.get(update.toolCallId)!, extractOutput(update.rawOutput).json);
  }
}

describe("generic tool result model", () => {
  it("renders search results as a discovered table with SQL and truncation", () => {
    const model = describeResult(outputs.get("search_logs"))!;
    expect(model.truncated).toBe(true);
    expect(model.tables[0]!.columns).toEqual(["record_id", "timestamp", "component", "severity", "message"]); // all-null event_id dropped
    expect(model.sql.map((q) => q.name)).toEqual(["records", "total_count"]);
    expect(model.sql[0]!.parameters).toMatchObject({ text: "kernel panic" });
    expect(model.headline).toContain("54,518");
  });

  it("renders grouped counts as bars with all executed queries", () => {
    const model = describeResult(outputs.get("summarize_logs"))!;
    expect(model.bars[0]).toMatchObject({ countKey: "record_count", labelKeys: ["team"] });
    expect(model.sql.map((q) => q.name)).toEqual(["aggregation", "total_count", "mapping_coverage"]);
    expect(model.truncated).toBe(false);
  });

  it("merges anchor and before/after events into one highlighted timeline", () => {
    const model = describeResult(outputs.get("get_nearby_events"))!;
    const timeline = model.tables.find((t) => t.key === "timeline")!;
    expect(timeline.rows).toHaveLength(5);
    expect(timeline.highlightRow).toBe(2);
    expect(model.notes.some((n) => n.label === "causation warning")).toBe(true);
  });

  it("handles a tool it has never seen", () => {
    const model = describeResult({
      latency_percentiles: [{ service: "nova-api", p50: 0.2, p95: 1.4 }],
      error_rate: 0.031,
      truncated: false,
      provenance: { executed_sql: { latency: "SELECT service, percentile_cont(0.5) ..." }, parameters: { latency: { dataset: "openstack" } } },
    })!;
    expect(model.tables[0]!.columns).toEqual(["service", "p50", "p95"]);
    expect(model.stats).toContainEqual({ key: "error_rate", label: "error rate", value: "0.031" });
    expect(model.sql[0]).toMatchObject({ name: "latency", parameters: { dataset: "openstack" } });
  });

  it("ignores non-object results", () => {
    expect(describeResult("plain text")).toBeNull();
  });
});
