"""Which tool calls a stopped worker left unanswered, and where their results go."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from surogates.harness.device_replay import place, resumable, unanswered_calls


def call(call_id: str) -> dict:
    return {"id": call_id, "type": "function", "function": {"name": "terminal", "arguments": "{}"}}


def event(event_id: int, kind: str, **data) -> SimpleNamespace:
    return SimpleNamespace(id=event_id, type=kind, data=data)


def test_a_call_started_and_never_answered_is_unanswered():
    events = [
        event(1, "llm.response", message={"role": "assistant", "tool_calls": [call("a"), call("b"), call("c")]}),
        event(2, "tool.call", tool_call_id="a"),
        event(3, "tool.result", tool_call_id="a"),
        event(4, "tool.call", tool_call_id="b"),
    ]
    # c never started: it did nothing, and is left to the usual stub.
    assert unanswered_calls(events) == [(4, call("b"))]


def test_a_call_of_an_earlier_response_is_past_resuming():
    events = [
        event(1, "llm.response", message={"tool_calls": [call("a")]}),
        event(2, "tool.call", tool_call_id="a"),
        # The model was answered with a stub and went on.
        event(3, "llm.response", message={"role": "assistant", "content": "done", "tool_calls": None}),
    ]
    assert unanswered_calls(events) == []


def test_a_reused_call_id_is_unanswered_again_from_its_latest_start():
    events = [
        event(1, "llm.response", message={"tool_calls": [call("a")]}),
        event(2, "tool.call", tool_call_id="a"),
        event(3, "tool.result", tool_call_id="a"),
        event(4, "llm.response", message={"tool_calls": [call("a")]}),
        event(5, "tool.call", tool_call_id="a"),
    ]
    assert unanswered_calls(events) == [(5, call("a"))]


def test_a_call_with_no_model_response_is_not_resumed():
    assert unanswered_calls([event(2, "tool.call", tool_call_id="a")]) == []


def test_a_local_folder_session_with_a_call_whose_sibling_answered_has_work():
    events = [
        event(9, "llm.response", message={"tool_calls": [call("a"), call("b")]}),
        event(10, "tool.call", tool_call_id="a"),
        event(11, "tool.call", tool_call_id="b"),
        event(12, "tool.result", tool_call_id="b"),
    ]
    local = SimpleNamespace(config={"execution": {"kind": "device", "device_id": str(uuid4())}})
    assert resumable(local, events)
    assert not resumable(SimpleNamespace(config={}), events)


def test_a_result_goes_after_the_results_already_answering_its_message():
    messages = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "", "tool_calls": [call("a"), call("b")]},
        {"role": "tool", "tool_call_id": "a", "content": "done"},
        {"role": "user", "content": "and then"},
    ]
    place(messages, {"role": "tool", "tool_call_id": "b", "content": "resumed"})
    assert [m.get("tool_call_id") for m in messages] == [None, None, "a", "b", None]
