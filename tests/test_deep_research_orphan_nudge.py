# Copyright (c) 2026, Invergent SA, developed by Flavius Burca
# SPDX-License-Identifier: AGPL-3.0-only
#
# Pins the helpers behind the deep-research orphan-completion guard:
# the planner is supposed to delegate to ``research-writer`` as its
# final tool call, and in the wild we have seen the model describe the
# handoff in prose without emitting the call.  The guard detects that
# case and re-prompts once; these tests pin the two predicates the
# guard uses (``_is_deep_research_planner`` + ``_planner_already_
# delegated_to_writer``) so the rule can't drift silently.

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from surogates.harness.loop import (
    _is_deep_research_planner,
    _planner_already_delegated_to_writer,
    DEEP_RESEARCH_NO_DELEGATE_NUDGE,
)
from surogates.session.events import EventType


@dataclass
class _FakeEvent:
    type: str
    data: dict[str, Any]


@dataclass
class _FakeSession:
    config: dict[str, Any] | None


# ---------------------------------------------------------------------------
# _is_deep_research_planner
# ---------------------------------------------------------------------------


class TestIsDeepResearchPlanner:
    def test_matches_deep_research_agent_type(self) -> None:
        s = _FakeSession(config={"agent_type": "deep-research"})
        assert _is_deep_research_planner(s) is True

    def test_rejects_other_agent_types(self) -> None:
        assert _is_deep_research_planner(
            _FakeSession(config={"agent_type": "research-writer"}),
        ) is False
        assert _is_deep_research_planner(
            _FakeSession(config={"agent_type": "code-reviewer"}),
        ) is False
        assert _is_deep_research_planner(
            _FakeSession(config={}),
        ) is False

    def test_handles_missing_config(self) -> None:
        # session.config may be None for a freshly-created row.
        assert _is_deep_research_planner(_FakeSession(config=None)) is False

    def test_handles_none_session(self) -> None:
        assert _is_deep_research_planner(None) is False  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# _planner_already_delegated_to_writer
# ---------------------------------------------------------------------------


def _tool_call(name: str, arguments: dict[str, Any]) -> _FakeEvent:
    return _FakeEvent(
        type=EventType.TOOL_CALL.value,
        data={"name": name, "arguments": arguments},
    )


class TestPlannerAlreadyDelegatedToWriter:
    def test_empty_event_log_returns_false(self) -> None:
        assert _planner_already_delegated_to_writer([]) is False
        assert _planner_already_delegated_to_writer(None) is False

    def test_matches_single_goal_delegation(self) -> None:
        events = [
            _tool_call("delegate_task", {
                "agent_type": "research-writer",
                "goal": "write the report",
            }),
        ]
        assert _planner_already_delegated_to_writer(events) is True

    def test_matches_batched_goals_delegation(self) -> None:
        # The batched form puts agent_type inside each goals[] item
        # rather than at the top level.  Both shapes must satisfy the
        # check; a writer-targeting delegation in either form counts
        # as "the planner already did its job".
        events = [
            _tool_call("delegate_task", {
                "goals": [
                    {"goal": "write", "agent_type": "research-writer"},
                ],
            }),
        ]
        assert _planner_already_delegated_to_writer(events) is True

    def test_rejects_delegation_to_other_agent_type(self) -> None:
        # A planner that delegated to a *different* sub-agent (e.g.
        # ``code-reviewer``) still hasn't done the research-writer
        # handoff, so the orphan nudge should still fire.
        events = [
            _tool_call("delegate_task", {
                "agent_type": "code-reviewer",
                "goal": "review",
            }),
        ]
        assert _planner_already_delegated_to_writer(events) is False

    def test_rejects_other_tool_calls(self) -> None:
        events = [
            _tool_call("research_memory", {"action": "add"}),
            _tool_call("web_search", {"query": "x"}),
            _tool_call("research_outline", {"action": "set"}),
        ]
        assert _planner_already_delegated_to_writer(events) is False

    def test_ignores_non_tool_call_events(self) -> None:
        # A user.message event named delegate_task in some odd payload
        # must not satisfy the check -- the predicate is strictly
        # about TOOL_CALL events.
        events = [
            _FakeEvent(
                type=EventType.USER_MESSAGE.value,
                data={"name": "delegate_task",
                      "arguments": {"agent_type": "research-writer"}},
            ),
        ]
        assert _planner_already_delegated_to_writer(events) is False


# ---------------------------------------------------------------------------
# Nudge text contract
# ---------------------------------------------------------------------------


class TestNudgeText:
    def test_mentions_delegate_task_and_research_writer(self) -> None:
        # Both the tool name and the target agent_type appear verbatim
        # so a copy-paste-prone model can use the message as a literal
        # template for the missing tool call.
        assert "delegate_task" in DEEP_RESEARCH_NO_DELEGATE_NUDGE
        assert "research-writer" in DEEP_RESEARCH_NO_DELEGATE_NUDGE

    def test_warns_against_prose_or_next_action_blocks(self) -> None:
        # The two failure modes that prompted this guard must each
        # appear in the nudge so the model understands what it did
        # wrong.
        assert "tool call" in DEEP_RESEARCH_NO_DELEGATE_NUDGE
        assert "next_action" in DEEP_RESEARCH_NO_DELEGATE_NUDGE
