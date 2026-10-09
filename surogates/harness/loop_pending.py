"""Pending-event and slash-command idempotency helpers for the harness loop."""

from __future__ import annotations

from typing import Any

from surogates.browser.control import HANDED_BACK_FROM
from surogates.session.events import EventType

_HARNESS_CONTROL_PENDING_EVENT_TYPES = frozenset({
    EventType.HARNESS_RECOVERED.value,
    EventType.HARNESS_WAKE.value,
    # These tell viewers about a wait on the user's computer and give the harness nothing to do.
    EventType.DEVICE_WAITING.value,
    EventType.DEVICE_RESUMED.value,
    # These tell a chat's browser pane that its browser opened, closed or is not there, and that its
    # user took it over, which is to stop the agent.  The hand back is not among them: it is what
    # wakes the agent to go on.
    EventType.BROWSER_PROVISIONED.value,
    EventType.BROWSER_DESTROYED.value,
    EventType.BROWSER_UNAVAILABLE.value,
    EventType.BROWSER_CONTROL_GRANTED.value,
})


def _handed_back_elsewhere(event_type: str, event: Any) -> bool:
    """Whether an event tells a chat that its browser was handed back from another chat: told for its
    pane, as the take-over was.  The agent goes on in the chat its user handed it back from."""
    return event_type == EventType.BROWSER_CONTROL_RETURNED.value and HANDED_BACK_FROM in (
        getattr(event, "data", None) or {}
    )


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
            and not _handed_back_elsewhere(event_type, event)
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
