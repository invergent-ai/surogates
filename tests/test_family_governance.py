"""What a session keeps of the agent family whatever its agent's policy says.

A project's master keeps the family: Studio's policy editor does not know
projects, and a master that can start no thread does no work.  A sub-agent
keeps ``send_message``, kept only to reach the agent that started it.  A
session whose agents run keeps listing, messaging and stopping them.  Nobody
else keeps anything: a policy that denies ``agent`` denies starting agents.
"""

from __future__ import annotations

import logging

from surogates.governance.policy import GovernanceGate, KeptFamilyGate
from surogates.harness.subagents import DISPATCH_KEY, kept_whatever_policy, offer_family, parent_only, warn_unknown_tools
from surogates.tools.loader import AgentDef

FAMILY = {"agent", "send_message", "list_agents", "stop_agent"}
MASTER = {"workstream_role": "coordinator", "workstream_id": "p1"}
SUB_AGENT = {DISPATCH_KEY: {"mode": "background", "description": "Count", "parent": "p"}}


def gate(policy: GovernanceGate, config: dict, *, has_agents: bool = False) -> KeptFamilyGate:
    return KeptFamilyGate(policy, kept_whatever_policy(config, has_agents=has_agents))


def test_a_master_keeps_the_family_under_a_policy_that_denies_it():
    kept = gate(GovernanceGate(denied_tools={"agent", "send_message"}), MASTER)
    assert all(kept.offers(name) and kept.check(name, {}).allowed for name in FAMILY)


def test_a_chat_under_that_policy_cannot_start_agents():
    kept = gate(GovernanceGate(denied_tools={"agent"}), {})
    assert not kept.offers("agent") and not kept.check("agent", {}).allowed


def test_a_session_with_agents_keeps_list_message_and_stop():
    kept = gate(GovernanceGate(denied_tools=FAMILY), {}, has_agents=True)
    assert {name for name in FAMILY if kept.offers(name)} == {"send_message", "list_agents", "stop_agent"}
    assert all(kept.check(name, {}).allowed for name in ("send_message", "list_agents", "stop_agent"))
    assert not kept.check("agent", {}).allowed


def test_an_allow_list_without_the_family_keeps_a_masters_family():
    kept = gate(GovernanceGate(allowed_tools={"web_search"}), MASTER)
    assert all(kept.offers(name) and kept.check(name, {}).allowed for name in FAMILY)
    assert not kept.offers("read_file") and not kept.check("read_file", {"path": "a.md"}).allowed
    # A master's own tool list (``allowed_tools`` in its config) keeps them too.
    assert FAMILY <= offer_family({"agent", "memory"}, {**MASTER, "allowed_tools": ["memory"]})


def test_a_sub_agent_keeps_send_message_only_to_reach_its_parent():
    kept = gate(GovernanceGate(denied_tools=FAMILY), SUB_AGENT)
    assert kept.offers("send_message") and kept.kept_only("send_message")
    assert not kept.offers("agent") and not kept.offers("list_agents")
    assert parent_only(SUB_AGENT, kept_only=True) and not parent_only(SUB_AGENT)
    # Under a policy that offers it, it is no more kept-only than any tool.
    assert not gate(GovernanceGate(), SUB_AGENT).kept_only("send_message")


def test_a_profile_narrows_what_is_not_kept_and_keeps_what_is():
    narrowed = gate(GovernanceGate(), MASTER).with_profile({"denied_tools": ["agent", "read_file"]})
    assert narrowed.offers("agent") and not narrowed.offers("read_file")


def test_an_agent_definition_naming_an_unknown_tool_is_warned_about_once(caplog):
    agents = [AgentDef(
        name="old-reader", description="Reads", system_prompt="", source="platform",
        tools=["read_file", "delegate_task", "mcp__hub__search"], disallowed_tools=["spawn_worker"],
    )]
    with caplog.at_level(logging.WARNING, logger="surogates.harness.subagents"):
        warn_unknown_tools(agents, {"read_file", "agent"})
        warn_unknown_tools(agents, {"read_file", "agent"})
    warned = [r.getMessage() for r in caplog.records if r.name == "surogates.harness.subagents"]
    assert len(warned) == 2
    assert any("delegate_task" in w and "old-reader" in w for w in warned)
    assert any("spawn_worker" in w for w in warned)
