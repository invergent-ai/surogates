"""PolicyDecision metadata for user-overridable denials."""

from __future__ import annotations

from surogates.governance.policy import PolicyDecision


def test_policy_decision_overridable_default_false():
    decision = PolicyDecision(
        allowed=False,
        reason="external",
        tool_name="send_email",
    )

    assert decision.overridable is False
    assert decision.policy_id is None


def test_policy_decision_overridable_can_be_set_true():
    decision = PolicyDecision(
        allowed=False,
        reason="external",
        tool_name="send_email",
        overridable=True,
        policy_id="external-comms-v1",
    )

    assert decision.overridable is True
    assert decision.policy_id == "external-comms-v1"
