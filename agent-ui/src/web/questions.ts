import type { RoleId } from "../shared/events";

// Aligned with the RUBIN v1.5 role workflows in .kiro/agents (one VM spawn performance question each).
export const SUGGESTED_QUESTIONS: Record<RoleId, string[]> = {
  engineer: [
    "Which VM spawns were the slowest compared with the measured baseline? Pick the slowest real instance and show what its logs say around the spawn.",
    "Search BGL for kernel panic messages. Take the earliest match, show the events before and after it, and suggest the next diagnostic check.",
    "Find OpenStack ERROR records from nova-compute, pivot on one real instance_id and show its event sequence.",
  ],
  "team-lead": [
    "How often are VM launches slow, how many distinct instances are affected, and is it getting worse day by day?",
    "Which specific BGL error patterns recur most over time, and which (fictional) teams own the components involved?",
    "Show the weekly trend of BGL FATAL records and suggest an investigation order for the teams.",
  ],
  manager: [
    "Is VM launch performance materially worse than its baseline, and should it be a priority next to the BGL kernel failures?",
    "Across BGL and OpenStack, which observed problems deserve investigation first? Give up to three priorities with measured evidence.",
    "What do these logs not tell us about operational impact, and what should we measure next?",
  ],
};
