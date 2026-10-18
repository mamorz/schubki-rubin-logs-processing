import { ROLES } from "../shared/events";
import { describeResult } from "../shared/toolResult";
import type { Turn } from "./store";
import type { ToolCallView } from "../shared/events";

/** Writes the session as a worked investigation: question → tool calls → SQL → result → answer. */
export function sessionToMarkdown(turns: Turn[], toolCalls: Record<string, ToolCallView>): string {
  const lines = [`# RUBIN worked investigation`, ``, `Exported ${new Date().toISOString()} from the RUBIN agent UI.`, ``];
  for (const turn of turns) {
    const role = ROLES.find((r) => r.id === turn.role)?.label ?? turn.role;
    lines.push(`## ${role}: ${turn.question}`, ``);
    turn.toolCallIds.forEach((id, index) => {
      const call = toolCalls[id];
      if (!call) return;
      const model = describeResult(call.output);
      lines.push(`### Tool call ${index + 1}: \`${call.tool ?? call.title}\``, ``);
      lines.push("```json", JSON.stringify(call.input ?? {}, null, 2), "```", ``);
      if (model) lines.push(`**Result:** ${model.headline}`, ``);
      for (const query of model?.sql ?? []) {
        lines.push(`SQL (${query.name}), parameters \`${JSON.stringify(query.parameters)}\`:`, ``, "```sql", query.sql.trim(), "```", ``);
      }
    });
    lines.push(`### Answer`, ``, turn.text.trim() || "_(no answer)_", ``);
    if (turn.credits !== null) lines.push(`_Kiro credits used: ${turn.credits.toFixed(3)}_`, ``);
  }
  return lines.join("\n");
}

export function download(filename: string, text: string): void {
  const url = URL.createObjectURL(new Blob([text], { type: "text/markdown" }));
  const link = Object.assign(document.createElement("a"), { href: url, download: filename });
  link.click();
  URL.revokeObjectURL(url);
}
