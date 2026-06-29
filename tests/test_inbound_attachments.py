"""Tests: Slack file attachments ingested through the inbound pipeline.

Covers:
1. A Slack message with files calls deps.attachments AFTER backfill,
   BEFORE USER_MESSAGE emit; the event carries images/attachments and
   the note is folded into content.
2. A raising deps.attachments never drops the USER_MESSAGE.
3. deps.attachments=None (default) leaves the event shape unchanged.
"""

from __future__ import annotations

from dataclasses import replace

from surogates.channels.inbound import (
    ChannelInboundPipeline,
    InboundFileRef,
    InboundOutcome,
)
from surogates.session.events import EventType

from tests.test_channel_pipeline import (  # noqa: PLC2701
    SESSION_ID,
    _make_config,
    _make_deps,
    _make_msg,
    _make_routing,
)


def _file() -> InboundFileRef:
    return InboundFileRef(
        url="https://files.slack.com/p1",
        filename="pic.png",
        mime_type="image/png",
        size=123,
    )


# ---------------------------------------------------------------------------
# Test 1: attachments called after backfill, before USER_MESSAGE; payload merged
# ---------------------------------------------------------------------------


async def test_slack_files_run_attachments_after_backfill_before_user_event():
    """Ordering: backfill < attachments < USER_MESSAGE emit.

    Also verifies that the returned images/attachments are merged into the
    event data and the note is appended to the content field.
    """
    order: list[tuple] = []
    deps = _make_deps()

    original_emit = deps.session_store.emit_event

    async def recording_emit(session_id, event_type, data):
        order.append(("emit", session_id, event_type))
        await original_emit(session_id, event_type, data)

    deps.session_store.emit_event = recording_emit

    async def backfill(session_id, channel_id, routing):
        order.append(("backfill", session_id, channel_id))
        return None

    async def attachments(session_id, msg):
        order.append(("attachments", session_id, msg.identifier))
        return {
            "images": [{"data": "abc", "mime_type": "image/png"}],
            "attachments": [
                {
                    "path": "uploads/slack/100.0-1-notes.txt",
                    "filename": "notes.txt",
                    "mime_type": "text/plain",
                    "size": 5,
                    "inlined_text": "hello",
                    "inlined_render_kind": "text",
                }
            ],
            "note": "[shared file(s) not read: skipped.bin]",
        }

    deps.backfill = backfill
    deps.attachments = attachments

    msg = replace(
        _make_msg(is_dm=False, is_mention=True, identifier="C1", ts="100.0"),
        files=[_file()],
    )
    result = await ChannelInboundPipeline().handle(
        msg,
        routing=_make_routing(),
        config=_make_config(require_mention=True),
        deps=deps,
    )

    assert result == InboundOutcome.PROCESSED

    # The USER_MESSAGE event must carry the merged attachment payload.
    ev = next(e for e in deps.session_store.events if e[1] == EventType.USER_MESSAGE)
    data = ev[2]
    assert data["images"] == [{"data": "abc", "mime_type": "image/png"}]
    assert data["attachments"][0]["path"] == "uploads/slack/100.0-1-notes.txt"
    # Note folded into content.
    assert "[shared file(s) not read: skipped.bin]" in data["content"]

    # Ordering: backfill < attachments < USER_MESSAGE emit.
    backfill_index = next(i for i, item in enumerate(order) if item[0] == "backfill")
    attachments_index = next(i for i, item in enumerate(order) if item[0] == "attachments")
    emit_index = next(
        i for i, item in enumerate(order)
        if item[0] == "emit" and item[2] == EventType.USER_MESSAGE
    )
    assert backfill_index < attachments_index < emit_index, (
        f"Expected backfill < attachments < USER_MESSAGE emit; order={order!r}"
    )

    # The attachments callable received the correct session_id.
    att_entry = next(item for item in order if item[0] == "attachments")
    assert att_entry[1] == SESSION_ID


# ---------------------------------------------------------------------------
# Test 2: raising attachments never drops USER_MESSAGE
# ---------------------------------------------------------------------------


async def test_attachment_failure_still_emits_user_message():
    """Best-effort: a failing deps.attachments must not drop the user's message."""
    deps = _make_deps()

    async def attachments(session_id, msg):
        raise RuntimeError("slack download failed")

    deps.attachments = attachments
    msg = replace(_make_msg(is_dm=True, identifier="D1", ts="101.0"), files=[_file()])

    result = await ChannelInboundPipeline().handle(
        msg,
        routing=_make_routing(),
        config=_make_config(),
        deps=deps,
    )

    assert result == InboundOutcome.PROCESSED
    ev = next(e for e in deps.session_store.events if e[1] == EventType.USER_MESSAGE)
    # Original text still there; no images/attachments injected.
    assert ev[2]["content"] == "hello"
    assert "images" not in ev[2]
    assert "attachments" not in ev[2]


# ---------------------------------------------------------------------------
# Test 3: deps.attachments=None (default) leaves event shape unchanged
# ---------------------------------------------------------------------------


async def test_none_attachments_keeps_existing_event_shape():
    """When deps.attachments is None (default), the event carries no injected media."""
    deps = _make_deps()
    assert deps.attachments is None

    msg = replace(_make_msg(is_dm=True, identifier="D1", ts="102.0"), files=[_file()])

    result = await ChannelInboundPipeline().handle(
        msg,
        routing=_make_routing(),
        config=_make_config(),
        deps=deps,
    )

    assert result == InboundOutcome.PROCESSED
    ev = next(e for e in deps.session_store.events if e[1] == EventType.USER_MESSAGE)
    assert ev[2]["content"] == "hello"
    assert "images" not in ev[2]
    assert "attachments" not in ev[2]
