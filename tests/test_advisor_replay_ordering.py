"""ADVISOR_RESULT replay must never split an open tool round-trip.

The consult runs concurrently with the executor, so its event can be
logged between an ``llm.response`` carrying tool_calls and the matching
``tool.result`` events. Replaying it in raw event order would rebuild a
history with a user message inside the tool pair — an ordering strict
providers reject — so it defers to the iteration close exactly like
mid-turn user messages do.
"""
from __future__ import annotations

from types import SimpleNamespace

from surogates.harness.loop_advisor import AdvisorMixin
from surogates.harness.loop_context_replay import ContextReplayMixin
from surogates.session.events import EventType


def _event(etype: EventType, data: dict, eid: int):
    return SimpleNamespace(type=etype.value, data=data, id=eid)


def _rebuild(events):
    host = type("_H", (ContextReplayMixin, AdvisorMixin), {})()
    return host._rebuild_messages(events)


def _tool_call_iteration(eid_start: int) -> list:
    """LLM_REQUEST → LLM_RESPONSE(tool_calls) ... TOOL_RESULT."""
    return [
        _event(EventType.LLM_REQUEST, {}, eid_start),
        _event(EventType.LLM_RESPONSE, {
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "call_1", "type": "function",
                                "function": {"name": "read_file",
                                             "arguments": "{}"}}],
            },
        }, eid_start + 1),
    ]


def test_advisor_result_mid_iteration_defers_to_iteration_close():
    events = [
        _event(EventType.USER_MESSAGE, {"content": "fix the bug"}, 1),
        *_tool_call_iteration(2),
        # Advisor completed while the tool was executing.
        _event(EventType.ADVISOR_RESULT, {
            "category": "debugging", "content": "Check the delimiter.",
        }, 4),
        _event(EventType.TOOL_RESULT, {
            "tool_call_id": "call_1", "content": "file contents",
        }, 5),
    ]
    messages = _rebuild(events)
    roles = [m["role"] for m in messages]
    # user, assistant(tool_calls), tool, THEN advisor guidance.
    assert roles == ["user", "assistant", "tool", "user"]
    assert messages[1].get("tool_calls")
    assert messages[2]["tool_call_id"] == "call_1"
    assert messages[3]["content"].startswith("[Advisor guidance: debugging]")


def test_advisor_result_outside_iteration_replays_in_place():
    events = [
        _event(EventType.USER_MESSAGE, {"content": "fix the bug"}, 1),
        _event(EventType.ADVISOR_RESULT, {
            "category": "debugging", "content": "Check the delimiter.",
        }, 2),
    ]
    messages = _rebuild(events)
    assert [m["role"] for m in messages] == ["user", "user"]
    assert messages[1]["content"].startswith("[Advisor guidance: debugging]")


def test_deferred_advisor_stays_separate_from_coalesced_steers():
    """Live turns inject guidance as its own message; replay matches."""
    events = [
        _event(EventType.USER_MESSAGE, {"content": "fix the bug"}, 1),
        *_tool_call_iteration(2),
        _event(EventType.USER_MESSAGE, {"content": "also check tests"}, 4),
        _event(EventType.ADVISOR_RESULT, {
            "category": "debugging", "content": "Check the delimiter.",
        }, 5),
        _event(EventType.TOOL_RESULT, {
            "tool_call_id": "call_1", "content": "file contents",
        }, 6),
    ]
    messages = _rebuild(events)
    roles = [m["role"] for m in messages]
    assert roles == ["user", "assistant", "tool", "user", "user"]
    # The steer flushes first (coalesced), then the advisor's own message.
    assert "also check tests" in messages[3]["content"]
    assert messages[4]["content"].startswith("[Advisor guidance: debugging]")


def test_advisor_result_without_content_is_skipped():
    events = [
        _event(EventType.USER_MESSAGE, {"content": "hi"}, 1),
        _event(EventType.ADVISOR_RESULT, {"category": "coding"}, 2),
    ]
    assert len(_rebuild(events)) == 1
