"""Tests for the pure _build_channel_payload helper in store.py.

Verifies that:
- LLM_RESPONSE rows with tool_calls are tagged intermediate=True.
- LLM_RESPONSE rows without tool_calls are tagged intermediate=False.
- Tool-call-only responses (empty content) yield an empty payload.
- INBOX_INPUT_REQUIRED is only delivered to Slack, not to other channels.
"""

from surogates.session.events import EventType
from surogates.session.store import _build_channel_payload


def test_intermediate_llm_response_tagged_intermediate():
    data = {"message": {"content": "Let me try X", "tool_calls": [{"id": "t1"}]}}
    p = _build_channel_payload(EventType.LLM_RESPONSE, data, "slack")
    assert p["content"] == "Let me try X"
    assert p["intermediate"] is True


def test_final_llm_response_tagged_not_intermediate():
    data = {"message": {"content": "Here is the answer", "tool_calls": []}}
    p = _build_channel_payload(EventType.LLM_RESPONSE, data, "slack")
    assert p["content"] == "Here is the answer"
    assert p["intermediate"] is False


def test_tool_call_only_response_yields_empty_payload():
    data = {"message": {"content": "", "tool_calls": [{"id": "t1"}]}}
    assert _build_channel_payload(EventType.LLM_RESPONSE, data, "slack") == {}


def test_input_required_payload_slack_only():
    data = {"questions": [{"q": "?"}], "tool_call_id": "t1", "context": "ctx"}
    assert _build_channel_payload(EventType.INBOX_INPUT_REQUIRED, data, "slack")["input_prompt"] is True
    assert _build_channel_payload(EventType.INBOX_INPUT_REQUIRED, data, "telegram") == {}
