"""Role-specific answer expectations and evidence handling rules."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Role(str, Enum):
    ENGINEER = "engineer"
    TEAM_LEAD = "team_lead"
    MANAGER = "manager"


@dataclass(frozen=True)
class RolePolicy:
    """High-level requirements derived from the challenge brief."""

    role: Role
    objective: str
    required_evidence: tuple[str, ...]


ROLE_POLICIES: dict[Role, RolePolicy] = {
    Role.ENGINEER: RolePolicy(
        role=Role.ENGINEER,
        objective="Investigate a failure and recommend the next diagnostic check.",
        required_evidence=("relevant log entries", "nearby events", "component"),
    ),
    Role.TEAM_LEAD: RolePolicy(
        role=Role.TEAM_LEAD,
        objective="Identify recurring problems and what the team should investigate first.",
        required_evidence=("complete-scope counts", "time trend", "component or team"),
    ),
    Role.MANAGER: RolePolicy(
        role=Role.MANAGER,
        objective="Prioritize observed problems and supported next actions.",
        required_evidence=("measured frequency", "affected components", "stated scope"),
    ),
}


EVIDENCE_RULES: tuple[str, ...] = (
    "Ground every measured claim in an executed query.",
    "State the dataset, time range, filters, and whether results were truncated.",
    "Separate observed facts, hypotheses, recommended actions, and unknowns.",
    "Do not describe matching log entries as distinct incidents without correlation evidence.",
    "Do not invent financial loss, downtime, or customer impact.",
)


def get_role_policy(role: Role) -> RolePolicy:
    """Return the policy for one of the three required roles."""

    return ROLE_POLICIES[role]
