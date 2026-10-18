import { ROLES, type ToolCallView } from "../shared/events";
import { describeResult, humanize, type Row } from "../shared/toolResult";
import type { Turn } from "./store";

const cell = (value: unknown) =>
  value === null || value === undefined ? "" : String(value).replace(/\|/g, "\\|").replace(/\n/g, " ");

function table(columns: string[], rows: Row[]): string[] {
  return [
    `| ${columns.map(humanize).join(" | ")} |`,
    `| ${columns.map(() => "---").join(" | ")} |`,
    ...rows.map((row) => `| ${columns.map((c) => cell(row[c])).join(" | ")} |`),
  ];
}

function toolSection(call: ToolCallView, index: number): string[] {
  const lines = [`### ${index}. Tool call \`${call.tool ?? call.title}\` (${call.status})`, ""];
  lines.push("Arguments:", "", "```json", JSON.stringify(call.input ?? {}, null, 2), "```", "");
  const model = describeResult(call.output);
  if (!model) {
    if (call.outputText ?? call.output) lines.push("Result:", "", "```", call.outputText ?? JSON.stringify(call.output, null, 2), "```", "");
    return lines;
  }
  lines.push(`**Result:** ${model.headline}${model.truncated === false ? " (complete)" : ""}`, "");
  if (model.stats.length > 0) lines.push(...model.stats.map((s) => `- ${s.label}: ${s.value}`), "");
  for (const bars of model.bars) lines.push(`**${bars.label}**`, "", ...table([...bars.labelKeys, bars.countKey], bars.rows), "");
  for (const t of model.tables) {
    const rows = t.highlightRow === null ? t.rows : t.rows.map((r, i) => (i === t.highlightRow ? { ...r, "": "◀ anchor" } : r));
    lines.push(`**${t.label}** (${t.rows.length} rows)`, "", ...table(t.highlightRow === null ? t.columns : [...t.columns, ""], rows), "");
  }
  for (const note of model.notes) lines.push(`> **${note.label}:** ${note.text}`, "");
  for (const detail of model.details) {
    lines.push(`<details><summary>${detail.label}</summary>`, "", ...Object.entries(detail.values).map(([k, v]) => `- ${k}: ${cell(v)}`), "", "</details>", "");
  }
  for (const query of model.sql) {
    lines.push(`SQL (${humanize(query.name)}), parameters \`${JSON.stringify(query.parameters)}\`:`, "", "```sql", query.sql.trim(), "```", "");
  }
  return lines;
}

/** Full report of one request: question, the agent's workflow in order (reasoning, tool calls with
 * results and SQL), the answer, and usage. */
export function turnToMarkdown(turn: Turn, toolCalls: Record<string, ToolCallView>): string {
  const role = ROLES.find((r) => r.id === turn.role)?.label ?? turn.role;
  const lines = [
    `# RUBIN report: ${role}`,
    "",
    `**Request:** ${turn.question}`,
    "",
    `- Role: ${role}`,
    `- Asked: ${new Date(turn.startedAt).toISOString()}`,
    `- Tool calls: ${turn.toolCallIds.length}`,
    ...(turn.durationMs !== null ? [`- Duration: ${(turn.durationMs / 1000).toFixed(1)} s`] : []),
    ...(turn.credits !== null ? [`- Kiro credits: ${turn.credits.toFixed(3)}`] : []),
    "",
    "## Agent workflow",
    "",
  ];
  // The last text segment is the answer; earlier ones are interim notes inside the workflow.
  const answer = [...turn.items].reverse().find((i) => i.kind === "text");
  let toolIndex = 0;
  for (const item of turn.items) {
    if (item.kind === "thought" && item.text.trim()) {
      lines.push(...item.text.trim().split("\n").map((l) => `> ${l}`), "");
    } else if (item.kind === "tool") {
      const call = toolCalls[item.id];
      if (call) lines.push(...toolSection(call, ++toolIndex));
    } else if (item.kind === "text" && item.text.trim() && item !== answer) {
      lines.push(item.text.trim(), "");
    }
  }
  lines.push("## Answer", "", answer?.kind === "text" ? answer.text.trim() : "_(no answer)_", "");
  if (turn.error) lines.push(`> **Error:** ${turn.error}`, "");
  lines.push("---", "_Counts are log records, not incidents. Team ownership is a fictional mapping._", "");
  return lines.join("\n");
}

export function reportFilename(turn: Turn): string {
  const slug = turn.question.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 40) || "request";
  return `rubin-report-${turn.role}-${new Date(turn.startedAt).toISOString().slice(0, 10)}-${slug}.md`;
}

export function download(filename: string, text: string): void {
  const url = URL.createObjectURL(new Blob([text], { type: "text/markdown" }));
  const link = Object.assign(document.createElement("a"), { href: url, download: filename });
  link.click();
  URL.revokeObjectURL(url);
}
