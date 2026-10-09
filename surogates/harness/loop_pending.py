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

    Nothing else says a command was answered.  Not the cursor: a tool's
    result, or a turn that was refused or failed, moves it past a message
    nobody read.  Not a model's request after it: a command is never the
    model's to read.
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


_REPORT_EVENT_TYPES = frozenset({
    EventType.WORKER_COMPLETE.value,
    EventType.WORKER_FAILED.value,
})
#: The messages the harness writes to give a goal its next turn.
_GOAL_TURN_MESSAGES = frozenset({"outcome_kickoff", "outcome_continuation"})


def _first_unread(events: list[Any], *, goal_in_flight: bool) -> int | None:
    """Return the id of the first event in *events* that still waits for the model to read it.

    A worker's report, and, while a goal is in flight, the message that
    gives the goal its next turn.  A request reads what was written before
    it, so the unread ones are those after the log's last ``llm.request``.
    """
    first: int | None = None
    for event in events:
        event_type = _event_type(event)
        if event_type == EventType.LLM_REQUEST.value:
            first = None
        elif first is None and (
            event_type in _REPORT_EVENT_TYPES
            or goal_in_flight and _gives_a_goal_its_turn(event)
        ):
            first = event.id
    return first


def _gives_a_goal_its_turn(event: Any) -> bool:
    return (
        _event_type(event) == EventType.USER_MESSAGE.value
        and (event.data or {}).get("synthetic") in _GOAL_TURN_MESSAGES
    )


def _goal_turn_waits(events: list[Any]) -> bool:
    """Return True if a goal's next turn is queued in *events* and no model request has read it."""
    waits = False
    for event in events:
        if _event_type(event) == EventType.LLM_REQUEST.value:
            waits = False
        elif _gives_a_goal_its_turn(event):
            waits = True
    return waits
