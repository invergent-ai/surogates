"""Tests: channel-backfill lazy seed wired into the inbound pipeline.

Covers two real behaviours:
1. A first non-DM Slack channel message triggers backfill exactly once, with
   the session_id returned by get_or_create, BEFORE the USER_MESSAGE event.
2. A DM message does NOT call backfill (is_dm guard).
"""

from __future__ import annotations

import pytest

from surogates.channels.inbound import ChannelInboundPipeline, InboundOutcome
from surogates.session.events import EventType

# Reuse the existing channel-pipeline fakes rather than duplicating the full
# dependency harness.
from tests.test_channel_pipeline import (  # noqa: PLC2701
    SESSION_ID,
    _make_config,
    _make_deps,
    _make_msg,
    _make_routing,
)


# ---------------------------------------------------------------------------
# Test 1: first non-DM Slack channel message triggers backfill BEFORE USER_MESSAGE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_slack_channel_message_triggers_backfill_before_user_event():
    """Backfill is awaited once, with the correct session_id, before USER_MESSAGE.

    Assertions:
    (a) The backfill callable was awaited exactly once.
    (b) The session_id passed to backfill equals the one get_or_create returned.
    (c) Ordering: the backfill call is recorded in the shared `order` list BEFORE
        the USER_MESSAGE emit — so index(backfill) < index(USER_MESSAGE emit).
    """
    order: list[tuple] = []

    deps = _make_deps()

    # Wrap the real emit_event so we record events in the shared order list.
    original_emit = deps.session_store.emit_event

    async def recording_emit(session_id, event_type, data):
        order.append(("emit", session_id, event_type))
        await original_emit(session_id, event_type, data)

    deps.session_store.emit_event = recording_emit

    # The backfill callable records into the same order list.
    backfill_calls: list[dict] = []

    async def fake_backfill(session_id, channel_id, routing):
        order.append(("backfill", session_id, channel_id, routing.identifier))
        backfill_calls.append(
            {"session_id": session_id, "channel_id": channel_id}
        )
        return 999

    deps.backfill = fake_backfill

    msg = _make_msg(is_dm=False, is_mention=True, identifier="C1", ts="50.0")
    result = await ChannelInboundPipeline().handle(
        msg,
        routing=_make_routing(),
        config=_make_config(require_mention=True),
        deps=deps,
    )

    # (a) Outcome and await count.
    assert result == InboundOutcome.PROCESSED
    assert len(backfill_calls) == 1, (
        f"backfill must be awaited exactly once; got {len(backfill_calls)} calls"
    )

    # (b) The session_id passed to backfill matches what get_or_create returned.
    assert backfill_calls[0]["session_id"] == SESSION_ID, (
        f"backfill received session_id={backfill_calls[0]['session_id']!r}, "
        f"expected {SESSION_ID!r}"
    )

    # (c) Ordering: find the backfill entry and the USER_MESSAGE emit entry.
    backfill_index = next(
        i for i, item in enumerate(order) if item[0] == "backfill"
    )
    user_message_emit_index = next(
        i for i, item in enumerate(order)
        if item[0] == "emit" and item[2] == EventType.USER_MESSAGE
    )
    assert backfill_index < user_message_emit_index, (
        f"backfill (index {backfill_index}) must precede USER_MESSAGE emit "
        f"(index {user_message_emit_index}); order={order!r}"
    )

    # Also verify the backfill received the channel_id from msg.identifier.
    assert backfill_calls[0]["channel_id"] == "C1"


# ---------------------------------------------------------------------------
# Test 2: DM messages do NOT trigger backfill
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dm_message_does_not_trigger_backfill():
    """is_dm=True → backfill must not be called, even when deps.backfill is set.

    The guard `not msg.is_dm` must prevent the seed from running on DMs.
    """
    backfill_calls: list[tuple] = []

    deps = _make_deps()

    async def fake_backfill(session_id, channel_id, routing):
        backfill_calls.append((session_id, channel_id))
        return 999

    deps.backfill = fake_backfill

    msg = _make_msg(is_dm=True, is_mention=False, identifier="D1", ts="51.0")
    result = await ChannelInboundPipeline().handle(
        msg,
        routing=_make_routing(),
        config=_make_config(),
        deps=deps,
    )

    assert result == InboundOutcome.PROCESSED
    assert backfill_calls == [], (
        f"backfill must NOT be called for DMs; got calls: {backfill_calls!r}"
    )


# ---------------------------------------------------------------------------
# Test 3: backfill=None (default) keeps existing pipeline green
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_none_backfill_does_not_crash_pipeline():
    """deps.backfill=None (the default) — pipeline continues to work normally.

    This is the regression guard: existing tests don't pass backfill, so None
    must be the safe default that skips the seed without any AttributeError.
    """
    deps = _make_deps()
    # backfill is NOT set (should default to None on PipelineDeps)
    assert deps.backfill is None, "PipelineDeps.backfill must default to None"

    msg = _make_msg(is_dm=True, ts="52.0")
    result = await ChannelInboundPipeline().handle(
        msg,
        routing=_make_routing(),
        config=_make_config(),
        deps=deps,
    )

    assert result == InboundOutcome.PROCESSED
