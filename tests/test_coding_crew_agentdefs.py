"""The three crew AgentDefs parse and expose the expected tool filters."""

from __future__ import annotations

from surogates.coding_agents.crew_seed import parse_crew_agentdefs


def _by_name():
    return {a.name: a for a in parse_crew_agentdefs()}


def test_all_three_parse():
    defs = _by_name()
    assert set(defs) == {"claude-coder", "codex-reviewer", "code-orchestrator"}
    for a in defs.values():
        assert a.description  # required by _build_agent_def
        assert a.system_prompt.strip()


def test_claude_coder_can_run_coding_agent():
    a = _by_name()["claude-coder"]
    assert "run_coding_agent" in (a.tools or [])


def test_codex_reviewer_can_run_coding_agent():
    a = _by_name()["codex-reviewer"]
    assert "run_coding_agent" in (a.tools or [])


def test_orchestrator_cannot_run_code_directly():
    a = _by_name()["code-orchestrator"]
    assert "spawn_task" in (a.tools or [])
    assert "run_coding_agent" in (a.disallowed_tools or [])
