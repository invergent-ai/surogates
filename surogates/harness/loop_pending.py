"""Pending-event and slash-command idempotency helpers for the harness loop."""

from __future__ import annotations

from typing import Any

from surogates.devices.browser import for_the_pane_alone, resumes_the_agent, takes_the_browser_over
from surogates.session.events import EventType

_HARNESS_CONTROL_PENDING_EVENT_TYPES = frozenset({
    EventType.HARNESS_RECOVERED.value,
    EventType.HARNESS_WAKE.value,
    # These tell viewers about a wait on the user's computer and give the harness nothing to do.
    EventType.DEVICE_WAITING.value,
    EventType.DEVICE_RESUMED.value,
    # These tell a chat's browser pane that its browser opened, closed or is not there, and that its
    # user took it over, which is to stop the agent.  The hand back is not among them: the cloud's
    # wakes the agent.  A computer's is for the pane too (for_the_pane_alone): the turn a hand back
    # its user confirmed gives is the resume written with it.
    EventType.BROWSER_PROVISIONED.value,
    EventType.BROWSER_DESTROYED.value,
    EventType.BROWSER_UNAVAILABLE.value,
    EventType.BROWSER_CONTROL_GRANTED.value,
})


def _event_type(event: Any) -> str:
    return event.type.value if isinstance(event.type, EventType) else str(event.type)


def _hand_backs_taken_over_again(events: list[Any]) -> set[int]:
    """The resumes a hand back of the browser gave a chat that are news no more: its user took the
    browser over again before any request of the model's had read them.

    The agent would read that the browser tools work again while its user
    holds the browser.  Such a resume is as though it had not been written:
    no work for a wake, no turn, and nothing to read.  The next hand back
    gives its own.
    """
    waiting: list[int] = []
    taken_again: set[int] = set()
    for event in events:
        if _event_type(event) == EventType.LLM_REQUEST.value:
            waiting = []
        elif resumes_the_agent(event):
            waiting.append(event.id)
        elif takes_the_browser_over(event):
            taken_again.update(waiting)
            waiting = []
    return taken_again


def _actionable_pending_events(events: list[Any], cursor: int) -> list[Any]:
    """Return post-cursor events that should start harness work."""
    taken_again = _hand_backs_taken_over_again(events)
    pending = []
    for event in events:
        if (
            event.id is not None
            and event.id > cursor
            and _event_type(event) not in _HARNESS_CONTROL_PENDING_EVENT_TYPES
            and not for_the_pane_alone(event)
            and event.id not in taken_again
        ):
            pending.append(event)
    return pending


def _hand_back_unread(events: list[Any]) -> bool:
    """Whether the resume a hand back of the browser gave the chat waits to be read: none of the
    model's requests came after it, and its user has not taken the browser over again since.

    The cursor cannot tell: one that lands while a turn, or a command's
    wake, is under way is behind the cursor once that moves.  Every model
    request reads the hand backs written before it, live and in replay
    alike (surogates.harness.loop_context_replay.unread_reports).
    """
    unread = False
    for event in events:
        if _event_type(event) == EventType.LLM_REQUEST.value or takes_the_browser_over(event):
            unread = False
        elif resumes_the_agent(event):
            unread = True
    return unread


def _turn_for_a_hand_back(events: list[Any]) -> bool:
    """Whether the turn a wake is about to run is one a hand back of the browser gives the agent.

    No message of the user's waits to be taken, and the resume a hand back
    gave has not been answered.  A wake reads the user's last message to
    run its command; in such a turn that message is not what the wake is
    for, and its command must not run again.

    A hand back opens a turn when it lands with none under way.  A message a
    wake took, with no request of the model's since, is a command the
    harness answered itself, however it answered: done with, so the hand
    back opens a turn then too.  A message no wake has taken yet keeps its
    turn, and its command runs once.  A hand back that lands in a turn the
    model is in opens the next, unless a request of that turn read it.  A
    turn the hand back opened and a dead worker cut off is still its own.
    """
    opened_by: str | None = None
    # Whether a wake took the user's message, and whether the model was asked since.
    taken = asked = False
    # A hand back no model request has read yet.
    unread = False
    taken_again = _hand_backs_taken_over_again(events)
    for event in events:
        if event.id in taken_again:
            continue
        kind = _event_type(event)
        ends_a_turn = kind == EventType.SESSION_COMPLETE.value or (
            # The model's answer ends a turn; its calls for tools do not.
            kind == EventType.LLM_RESPONSE.value
            and not ((getattr(event, "data", None) or {}).get("message") or {}).get("tool_calls")
        )
        if kind == EventType.USER_MESSAGE.value:
            opened_by, taken, asked = "message", False, False
        elif kind == EventType.HARNESS_WAKE.value:
            taken = True
        elif kind == EventType.LLM_REQUEST.value:
            unread, asked = False, True
        elif ends_a_turn:
            opened_by = "hand back" if unread else None
        elif resumes_the_agent(event):
            unread = True
            if opened_by is None or (opened_by == "message" and taken and not asked):
                opened_by = "hand back"
    return opened_by == "hand back"


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
