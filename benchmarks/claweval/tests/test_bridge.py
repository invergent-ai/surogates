import json

import pytest

pytest.importorskip(
    "claw_eval",
    reason="vendored claw-eval not installed in this venv (see README Setup)",
)

from claweval_bench.bridge import load_dispatches, to_messages
from claweval_bench.client import Event


def resp_event(eid, content="", tool_calls=None, in_tok=10, out_tok=5):
    return Event(id=eid, type="llm.response", data={
        "message": {"content": content, "tool_calls": tool_calls or []},
        "input_tokens": in_tok, "output_tokens": out_tok,
    })


def test_conversation_rebuild_roles_and_blocks():
    events = [
        resp_event(1, content="Looking it up.", tool_calls=[{
            "id": "call_1",
            "function": {"name": "config_list", "arguments": '{"status": "active"}'},
        }]),
        Event(id=2, type="tool.result",
              data={"tool_call_id": "call_1", "content": '{"items": []}'}),
        resp_event(3, content="All integrations are healthy."),
    ]
    messages = to_messages(events, question="Check the integrations.")
    assert messages[0].message.role == "user"
    assert messages[0].message.text == "Check the integrations."
    tool_uses = [b for b in messages[1].message.content if b.type == "tool_use"]
    assert tool_uses[0].name == "config_list"
    assert tool_uses[0].input == {"status": "active"}
    assert messages[-1].message.role == "assistant"
    assert "healthy" in messages[-1].message.text
    assert messages[1].usage.input_tokens == 10


def test_empty_responses_are_dropped():
    messages = to_messages([resp_event(1, content="")], question="q")
    assert len(messages) == 1  # just the user question


def test_load_dispatches_roundtrip(tmp_path):
    path = tmp_path / "dispatches.jsonl"
    path.write_text(json.dumps({
        "tool_use_id": "x", "tool_name": "config_list",
        "endpoint_url": "http://localhost:9111/x", "request_body": {},
        "response_status": 200, "response_body": {"ok": True},
        "latency_ms": 12.5,
    }) + "\n")
    dispatches = load_dispatches(path)
    assert dispatches[0].tool_name == "config_list"
    assert dispatches[0].response_status == 200


def test_load_dispatches_missing_file_is_empty(tmp_path):
    assert load_dispatches(tmp_path / "nope.jsonl") == []
