from log_insights.policies import EVIDENCE_RULES, ROLE_POLICIES, Role


def test_all_required_roles_have_distinct_policies() -> None:
    assert set(ROLE_POLICIES) == set(Role)
    assert len({policy.objective for policy in ROLE_POLICIES.values()}) == len(Role)


def test_evidence_policy_requires_executed_queries() -> None:
    assert any("executed query" in rule for rule in EVIDENCE_RULES)
