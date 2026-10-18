// Models sometimes write pseudo tool-call markup into their visible answer text. The real calls
// arrive as ACP tool calls (and are shown as evidence cards), so this markup is never evidence.

const MARKUP_TAGS = "function_calls|function_results|invoke|parameter|tool_call|tool_result";
const COMPLETE_BLOCK = new RegExp(`<(${MARKUP_TAGS})\\b[^>]*>[\\s\\S]*?</\\1>`, "g");
/** An opening tag whose block has not finished streaming yet. */
const OPEN_BLOCK_AT_END = new RegExp(`<(?:${MARKUP_TAGS})\\b[\\s\\S]*$`);

/** Removes pseudo tool-call blocks (complete, or still streaming at the end) from answer text. */
export function stripToolMarkup(text: string): string {
  let previous: string;
  let result = text;
  do {
    previous = result;
    result = result.replace(COMPLETE_BLOCK, "");
  } while (result !== previous);
  return result.replace(OPEN_BLOCK_AT_END, "").replace(/\n{3,}/g, "\n\n");
}
