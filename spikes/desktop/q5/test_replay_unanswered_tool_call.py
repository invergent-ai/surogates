"""What replay rebuilds for a tool call that never got a result.

Characterizes current behaviour for the spike; not a regression test.
"""

from __future__ import annotations

from types import SimpleNamespace

from surogates.harness.loop import AgentHarness
from surogates.session.events import EventType
from tests.test_wake_stranded_user_message import _harness


def _ev(event_id: int, etype: EventType, data: dict) -> SimpleNamespace:
    return SimpleNamespace(id=event_id, type=etype.value, data=data)


def test_unanswered_tool_call_is_left_dangling() -> None:
    call = {
        "id": "call_1",
        "type": "function",
        "function": {"name": "write_file", "arguments": '{"path": "a.txt", "content": "x"}'},
    }
    events = [
        _ev(1, EventType.USER_MESSAGE, {"content": "write a.txt"}),
        _ev(2, EventType.LLM_REQUEST, {}),
        _ev(3, EventType.LLM_RESPONSE, {"message": {"role": "assistant", "content": None, "tool_calls": [call]}}),
        _ev(4, EventType.TOOL_CALL, {"name": "write_file", "arguments": call["function"]["arguments"], "tool_call_id": "call_1"}),
    ]
    messages = AgentHarness._rebuild_messages(_harness(store=None), events)
    print(messages)
    assert messages[-1]["role"] == "assistant"
    assert messages[-1]["tool_calls"][0]["id"] == "call_1"
    assert not any(m.get("role") == "tool" for m in messages)
