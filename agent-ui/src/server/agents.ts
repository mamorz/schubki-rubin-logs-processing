import { access } from "node:fs/promises";
import { join } from "node:path";
import { ROLES, type AgentInfo } from "../shared/events.js";

async function exists(path: string): Promise<boolean> {
  return access(path).then(() => true, () => false);
}

/** Workspace agents (teammates' .kiro/agents) win over the bundled fallbacks of the same name. */
export async function listAgents(workspace: string): Promise<AgentInfo[]> {
  return Promise.all(
    ROLES.map(async (role) => {
      const dir = join(workspace, ".kiro", "agents");
      const inWorkspace = (await exists(join(dir, `${role.agent}.json`))) || (await exists(join(dir, `${role.agent}.md`)));
      return { role: role.id, agent: role.agent, source: inWorkspace ? "workspace" : "fallback" } as const;
    }),
  );
}
