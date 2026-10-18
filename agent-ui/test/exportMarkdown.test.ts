import { describe, expect, it } from "vitest";
import type { ToolCallView } from "../src/shared/events.js";
import { reportFilename, turnToMarkdown } from "../src/web/exportMarkdown.js";
import type { Turn } from "../src/web/store.js";

const call: ToolCallView = {
  id: "c1",
  title: "Running: @rubin-log-insights/search_logs",
  server: "rubin-log-insights",
  tool: "search_logs",
  status: "completed",
  input: { dataset: "bgl", query: "kernel panic" },
  output: {
    records: [{ record_id: 1, message: "rts panic! | stop", severity: "FATAL" }],
    returned_record_count: 1,
    total_corpus_match_count: 54518,
    truncated: true,
    provenance: { executed_sql: { records: "SELECT 1" }, parameters: { records: { text: "kernel panic" } } },
  },
  outputText: null,
};

const turn: Turn = {
  id: "t1",
  role: "engineer",
  question: "What failed in BGL?",
  items: [
    { kind: "thought", text: "Search first." },
    { kind: "text", text: "Let me look." },
    { kind: "tool", id: "c1" },
    { kind: "text", text: "**Observed facts:** kernel panics." },
  ],
  text: "Let me look.**Observed facts:** kernel panics.",
  toolCallIds: ["c1"],
  done: true,
  stopReason: "end_turn",
  credits: 0.05,
  durationMs: 4200,
  error: null,
  startedAt: Date.UTC(2026, 9, 18, 12),
};

describe("per-request report", () => {
  const md = turnToMarkdown(turn, { c1: call });

  it("keeps the workflow order and puts only the last text under Answer", () => {
    const order = ["> Search first.", "Let me look.", "### 1. Tool call `search_logs`", "## Answer", "**Observed facts:** kernel panics."].map((s) => md.indexOf(s));
    expect(order.every((pos) => pos >= 0)).toBe(true);
    expect([...order].sort((a, b) => a - b)).toEqual(order);
  });

  it("includes results as tables, truncation and SQL with parameters", () => {
    expect(md).toContain("**Result:** 54,518 total corpus match count (truncated)");
    expect(md).toContain("| 1 | rts panic! \\| stop | FATAL |");
    expect(md).toContain("```sql\nSELECT 1\n```");
    expect(md).toContain('parameters `{"text":"kernel panic"}`');
  });

  it("names the file after role, date and question", () => {
    expect(reportFilename(turn)).toBe("rubin-report-engineer-2026-10-18-what-failed-in-bgl.md");
  });
});
