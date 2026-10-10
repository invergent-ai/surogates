from unittest.mock import AsyncMock

import pytest

from surogates.channels.inbound import ChannelInboundPipeline, InboundOutcome
from surogates.session.events import EventType

from tests.test_channel_pipeline import (
    SESSION_ID,
    _make_config,
    _make_deps,
    _make_msg,
    _make_routing,
    _Routing,
)


async def test_slack_pending_input_reply_gets_nudge_and_suppresses_turn():
    deps = _make_deps()
    pending_calls = []
    nudge_calls = []

    async def pending_input(session_id):
        pending_calls.append(session_id)
        return {"tool_call_id": "tc1", "questions": [{"prompt": "q"}], "context": ""}

    async def input_nudge(session_id, msg, text):
        nudge_calls.append((session_id, msg.identifier, msg.thread_key, text))

    deps.pending_input = pending_input
    deps.input_nudge = input_nudge

    msg = _make_msg(is_dm=True, identifier="D1", thread_key=None, ts="700.0")
    result = await ChannelInboundPipeline().handle(
        msg,
        routing=_make_routing(),
        config=_make_config(),
        deps=deps,
    )

    assert result == InboundOutcome.DROPPED
    assert pending_calls == [SESSION_ID]
    assert nudge_calls
    assert "Answer" in nudge_calls[0][3]
    assert not deps._enqueued
    assert not any(event_type == EventType.USER_MESSAGE for _, event_type, _ in deps.session_store.events)


async def test_no_pending_input_preserves_normal_turn():
    deps = _make_deps()
    nudge_calls = []

    async def pending_input(session_id):
        return None

    async def input_nudge(session_id, msg, text):
        nudge_calls.append((session_id, text))

    deps.pending_input = pending_input
    deps.input_nudge = input_nudge

    msg = _make_msg(is_dm=True, identifier="D1", ts="701.0")
    result = await ChannelInboundPipeline().handle(
        msg,
        routing=_make_routing(),
        config=_make_config(),
        deps=deps,
    )

    assert result == InboundOutcome.PROCESSED
    assert nudge_calls == []
    assert deps._enqueued
    assert any(event_type == EventType.USER_MESSAGE for _, event_type, _ in deps.session_store.events)


@pytest.mark.parametrize("platform, text", [
    ("slack", "/goal status"),
    # Slack takes a message that starts with "/" for its own command: its users type a space first.
    ("slack", " /goal status"),
    ("telegram", "/goal status"),
    ("whatsapp", "/goal status"),
])
async def test_a_command_typed_while_a_question_waits_goes_on_as_a_message(monkeypatch, platform, text):
    deps = _make_deps()
    nudges = []

    async def pending_input(session_id):
        return {"tool_call_id": "tc1", "questions": [{"prompt": "Which quarter?"}], "context": ""}

    async def input_nudge(session_id, msg, said):
        nudges.append(said)

    deps.pending_input = pending_input
    deps.input_nudge = input_nudge
    resolve = AsyncMock(return_value=True)
    monkeypatch.setattr("surogates.session.interactive_input.resolve_input_response", resolve)

    result = await ChannelInboundPipeline().handle(
        _make_msg(is_dm=True, text=text, ts="702.0"),
        routing=_Routing(platform=platform, identifier="A0APP"),
        config=_make_config(),
        deps=deps,
    )

    # Never the question's answer, nor held back for it: written as its user's message, whose
    # command the question's wait reads, and its wake runs.
    assert (result, nudges) == (InboundOutcome.PROCESSED, [])
    resolve.assert_not_awaited()
    said = [data for _, event_type, data in deps.session_store.events if event_type == EventType.USER_MESSAGE]
    assert [data["content"] for data in said] == [text]
    assert deps._enqueued
