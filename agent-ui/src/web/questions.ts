import type { RoleId } from "../shared/events";

export const SUGGESTED_QUESTIONS: Record<RoleId, string[]> = {
  engineer: [
    "Search BGL for kernel panic messages. Take the earliest match, show the 5 events before and after it, explain a possible cause and the next check.",
    "Find FATAL records in BGL on 2005-06-05 and show what happened around the first one.",
    "Search OpenStack for ERROR messages from nova-compute and show nearby events for one of them.",
  ],
  "team-lead": [
    "Which BGL components produce the most FATAL and ERROR records, and which teams own them?",
    "Show the daily trend of BGL FATAL records. Which days spike, and which components drive them?",
    "Summarize OpenStack logs by team and by severity. What should the team investigate first?",
  ],
  manager: [
    "Across BGL and OpenStack, which observed problems deserve attention first? Give a short prioritized list with measured counts.",
    "Which teams are most affected by error-level records, and what should happen next?",
    "What do these logs not tell us about impact, and what should we measure next?",
  ],
};
