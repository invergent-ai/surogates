"""The schema filter and the prompt filter must gate on the same conditions.

``worker._filter_effective_tools`` builds the PROMPT surface;
``AgentHarness._tool_filter_for_session`` builds the LLM-visible SCHEMAS.
They ran different rules: the prompt already dropped the board / worker /
research self-tools for sessions lacking their gate, while the schema kept
advertising them — so a plain web session paid context for tools it was
never told about and whose handlers fail closed on the same condition.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from surogates.harness.loop import AgentHarness
from surogates.orchestrator.worker import _filter_effective_tools
from surogates.runtime.governance import (
    BOARD_SELF_TOOLS,
    RESEARCH_SPINE_TOOLS,
    WORKER_SELF_TOOLS,
)

ALL_TOOLS = (
    {"read_file", "terminal", "memory"}
    | set(WORKER_SELF_TOOLS) | set(BOARD_SELF_TOOLS) | set(RESEARCH_SPINE_TOOLS)
)
GATED = set(WORKER_SELF_TOOLS) | set(BOARD_SELF_TOOLS) | set(RESEARCH_SPINE_TOOLS)


def _harness() -> SimpleNamespace:
    return SimpleNamespace(_tools=SimpleNamespace(tool_names=set(ALL_TOOLS)))


def _session(**kw) -> SimpleNamespace:
    return SimpleNamespace(
        task_id=kw.pop("task_id", None),
        channel=kw.pop("channel", "web"),
        config=kw.pop("config", {}),
        **kw,
    )


def _gate(session) -> set[str]:
    return AgentHarness._apply_execution_context_gates(
        _harness(), session, None,
    )


def test_plain_web_session_is_offered_none_of_the_gated_tools() -> None:
    assert _gate(_session()) & GATED == set()


def test_task_worker_keeps_its_self_tools_under_a_restrictive_allowlist() -> None:
    # An AgentDef whose `tools` list is just its domain tools: the worker
    # still needs to be able to signal completion or failure.
    result = AgentHarness._apply_execution_context_gates(
        _harness(), _session(task_id="t-1"), {"read_file"},
    )
    assert WORKER_SELF_TOOLS <= result
    assert result & set(BOARD_SELF_TOOLS) == set()


def test_board_tools_appear_only_for_a_coordination_group_member() -> None:
    member = _gate(_session(config={"context_group_id": "g-1"}))
    assert BOARD_SELF_TOOLS <= member


def test_research_spine_is_coordinator_only_and_never_force_added() -> None:
    coordinator = _gate(_session(config={"active_research_run_id": "r-1"}))
    assert RESEARCH_SPINE_TOOLS <= coordinator

    # A task worker inside the same run stays tree-blind.
    executor = _gate(
        _session(task_id="t-1", config={"active_research_run_id": "r-1"}),
    )
    assert executor & set(RESEARCH_SPINE_TOOLS) == set()

    # Never force-added: absent from the allowlist means absent.
    restricted = AgentHarness._apply_execution_context_gates(
        _harness(), _session(config={"active_research_run_id": "r-1"}),
        {"read_file"},
    )
    assert restricted & set(RESEARCH_SPINE_TOOLS) == set()


@pytest.mark.parametrize("config,task_id", [
    ({}, None),
    ({"context_group_id": "g-1"}, None),
    ({"active_research_run_id": "r-1"}, None),
    ({}, "t-1"),
    ({"context_group_id": "g-1", "active_research_run_id": "r-1"}, "t-1"),
])
def test_schema_filter_agrees_with_the_prompt_filter(config, task_id) -> None:
    """The invariant this change exists to restore.

    If these two ever disagree the model is told about a tool it does not
    hold, or holds one it was never told about. Observed in the wild: a
    claude-coder task worker could not signal failure, so its refusal was
    filed as a successful result and the parent mission stalled.
    """
    session = _session(config=config, task_id=task_id)
    schemas = AgentHarness._apply_execution_context_gates(
        _harness(), session, None,
    )
    prompt = _filter_effective_tools(
        tools=set(ALL_TOOLS),
        tenant=SimpleNamespace(user_id="u-1", service_account_id=None),
        session=session,
        use_api_for_harness_tools=True,
    )
    assert schemas & GATED == prompt & GATED
