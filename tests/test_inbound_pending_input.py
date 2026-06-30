from surogates.channels.inbound import ChannelInboundPipeline, InboundOutcome
from surogates.session.events import EventType

from tests.test_channel_pipeline import (
    SESSION_ID,
    _make_config,
    _make_deps,
    _make_msg,
    _make_routing,
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
