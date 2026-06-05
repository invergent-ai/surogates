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
#
# Note on the ``_planner_already_delegated_to_writer`` shape: the
# function reads the harness loop's in-memory ``messages`` list (not
# the wake's ``all_events`` snapshot).  The snapshot is captured ONCE
# at wake start and does not include tool calls emitted later in the
# same wake -- which produced a real bug in production where a planner
# that just successfully delegated and got a "report complete" reply
# would be nudged to delegate again, spawning a duplicate writer.
# These tests use the OpenAI message shape that the harness actually
# accumulates: ``{"role": "assistant", "tool_calls": [{"function":
# {"name": ..., "arguments": "<json string>"}}]}``.

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from surogates.harness.loop import (
    _is_deep_research_planner,
    _planner_already_delegated_to_writer,
    DEEP_RESEARCH_NO_DELEGATE_NUDGE,
)


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


def _assistant_tool_call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Build an OpenAI-shape assistant message carrying one tool call.

    Arguments are serialized to a JSON string to match what the
    provider returns and what the harness pushes into ``messages``.
    """
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": "call_test",
            "type": "function",
            "function": {
                "name": name,
                "arguments": json.dumps(arguments),
            },
        }],
    }


class TestPlannerAlreadyDelegatedToWriter:
    def test_empty_message_list_returns_false(self) -> None:
        assert _planner_already_delegated_to_writer([]) is False
        assert _planner_already_delegated_to_writer(None) is False

    def test_matches_single_goal_delegation(self) -> None:
        messages = [
            _assistant_tool_call("delegate_task", {
                "agent_type": "research-writer",
                "goal": "write the report",
            }),
        ]
        assert _planner_already_delegated_to_writer(messages) is True

    def test_matches_batched_goals_delegation(self) -> None:
        # The batched form puts agent_type inside each goals[] item
        # rather than at the top level.  Both shapes must satisfy the
        # check; a writer-targeting delegation in either form counts
        # as "the planner already did its job".
        messages = [
            _assistant_tool_call("delegate_task", {
                "goals": [
                    {"goal": "write", "agent_type": "research-writer"},
                ],
            }),
        ]
        assert _planner_already_delegated_to_writer(messages) is True

    def test_accepts_dict_arguments_in_addition_to_json_string(self) -> None:
        # Some code paths push a parsed-dict arguments object onto
        # messages (e.g. when the harness reconstructs a tool call
        # without re-serializing).  Honour both shapes so a future
        # refactor doesn't silently invalidate the check.
        messages = [{
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "call_x",
                "type": "function",
                "function": {
                    "name": "delegate_task",
                    "arguments": {"agent_type": "research-writer"},
                },
            }],
        }]
        assert _planner_already_delegated_to_writer(messages) is True

    def test_rejects_delegation_to_other_agent_type(self) -> None:
        # A planner that delegated to a *different* sub-agent (e.g.
        # ``code-reviewer``) still hasn't done the research-writer
        # handoff, so the orphan nudge should still fire.
        messages = [
            _assistant_tool_call("delegate_task", {
                "agent_type": "code-reviewer",
                "goal": "review",
            }),
        ]
        assert _planner_already_delegated_to_writer(messages) is False

    def test_rejects_other_tool_calls(self) -> None:
        messages = [
            _assistant_tool_call("research_memory", {"action": "add"}),
            _assistant_tool_call("web_search", {"query": "x"}),
            _assistant_tool_call("research_outline", {"action": "set"}),
        ]
        assert _planner_already_delegated_to_writer(messages) is False

    def test_ignores_non_assistant_messages(self) -> None:
        # A user message whose payload happens to mention
        # ``delegate_task`` must not satisfy the check -- the
        # predicate is strictly about assistant-emitted tool calls.
        messages = [
            {"role": "user", "content": "please delegate to research-writer"},
            {"role": "system", "content": "delegate_task is your hand-off tool"},
        ]
        assert _planner_already_delegated_to_writer(messages) is False

    def test_ignores_assistant_messages_without_tool_calls(self) -> None:
        # An assistant turn that mentioned the writer in prose but
        # didn't emit the tool call must NOT count as delegation --
        # that's exactly the failure mode the nudge is designed to
        # catch.
        messages = [{
            "role": "assistant",
            "content": "Now handing off to the research-writer.",
        }]
        assert _planner_already_delegated_to_writer(messages) is False

    def test_tolerates_malformed_arguments_json(self) -> None:
        # A truncated/streamed tool call whose arguments string is
        # incomplete JSON must not throw; it just doesn't satisfy the
        # check.
        messages = [{
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "call_y",
                "type": "function",
                "function": {
                    "name": "delegate_task",
                    "arguments": '{"agent_type": "research-writer',
                },
            }],
        }]
        assert _planner_already_delegated_to_writer(messages) is False


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
