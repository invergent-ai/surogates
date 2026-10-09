"""Pending-event and slash-command idempotency helpers for the harness loop."""

from __future__ import annotations

from typing import Any

from surogates.session.events import EventType

_HARNESS_CONTROL_PENDING_EVENT_TYPES = frozenset({
    EventType.HARNESS_RECOVERED.value,
    EventType.HARNESS_WAKE.value,
    # These tell viewers about a wait on the user's computer and give the harness nothing to do.
    EventType.DEVICE_WAITING.value,
    EventType.DEVICE_RESUMED.value,
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


def _event_type(event: Any) -> str:
    return event.type.value if isinstance(event.type, EventType) else str(event.type)


def _taken_up(events: list[Any], typed_at: int, cursor: int) -> bool:
    """Return True if a wake has taken up the message the user typed at event *typed_at*.

    The cursor is at or past the message, and after it a wake began or the
    model was asked.  The cursor alone cannot tell: a turn refused or failed
    before any wake read the message moves the cursor past it too, and what
    the message asked for is still to do.
    """
    if typed_at > cursor:
        return False
    return any(
        event.id is not None
        and event.id > typed_at
        and _event_type(event) in (EventType.HARNESS_WAKE.value, EventType.LLM_REQUEST.value)
        for event in events
    )


#: What the harness writes to answer a command of the user's itself, with no
#: model turn: its own words, or the result of the coding run the command was.
_COMMAND_ANSWER_EVENT_TYPES = frozenset({
    EventType.LLM_RESPONSE.value,
    EventType.CODE_RUN_RESULT.value,
})


def _command_answered(events: list[Any], typed_at: int) -> bool:
    """Return True if the harness has answered the command the user typed at event *typed_at*.

    The wake that takes a command up writes ``harness.wake`` and then the
    command's answer, with no request to the model between them.  So an
    answer after a wake after the message is that command's.  An answer
    with no wake before it belongs to an earlier command, still being
    answered when this one was typed, and one after a model's request is
    the model's word in a turn of its own.
    """
    taken_up = False
    for event in events:
        if event.id is None or event.id <= typed_at:
            continue
        event_type = _event_type(event)
        if event_type == EventType.HARNESS_WAKE.value:
            taken_up = True
        elif event_type == EventType.LLM_REQUEST.value:
            taken_up = False
        elif taken_up and event_type in _COMMAND_ANSWER_EVENT_TYPES:
            return True
    return False
