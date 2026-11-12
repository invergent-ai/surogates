"""A policy on the old sub-agent tools binds the tools that start the same work.

Two gaps:

- An approval rule on ``agent`` reached only ``agent`` (and ``monitor``): ``delegate_task``, which starts the same
  work and stays callable beside ``agent``, ran without the approval.  An expert inherited it through the session's
  gate (#648).
- A policy on the coordinator's worker tools (``spawn_worker``, ``send_worker_message``, ``stop_worker``) did not
  reach ``agent`` and its companions, so a deny on ``spawn_worker`` still let ``agent(run_in_background=true)``
  start the same background work.
"""

from __future__ import annotations

import pytest

from surogates.governance.policy import GovernanceGate
from surogates.tools.builtin.media_gen import MediaGenConfig
from tests.test_expert_tool_scope import _consult_in_a_turn

AGENT_CALL = {"description": "Check the logs", "prompt": "Look at the logs and report."}


def test_an_approval_rule_on_agent_stops_a_delegate_task_call():
    gate = GovernanceGate(require_approval={"agent"})
    for name, arguments in (("agent", AGENT_CALL), ("delegate_task", {"goal": "Look at the logs."})):
        decision = gate.check(name, arguments)
        assert not decision.allowed and decision.overridable, name
        assert gate.offers(name) and not gate.runs_unasked(name), name
    # Listing and messaging start no work: no approval for them.
    assert gate.check("list_agents", {}).allowed


async def test_an_expert_gets_no_delegate_task_where_agent_needs_approval(monkeypatch, tmp_path):
    """An expert cannot ask anyone: a tool that needs approval is never sent to it, nor run for it."""
    model, sandbox = await _consult_in_a_turn(
        monkeypatch, tmp_path, media_gen=MediaGenConfig(), gate=GovernanceGate(require_approval={"agent"}),
        calls=["delegate_task"], expert_tools=["read_file", "delegate_task"],
    )
    assert model.sent() == {"read_file"}
    assert "not available to this expert" in model.results()["c0"]


@pytest.mark.parametrize("arguments", [
    {**AGENT_CALL, "run_in_background": True},
    AGENT_CALL,
], ids=["background", "foreground"])
def test_a_deny_on_spawn_worker_blocks_agent(arguments):
    gate = GovernanceGate(denied_tools={"spawn_worker"})
    assert not gate.check("agent", arguments).allowed
    assert not gate.offers("agent")
    # The policy names spawn_worker only: the gate allows delegate_task.  (With parity on it is not offered,
    # since agent is not: such a session loses delegation, as the PR states.)
    assert gate.check("delegate_task", {"goal": "Look at the logs."}).allowed


def test_a_stored_policy_denying_spawn_worker_blocks_agent_on_the_production_path():
    from surogates.runtime.governance import build_governance_gate

    gate = build_governance_gate({"denied_tools": ["spawn_worker"]})
    assert not gate.check("agent", {**AGENT_CALL, "run_in_background": True}).allowed
    assert not gate.offers("agent") and not gate.runs_unasked("agent")


def test_worker_message_and_stop_rules_reach_send_message_and_stop_agent():
    gate = GovernanceGate(denied_tools={"send_worker_message", "stop_worker"})
    assert not gate.check("send_message", {"to": "a1", "message": "hi"}).allowed
    assert not gate.check("stop_agent", {"agent_id": "a1"}).allowed
    assert gate.check("agent", AGENT_CALL).allowed
    assert gate.check("list_agents", {}).allowed


def test_an_approval_rule_on_spawn_worker_needs_approval_for_agent_only():
    gate = GovernanceGate(require_approval={"spawn_worker"})
    assert gate.check("agent", {**AGENT_CALL, "run_in_background": True}).overridable
    assert gate.check("send_message", {"to": "a1", "message": "hi"}).allowed
    assert gate.check("stop_agent", {"agent_id": "a1"}).allowed


def test_an_allow_list_naming_spawn_worker_keeps_the_agent_family():
    """An allow-list naming only the worker tools keeps sub-agents once they are retired (the design's migration)."""
    gate = GovernanceGate(allowed_tools={"spawn_worker", "send_worker_message", "stop_worker"})
    for name in ("agent", "send_message", "list_agents", "stop_agent"):
        assert gate.offers(name), name
