import { describe, expect, it } from "vitest";
import type { RequestPermissionRequest } from "@agentclientprotocol/sdk";
import { parseDeviceLogin, parseWhoami } from "../src/server/auth.js";
import { mcpHealthUrl } from "../src/server/config.js";
import { decidePermission } from "../src/server/permissions.js";
import { extractOutput } from "../src/server/toolCalls.js";

const options: RequestPermissionRequest["options"] = [
  { optionId: "yes", name: "Allow", kind: "allow_once" },
  { optionId: "no", name: "Reject", kind: "reject_once" },
];

function request(title: string, server?: string): RequestPermissionRequest {
  return {
    sessionId: "s",
    options,
    toolCall: { toolCallId: "t", title, ...(server ? { _meta: { kiro: { mcpServerName: server } } } : {}) },
  };
}

describe("permission policy", () => {
  it("allows rubin tools", () => {
    expect(decidePermission(request("Running: @rubin-log-insights/search_logs"), "rubin-log-insights").outcome)
      .toEqual({ outcome: "selected", optionId: "yes" });
    expect(decidePermission(request("x", "rubin-log-insights"), "rubin-log-insights").outcome)
      .toEqual({ outcome: "selected", optionId: "yes" });
  });

  it("rejects shell, file and other servers", () => {
    for (const title of ["Running: shell", "Writing file /etc/passwd", "Running: @other/search_logs"]) {
      expect(decidePermission(request(title), "rubin-log-insights").outcome).toEqual({ outcome: "selected", optionId: "no" });
    }
  });
});

describe("kiro-cli output parsing", () => {
  it("parses device-flow output with spinner noise", () => {
    const out = "\nConfirm the following code in the browser\nCode: SKGQ-HDNQ\n\nOpen this URL: https://view.awsapps.com/start/#/device?user_code=SKGQ-HDNQ\n\x1b[?25l\r▰▱▱ Logging in...";
    expect(parseDeviceLogin(out)).toEqual({ code: "SKGQ-HDNQ", url: "https://view.awsapps.com/start/#/device?user_code=SKGQ-HDNQ" });
    expect(parseDeviceLogin("Logging in...")).toBeNull();
  });

  it("parses whoami", () => {
    expect(parseWhoami("Logged in with Builder ID\nEmail: someone@example.com\n")).toEqual({
      loggedIn: true, method: "Builder ID", account: "someone@example.com",
    });
    expect(parseWhoami("Not logged in")).toEqual({ loggedIn: false, method: null, account: null });
  });
});

describe("helpers", () => {
  it("unwraps Kiro MCP result envelopes", () => {
    expect(extractOutput({ items: [{ Json: { content: [{ type: "text", text: "{\"a\":1}" }] } }] })).toEqual({ json: { a: 1 }, text: null });
    expect(extractOutput({ content: [{ type: "text", text: "plain" }] })).toEqual({ json: null, text: "plain" });
  });

  it("derives the MCP health URL", () => {
    expect(mcpHealthUrl("http://mcp-server:8000/mcp")).toBe("http://mcp-server:8000/healthz");
  });
});

describe("tool call status", () => {
  it("marks MCP tool errors reported as completed by Kiro as failed", async () => {
    const { ToolCallTracker } = await import("../src/server/toolCalls.js");
    const tracker = new ToolCallTracker();
    const view = tracker.apply({
      toolCallId: "t1",
      title: "Running: @rubin-log-insights/detect_log_spikes",
      status: "completed",
      rawOutput: { items: [{ Json: { content: [{ type: "text", text: "Error executing tool detect_log_spikes: 1 validation error" }] } }] },
    });
    expect(view.status).toBe("failed");
    expect(view.outputText).toMatch(/^Error executing tool/);
  });
});

describe("pseudo tool-call markup in answers", () => {
  it("removes complete function_calls/function_results blocks and keeps the real text", async () => {
    const { stripToolMarkup } = await import("../src/shared/agentText.js");
    const leaked =
      'Intro.\n\n<function_results>Earliest hit: record 33937.</function_results>\n\n' +
      '<function_calls> <invoke name="get_nearby_events"> <parameter name="dataset">bgl</parameter> </invoke> </function_calls>\n\nThe earliest panic is record 33937.';
    expect(stripToolMarkup(leaked)).toBe("Intro.\n\nThe earliest panic is record 33937.");
  });

  it("hides an unfinished block while it is still streaming", async () => {
    const { stripToolMarkup } = await import("../src/shared/agentText.js");
    expect(stripToolMarkup('Now checking.\n<function_calls> <invoke name="search_logs"> <parameter')).toBe("Now checking.\n");
  });

  it("leaves normal text, code and comparisons untouched", async () => {
    const { stripToolMarkup } = await import("../src/shared/agentText.js");
    const text = "Use `a < b` and <b>bold</b>; record_count > 20.";
    expect(stripToolMarkup(text)).toBe(text);
  });
});
