"""A policy names the sub-agent tools by the family's own names; ``terminal`` also names ``monitor``."""

from __future__ import annotations

from surogates.governance.policy import GovernanceGate
from surogates.harness.subagents import offer_family

FAMILY = {"agent", "send_message", "list_agents", "stop_agent"}


def test_denying_the_family_and_terminal_denies_them_and_monitor():
    gate = GovernanceGate(denied_tools=FAMILY | {"terminal"})
    assert not any(gate.offers(name) for name in FAMILY | {"monitor", "terminal"})
    assert gate.offers("read_file")


def test_an_allow_list_naming_the_family_allows_it_and_no_more():
    gate = GovernanceGate(allowed_tools=FAMILY | {"read_file"})
    assert all(gate.offers(name) for name in FAMILY)
    assert not gate.offers("monitor") and not gate.offers("terminal")
    assert not any(GovernanceGate(allowed_tools={"read_file"}).offers(name) for name in FAMILY)


def test_approval_of_agent_is_asked_for_agent_and_not_for_a_list():
    gate = GovernanceGate(require_approval={"agent"})
    assert gate._name_decision("agent").overridable is True
    assert gate._name_decision("list_agents") is None


def test_a_session_without_agent_gets_none_of_its_companions():
    assert offer_family({"agent", "read_file"}, {}) >= {"agent", "send_message", "list_agents", "stop_agent"}
    assert not offer_family({"read_file"}, {"excluded_tools": ["agent"]}) & FAMILY
