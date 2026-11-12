"""A policy binds the ``agent`` family by its own names; the one stand-in left is ``terminal`` for ``monitor``.

The old sub-agent tools are gone, and so are their stand-ins: a stored policy that still names one names a tool
that does not exist (ops renames them in the stored policies), and binds nothing.
"""

from __future__ import annotations

from surogates.governance.policy import GovernanceGate
from surogates.tools.builtin.media_gen import MediaGenConfig
from tests.test_expert_tool_scope import _consult_in_a_turn

AGENT_CALL = {"description": "Check the logs", "prompt": "Look at the logs and report."}


def test_a_deny_on_terminal_denies_monitor():
    gate = GovernanceGate(denied_tools={"terminal"})
    assert not gate.check("monitor", {"command": "tail -f app.log", "description": "Errors"}).allowed
    assert not gate.offers("monitor")


def test_an_approval_rule_on_agent_gates_agent_and_not_its_companions():
    gate = GovernanceGate(require_approval={"agent"})
    decision = gate.check("agent", AGENT_CALL)
    assert not decision.allowed and decision.overridable
    assert gate.offers("agent") and not gate.runs_unasked("agent")
    # Listing, messaging and stopping start no work: no approval for them.
    for name, arguments in (("list_agents", {}), ("send_message", {"to": "a1", "message": "hi", "summary": "hi"}),
                            ("stop_agent", {"agent_id": "a1"})):
        assert gate.check(name, arguments).allowed, name


async def test_an_expert_gets_no_agent_where_agent_needs_approval(monkeypatch, tmp_path):
    """An expert cannot ask anyone: a tool that needs approval is never sent to it, nor run for it."""
    model, sandbox = await _consult_in_a_turn(
        monkeypatch, tmp_path, media_gen=MediaGenConfig(), gate=GovernanceGate(require_approval={"agent"}),
        calls=["agent"], expert_tools=["read_file", "agent"],
    )
    assert model.sent() == {"read_file"}
    assert "not available to this expert" in model.results()["c0"]


def test_a_policy_naming_an_old_tool_binds_nothing():
    for policy in ({"denied_tools": {"delegate_task", "spawn_worker"}}, {"require_approval": {"delegate_task"}}):
        gate = GovernanceGate(**policy)
        assert gate.check("agent", AGENT_CALL).allowed, policy
        assert gate.offers("agent") and gate.offers("send_message"), policy


def test_a_stored_policy_denying_agent_blocks_it_on_the_production_path():
    from surogates.runtime.governance import build_governance_gate

    gate = build_governance_gate({"denied_tools": ["agent"]})
    assert not gate.check("agent", {**AGENT_CALL, "run_in_background": True}).allowed
    assert not gate.offers("agent") and not gate.runs_unasked("agent")
