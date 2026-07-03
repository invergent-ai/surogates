"""A CODE_RUN_CHANNEL_UPDATE event is delivered to channels; PROGRESS is not."""

from surogates.session.events import EventType
from surogates.session.store import _DELIVERABLE_EVENTS, _build_channel_payload


def test_channel_update_is_gated_as_deliverable():
    # emit_event only enqueues an outbox item for types in this set — the
    # payload builder handling the type is not enough (this was the miss).
    assert EventType.CODE_RUN_CHANNEL_UPDATE in _DELIVERABLE_EVENTS


def test_channel_update_delivered_as_message():
    p = _build_channel_payload(
        EventType.CODE_RUN_CHANNEL_UPDATE, {"text": "🛠️ Still working…"}, "slack",
    )
    assert p == {"content": "🛠️ Still working…"}
    # not marked intermediate → delivered, not suppressed/folded
    assert "intermediate" not in p


def test_channel_update_empty_text_not_delivered():
    assert _build_channel_payload(EventType.CODE_RUN_CHANNEL_UPDATE, {}, "slack") == {}


def test_code_run_progress_still_not_delivered_to_channels():
    # The live streaming progress remains web-UI-only.
    assert _build_channel_payload(
        EventType.CODE_RUN_PROGRESS, {"chunk": "editing"}, "slack",
    ) == {}
