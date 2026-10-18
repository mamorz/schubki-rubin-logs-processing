import type { RequestPermissionRequest, RequestPermissionResponse } from "@agentclientprotocol/sdk";

/** Allow only tools of the rubin MCP server; reject everything else (shell, files, web). */
export function decidePermission(request: RequestPermissionRequest, serverName: string): RequestPermissionResponse {
  const call = request.toolCall;
  const meta = call._meta?.kiro as { mcpServerName?: string } | undefined;
  const isRubin = meta?.mcpServerName === serverName || (call.title ?? "").includes(`@${serverName}/`);
  const wanted = isRubin ? ["allow_once", "allow_always"] : ["reject_once", "reject_always"];
  const option = wanted.map((kind) => request.options.find((o) => o.kind === kind)).find(Boolean);
  if (!option) return { outcome: { outcome: "cancelled" } };
  return { outcome: { outcome: "selected", optionId: option.optionId } };
}
