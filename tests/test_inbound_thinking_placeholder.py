"""Tests: thinking-placeholder progress wired into the inbound pipeline.

Covers two behaviours:
1. A processed Slack message calls deps.progress exactly once, with the correct
   session_id / channel / thread_ts args, AFTER the USER_MESSAGE emit and
   BEFORE enqueue_session.
2. A non-Slack (telegram) processed message does NOT call deps.progress.
3. A progress failure (RuntimeError) does NOT block enqueue — the session still
   gets queued.
"""

from __future__ import annotations

from surogates.channels.inbound import ChannelInboundPipeline, InboundOutcome
from surogates.session.events import EventType

from tests.test_channel_pipeline import (  # noqa: PLC2701
    SESSION_ID,
    _make_config,
    _make_deps,
    _make_msg,
    _make_routing,
)


# ---------------------------------------------------------------------------
# Test 1: Slack processed message → progress called once, in the right order
# ---------------------------------------------------------------------------


async def test_slack_processed_message_runs_progress_after_user_event_before_enqueue():
    """Progress is awaited once with correct args, AFTER USER_MESSAGE emit, BEFORE enqueue.

    Three shared assertions:
    (a) progress_calls == [(SESSION_ID, channel_id, thread_ts)] — called exactly
        once with the right args.
    (b) Ordering: emit(USER_MESSAGE) index < progress index < enqueue index.
    (c) Outcome is PROCESSED and the session was enqueued.
    """
    order: list[tuple] = []
    deps = _make_deps()

    # Wrap emit_event so we record the USER_MESSAGE emit into the shared order list.
    original_emit = deps.session_store.emit_event

    async def recording_emit(session_id, event_type, data):
        order.append(("emit", session_id, event_type))
        await original_emit(session_id, event_type, data)

    deps.session_store.emit_event = recording_emit

    # Wrap enqueue_session so we record the enqueue into the shared order list.
    original_enqueue = deps.enqueue_session

    async def recording_enqueue(redis, *, org_id, agent_id, session_id):
        order.append(("enqueue", session_id))
        await original_enqueue(redis, org_id=org_id, agent_id=agent_id, session_id=session_id)

    deps.enqueue_session = recording_enqueue

    # progress callable: records into both the shared order list and its own list.
    progress_calls: list[tuple] = []

    async def progress(session_id, channel_id, thread_ts):
        order.append(("progress", session_id, channel_id, thread_ts))
        progress_calls.append((session_id, channel_id, thread_ts))

    deps.progress = progress

    msg = _make_msg(
        is_dm=False,
        is_mention=True,
        identifier="C1",
        thread_key="50.0",
        ts="51.0",
    )
    result = await ChannelInboundPipeline().handle(
        msg,
        routing=_make_routing(),
        config=_make_config(require_mention=True),
        deps=deps,
    )

    # (c) Outcome + enqueued.
    assert result == InboundOutcome.PROCESSED
    assert deps._enqueued

    # (a) progress called exactly once with the right args.
    assert progress_calls == [(SESSION_ID, "C1", "50.0")], (
        f"progress must be called once with (SESSION_ID, 'C1', '50.0'); got {progress_calls!r}"
    )

    # (b) Ordering: USER_MESSAGE emit → progress → enqueue.
    emit_index = next(
        i for i, item in enumerate(order)
        if item[0] == "emit" and item[2] == EventType.USER_MESSAGE
    )
    progress_index = next(i for i, item in enumerate(order) if item[0] == "progress")
    enqueue_index = next(i for i, item in enumerate(order) if item[0] == "enqueue")

    assert emit_index < progress_index, (
        f"progress (index {progress_index}) must come AFTER USER_MESSAGE emit "
        f"(index {emit_index}); order={order!r}"
    )
    assert progress_index < enqueue_index, (
        f"progress (index {progress_index}) must come BEFORE enqueue "
        f"(index {enqueue_index}); order={order!r}"
    )


# ---------------------------------------------------------------------------
# Test 2: Non-Slack (telegram) processed message → progress NOT called
# ---------------------------------------------------------------------------


async def test_non_slack_processed_message_does_not_run_progress():
    """routing.platform == 'telegram' → deps.progress must not be called.

    The gate is routing.platform == 'slack', so telegram messages skip
    the progress hook entirely, even when deps.progress is set.
    """
    deps = _make_deps()
    progress_calls: list[tuple] = []

    async def progress(session_id, channel_id, thread_ts):
        progress_calls.append((session_id, channel_id, thread_ts))

    deps.progress = progress

    routing = _make_routing()
    routing.platform = "telegram"

    msg = _make_msg(is_dm=True, identifier="CHAT1", ts="52.0")
    result = await ChannelInboundPipeline().handle(
        msg,
        routing=routing,
        config=_make_config(),
        deps=deps,
    )

    assert result == InboundOutcome.PROCESSED
    assert progress_calls == [], (
        f"progress must NOT be called for non-Slack platforms; got {progress_calls!r}"
    )
    assert deps._enqueued


# ---------------------------------------------------------------------------
# Test 3: Progress failure does NOT block enqueue (best-effort)
# ---------------------------------------------------------------------------


async def test_progress_failure_does_not_block_enqueue():
    """A RuntimeError from progress must be swallowed; enqueue still happens.

    The inbound hook wraps the progress call in try/except Exception so that
    a Slack API failure (or any other runtime error) never prevents the session
    from being queued for the worker.
    """
    deps = _make_deps()
    progress_calls: list[tuple] = []

    async def progress(session_id, channel_id, thread_ts):
        progress_calls.append((session_id, channel_id, thread_ts))
        raise RuntimeError("slack unavailable")

    deps.progress = progress

    # Use is_dm=True so the Slack platform guard passes (DMs are on slack too;
    # routing.platform defaults to "slack" in _make_routing()).
    msg = _make_msg(is_dm=True, identifier="D1", ts="53.0")
    result = await ChannelInboundPipeline().handle(
        msg,
        routing=_make_routing(),
        config=_make_config(),
        deps=deps,
    )

    assert result == InboundOutcome.PROCESSED
    # progress WAS called (once) — it just raised an exception.
    assert progress_calls == [(SESSION_ID, "D1", None)], (
        f"progress must be called once (then exception swallowed); got {progress_calls!r}"
    )
    assert deps._enqueued, "enqueue must still happen after a progress failure"
