// Contract audit: real responses of all five MCP tools (feature/advanced-log-analytics) against the
// generic card model. Each block maps to an item of the MCP/UI compatibility assessment.
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { describeResult, type ResultModel } from "../src/shared/toolResult.js";

function fixture(name: string): { is_error: boolean; text: string } {
  return JSON.parse(readFileSync(new URL(`./fixtures/mcp/${name}.json`, import.meta.url), "utf8"));
}

function model(name: string): ResultModel {
  return describeResult(JSON.parse(fixture(name).text))!;
}

describe("1. nested truncation", () => {
  it("flags truncation from ranking and time series sections", () => {
    const m = model("perf_truncated");
    expect(m.truncated).toBe(true);
    expect(new Set(m.truncatedSections)).toEqual(new Set(["ranking", "time series"]));
    expect(m.headline).toContain("(truncated: ranking, time series)");
  });

  it("names only the truncated section (default call: top 10 of 2,066 ranked events)", () => {
    const m = model("perf_default");
    expect(m.truncated).toBe(true);
    expect(m.truncatedSections).toEqual(["ranking"]);
  });

  it("reports complete when every flag is false", () => {
    expect(model("summarize_time_day").truncated).toBe(false);
  });
});

describe("2. nested statistics", () => {
  it("turns counts, statistics, baseline, detection and ranking into stat groups", () => {
    const m = model("perf_default");
    expect(m.statGroups.map((g) => g.key)).toEqual(expect.arrayContaining(["counts", "statistics", "baseline", "detection", "ranking"]));
    const statistics = m.statGroups.find((g) => g.key === "statistics")!;
    expect(statistics.stats.map((s) => s.key)).toEqual(expect.arrayContaining(["median_seconds", "p95_seconds"]));
    expect(m.headline).toMatch(/spawn event count/);
    expect(m.headline).toMatch(/slow event count/);
  });

  it("keeps inputs and scope as details, not metrics", () => {
    const m = model("perf_default");
    expect(m.statGroups.map((g) => g.key)).not.toContain("scope");
    expect(m.details.map((d) => d.label)).toEqual(expect.arrayContaining(["scope"]));
  });
});

describe("3. SQL provenance", () => {
  it("pairs every tool's statements with their parameters", () => {
    for (const name of ["perf_default", "search_openstack_by_instance", "summarize_time_day", "summarize_team_query", "nearby_openstack"]) {
      const m = model(name);
      expect(m.sql.length, name).toBeGreaterThan(0);
      expect(m.sql.every((q) => /^\s*(WITH|SELECT)/i.test(q.sql)), name).toBe(true);
    }
    expect(model("perf_default").sql[0]!.parameters).toMatchObject({ spawn_component: "nova.compute.manager" });
  });

  it("applies unkeyed parameters to every statement", () => {
    const m = describeResult({ provenance: { executed_sql: { a: "SELECT 1", b: "SELECT 2" }, parameters: { threshold: 25 } } })!;
    expect(m.sql.map((q) => q.parameters)).toEqual([{ threshold: 25 }, { threshold: 25 }]);
  });
});

describe("4. multi-metric time series", () => {
  it("renders nested buckets as small multiples plus a table, prioritising key metrics", () => {
    const m = model("perf_default");
    const series = m.series[0]!;
    expect(series.xKey).toBe("bucket_start");
    expect(series.metrics).toHaveLength(4);
    expect(series.metrics.slice(0, 2)).toEqual(["spawn_event_count", "slow_event_count"]);
    expect(m.tables.some((t) => t.label === series.label)).toBe(true);
  });

  it("skips metrics that are zero in every bucket (no slow spawns in the hourly sample)", () => {
    const series = model("perf_truncated").series[0]!;
    expect(series.metrics).not.toContain("slow_event_count");
    expect(series.metrics[0]).toBe("spawn_event_count");
  });

  it("turns summarize_logs time buckets into a series", () => {
    const m = model("summarize_time_day");
    expect(m.series[0]).toMatchObject({ xKey: "bucket_start", metrics: ["record_count"] });
  });

  it("keeps grouped team counts as a bar list", () => {
    expect(model("summarize_team_query").bars[0]).toMatchObject({ countKey: "record_count", labelKeys: ["team"] });
  });
});

describe("5. chronological event arrays", () => {
  it("marks time-ordered search records as a timeline", () => {
    const records = model("search_openstack_by_instance").tables.find((t) => t.key.endsWith("records"))!;
    expect(records.chronological).toBe(true);
  });

  it("does not treat duration-ranked events as a timeline", () => {
    const slowest = model("perf_default").tables.find((t) => t.key.endsWith("slowest_spawn_events"))!;
    expect(slowest.chronological).toBe(false);
  });

  it("still merges the nearby anchor timeline", () => {
    const timeline = model("nearby_openstack").tables.find((t) => t.key === "timeline")!;
    expect(timeline.highlightRow).toBe(3);
    expect(timeline.chronological).toBe(true);
  });
});

describe("7. unavailable columns", () => {
  it("lists all-empty columns instead of silently dropping them", () => {
    const records = model("search_openstack_by_instance").tables.find((t) => t.key.endsWith("records"))!;
    expect(records.unavailable).toEqual(expect.arrayContaining(["latency_s", "http_status"]));
    expect(records.columns).not.toContain("latency_s");
  });
});

describe("8. errors", () => {
  it("real tool errors arrive as MCP errors (plain text), not JSON data", () => {
    for (const name of ["perf_error", "search_error"]) {
      const f = fixture(name);
      expect(f.is_error).toBe(true);
      expect(f.text).toMatch(/^Error executing tool/);
    }
  });

  it("recognises structured error objects returned as data", () => {
    expect(describeResult({ error: "Invalid time range" })).toMatchObject({ error: "Invalid time range", headline: "error: Invalid time range" });
    expect(describeResult({ status: "failed", message: "timeout" })!.error).toBe("timeout");
  });

  it("does not treat warning statuses as errors", () => {
    expect(describeResult({ status: "unavailable_baseline", warnings: ["low support"] })).toMatchObject({ error: null });
  });
});

describe("notes", () => {
  it("shows warnings and limitations lists as notes", () => {
    const labels = model("perf_default").notes.map((n) => n.label);
    expect(labels).toEqual(expect.arrayContaining(["warning", "limitation"]));
  });
});
