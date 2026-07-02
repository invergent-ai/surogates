"""A ``/stop`` channel message interrupts the running turn out-of-band."""

from surogates.channels.inbound import (
    ChannelInboundPipeline,
    InboundOutcome,
    is_stop_command,
)
from surogates.session.events import EventType

from tests.test_channel_pipeline import (
    SESSION_ID,
    _make_config,
    _make_deps,
    _make_msg,
    _make_routing,
)


def test_is_stop_command():
    assert is_stop_command("/stop")
    assert is_stop_command("  /STOP ")  # leading-space (Slack) + case-insensitive
    assert is_stop_command("/cancel")
    assert not is_stop_command("stop")  # must be the slash form
    assert not is_stop_command("/stop the retries")  # only a bare command
    assert not is_stop_command("")
    assert not is_stop_command(None)


async def _run(deps, text):
    return await ChannelInboundPipeline().handle(
        _make_msg(is_dm=True, identifier="D1", ts="800.0", text=text),
        routing=_make_routing(),
        config=_make_config(),
        deps=deps,
    )


async def test_stop_command_publishes_interrupt_and_suppresses_turn():
    deps = _make_deps()
    nudges: list[str] = []

    async def input_nudge(session_id, msg, text):
        nudges.append(text)

    deps.input_nudge = input_nudge

    result = await _run(deps, "/stop")

    assert result == InboundOutcome.INTERRUPTED
    # interrupt published on the session's channel — out-of-band, so it reaches
    # the busy worker's listener instead of queuing behind the running tool.
    assert deps.redis.published == [
        (f"surogates:interrupt:{SESSION_ID}", "channel_stop"),
    ]
    # NOT enqueued for normal processing, and no USER_MESSAGE emitted.
    assert not deps._enqueued
    assert not any(
        et == EventType.USER_MESSAGE for _, et, _ in deps.session_store.events
    )
    # acked back to the channel.
    assert nudges and "Stopping" in nudges[0]


async def test_leading_space_stop_still_interrupts():
    # Slack users prefix a space so Slack doesn't eat the "/"; the text arrives
    # stripped, so it must still be recognised.
    deps = _make_deps()
    deps.input_nudge = lambda *a: _noop()
    assert await _run(deps, " /stop") == InboundOutcome.INTERRUPTED
    assert deps.redis.published


async def test_normal_message_is_not_interrupted():
    deps = _make_deps()
    result = await _run(deps, "please fix the login bug")
    assert result == InboundOutcome.PROCESSED
    assert deps.redis.published == []
    assert deps._enqueued


async def _noop():
    return None
