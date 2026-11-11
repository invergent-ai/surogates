"""A policy that names ``delegate_task`` names the sub-agent tools that take its place."""

from __future__ import annotations

from surogates.governance.policy import GovernanceGate
from surogates.harness.subagents import apply_parity

FAMILY = {"agent", "send_message", "list_agents", "stop_agent"}


def test_denying_delegate_task_denies_the_family_and_terminal_denies_monitor():
    gate = GovernanceGate(denied_tools={"delegate_task", "terminal"})
    assert not any(gate.offers(name) for name in FAMILY | {"monitor", "delegate_task", "terminal"})
    assert gate.offers("read_file")


def test_an_allow_list_naming_delegate_task_allows_the_family():
    gate = GovernanceGate(allowed_tools={"delegate_task", "read_file"})
    assert all(gate.offers(name) for name in FAMILY)
    assert not gate.offers("monitor") and not gate.offers("terminal")
    assert not any(GovernanceGate(allowed_tools={"read_file"}).offers(name) for name in FAMILY)


def test_approval_of_delegate_task_is_asked_for_agent_and_not_for_a_list():
    gate = GovernanceGate(require_approval={"delegate_task"})
    assert gate._name_decision("agent").overridable is True
    assert gate._name_decision("list_agents") is None


def test_an_explicit_exclusion_of_agent_holds_with_parity_on():
    on = {"subagent_parity": True}
    assert "agent" in apply_parity({"delegate_task", "read_file"}, on)
    assert "agent" not in apply_parity({"delegate_task", "read_file"}, {**on, "excluded_tools": ["agent"]})
    assert apply_parity({"delegate_task", "read_file"}, {"subagent_parity": False}) == {"delegate_task", "read_file"}
