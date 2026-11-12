"""Deep research runs on the ``agent`` tool: ``/deep-research`` starts the planner, the planner hands off to the writer."""

from __future__ import annotations

import json

from surogates.harness.loop_deep_research import (
    DEEP_RESEARCH_NO_DELEGATE_NUDGE,
    _planner_already_delegated_to_writer,
)
from surogates.harness.slash_skill import build_deep_research_message


def _calling(name: str, arguments: dict) -> list[dict]:
    return [{"role": "assistant", "tool_calls": [{"function": {"name": name, "arguments": json.dumps(arguments)}}]}]


def test_the_writer_handoff_is_an_agent_call():
    messages = _calling("agent", {"agent_type": "research-writer", "description": "Write the report", "prompt": "..."})
    assert _planner_already_delegated_to_writer(messages)


def test_an_agent_call_to_another_type_is_no_handoff():
    assert not _planner_already_delegated_to_writer(_calling("agent", {"agent_type": "deep-research", "prompt": "..."}))


def test_a_delegate_task_call_is_no_handoff():
    messages = _calling("delegate_task", {"agent_type": "research-writer", "goal": "..."})
    assert not _planner_already_delegated_to_writer(messages)


def test_the_nudge_asks_for_an_agent_call_to_the_writer():
    assert 'agent(agent_type="research-writer"' in DEEP_RESEARCH_NO_DELEGATE_NUDGE
    assert "delegate_task" not in DEEP_RESEARCH_NO_DELEGATE_NUDGE


def test_deep_research_starts_the_planner_with_one_agent_call():
    for text in (build_deep_research_message(topic="EU battery rules"), build_deep_research_message(topic="")):
        assert "`agent`" in text and 'agent_type="deep-research"' in text
        assert "delegate_task" not in text and "goals" not in text
    assert "EU battery rules" in build_deep_research_message(topic="EU battery rules")
