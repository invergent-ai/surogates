"""_rebuild_messages re-sequences a mid-iteration user.message to the boundary."""
from __future__ import annotations

from types import SimpleNamespace

from surogates.session.events import EventType


def _event(etype: EventType, data: dict, eid: int):
    return SimpleNamespace(type=etype.value, data=data, id=eid)


def _rebuild(events):
    from surogates.harness.loop_context_replay import ContextReplayMixin

    host = type("_H", (ContextReplayMixin,), {})()
    return host._rebuild_messages(events)


def _assistant_with_tool_call(tc_id: str):
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": tc_id,
            "type": "function",
            "function": {"name": "read_file", "arguments": "{}"},
        }],
    }


def test_user_message_between_toolcall_and_result_moves_to_iteration_close():
    # llm.request, assistant(tool_call X), user(steer), tool.result(X)
    events = [
        _event(EventType.USER_MESSAGE, {"content": "do it"}, 1),
        _event(EventType.LLM_REQUEST, {"iteration": 1}, 2),
        _event(EventType.LLM_RESPONSE, {"message": _assistant_with_tool_call("X")}, 3),
        _event(EventType.USER_MESSAGE, {"content": "also check Z"}, 4),
        _event(EventType.TOOL_RESULT, {"tool_call_id": "X", "content": "ok"}, 5),
    ]
    roles = [(m["role"], m.get("tool_call_id")) for m in _rebuild(events)]
    # assistant(tool_calls) MUST be immediately followed by its tool result,
    # and the steer message lands AFTER the tool result.
    assert roles == [
        ("user", None),       # "do it"
        ("assistant", None),  # tool_call X
        ("tool", "X"),        # result for X
        ("user", None),       # "also check Z" — deferred to the close
    ]
    assert _rebuild(events)[-1]["content"] == "also check Z"


def test_user_message_mid_stream_before_response_defers_past_tool_result():
    # user arrives between llm.request and llm.response of a tool-calling iter
    events = [
        _event(EventType.USER_MESSAGE, {"content": "do it"}, 1),
        _event(EventType.LLM_REQUEST, {"iteration": 1}, 2),
        _event(EventType.USER_MESSAGE, {"content": "steer"}, 3),
        _event(EventType.LLM_RESPONSE, {"message": _assistant_with_tool_call("X")}, 4),
        _event(EventType.TOOL_RESULT, {"tool_call_id": "X", "content": "ok"}, 5),
    ]
    roles = [m["role"] for m in _rebuild(events)]
    assert roles == ["user", "assistant", "tool", "user"]


def test_user_message_at_clean_boundary_keeps_natural_placement():
    # final response has no tool calls; next user message is a normal new turn
    events = [
        _event(EventType.USER_MESSAGE, {"content": "q1"}, 1),
        _event(EventType.LLM_REQUEST, {"iteration": 1}, 2),
        _event(EventType.LLM_RESPONSE, {"message": {"role": "assistant", "content": "a1"}}, 3),
        _event(EventType.USER_MESSAGE, {"content": "q2"}, 4),
    ]
    roles = [m["role"] for m in _rebuild(events)]
    assert roles == ["user", "assistant", "user"]


def test_two_steer_messages_in_one_window_are_coalesced():
    events = [
        _event(EventType.USER_MESSAGE, {"content": "do it"}, 1),
        _event(EventType.LLM_REQUEST, {"iteration": 1}, 2),
        _event(EventType.LLM_RESPONSE, {"message": _assistant_with_tool_call("X")}, 3),
        _event(EventType.USER_MESSAGE, {"content": "first"}, 4),
        _event(EventType.USER_MESSAGE, {"content": "second"}, 5),
        _event(EventType.TOOL_RESULT, {"tool_call_id": "X", "content": "ok"}, 6),
    ]
    msgs = _rebuild(events)
    assert msgs[-1] == {"role": "user", "content": "first\n\nsecond"}
    # exactly one trailing user message (coalesced), not two
    assert [m["role"] for m in msgs] == ["user", "assistant", "tool", "user"]


def test_multi_tool_iteration_flushes_only_after_last_result():
    events = [
        _event(EventType.USER_MESSAGE, {"content": "do it"}, 1),
        _event(EventType.LLM_REQUEST, {"iteration": 1}, 2),
        _event(EventType.LLM_RESPONSE, {"message": {
            "role": "assistant", "content": "",
            "tool_calls": [
                {"id": "A", "type": "function", "function": {"name": "t", "arguments": "{}"}},
                {"id": "B", "type": "function", "function": {"name": "t", "arguments": "{}"}},
            ],
        }}, 3),
        _event(EventType.USER_MESSAGE, {"content": "steer"}, 4),
        _event(EventType.TOOL_RESULT, {"tool_call_id": "A", "content": "ok"}, 5),
        _event(EventType.TOOL_RESULT, {"tool_call_id": "B", "content": "ok"}, 6),
    ]
    roles = [m["role"] for m in _rebuild(events)]
    assert roles == ["user", "assistant", "tool", "tool", "user"]


def test_synthetic_user_message_is_not_deferred():
    # a synthetic nudge keeps its natural placement even mid-iteration
    events = [
        _event(EventType.USER_MESSAGE, {"content": "do it"}, 1),
        _event(EventType.LLM_REQUEST, {"iteration": 1}, 2),
        _event(EventType.LLM_RESPONSE, {"message": _assistant_with_tool_call("X")}, 3),
        _event(EventType.USER_MESSAGE, {"content": "nudge", "synthetic": "x"}, 4),
        _event(EventType.TOOL_RESULT, {"tool_call_id": "X", "content": "ok"}, 5),
    ]
    # synthetic appended inline -> lands between assistant and tool result.
    # (Synthetic nudges are only ever emitted at real boundaries in practice;
    # this asserts we do not change their handling.)
    roles = [m["role"] for m in _rebuild(events)]
    assert roles == ["user", "assistant", "user", "tool"]


def test_log_ending_mid_iteration_flushes_buffer():
    events = [
        _event(EventType.USER_MESSAGE, {"content": "do it"}, 1),
        _event(EventType.LLM_REQUEST, {"iteration": 1}, 2),
        _event(EventType.LLM_RESPONSE, {"message": _assistant_with_tool_call("X")}, 3),
        _event(EventType.USER_MESSAGE, {"content": "steer"}, 4),
        # no tool.result yet — wake interrupted
    ]
    # buffer must still flush so the steer message is not lost
    assert _rebuild(events)[-1]["content"] == "steer"
