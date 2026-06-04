"""Pending-event and slash-command idempotency helpers for the harness loop."""

from __future__ import annotations

from typing import Any

from surogates.session.events import EventType

_HARNESS_CONTROL_PENDING_EVENT_TYPES = frozenset({
    EventType.HARNESS_RECOVERED.value,
    EventType.HARNESS_WAKE.value,
})


def _actionable_pending_events(events: list[Any], cursor: int) -> list[Any]:
    """Return post-cursor events that should start harness work."""
    pending = []
    for event in events:
        event_type = (
            event.type.value
            if isinstance(event.type, EventType)
            else str(event.type)
        )
        if (
            event.id is not None
            and event.id > cursor
            and event_type not in _HARNESS_CONTROL_PENDING_EVENT_TYPES
        ):
            pending.append(event)
    return pending


def _slash_loop_already_processed(events: list[Any]) -> bool:
    """Return True if the latest ``/loop`` user message has already been answered.

    ``_handle_loop_command`` emits exactly one ``LLM_RESPONSE`` via
    ``_emit_loop_response`` per run, so an ``LLM_RESPONSE`` whose id sits
    after the latest ``USER_MESSAGE`` proves the command has already been
    processed.  Used to skip duplicate schedule creation when the harness
    wakes a second time on the same ``/loop`` message — e.g. when the
    orphan sweeper re-enqueues a finished session.
    """
    latest_user_msg_id: int | None = None
    for event in events:
        event_type = (
            event.type.value
            if isinstance(event.type, EventType)
            else str(event.type)
        )
        if event_type == EventType.USER_MESSAGE.value and event.id is not None:
            if latest_user_msg_id is None or event.id > latest_user_msg_id:
                latest_user_msg_id = event.id
    if latest_user_msg_id is None:
        return False
    for event in events:
        event_type = (
            event.type.value
            if isinstance(event.type, EventType)
            else str(event.type)
        )
        if (
            event_type == EventType.LLM_RESPONSE.value
            and event.id is not None
            and event.id > latest_user_msg_id
        ):
            return True
    return False
