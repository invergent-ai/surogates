"""Pending-event and slash-command idempotency helpers for the harness loop."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
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


def _command_answered(events: list[Any], typed_at: int) -> bool:
    """Return True if the harness has answered the command the user typed at event *typed_at*.

    An answer names the message it answers: ``answers`` on the harness's
    ``llm.response``, and for a coding run the ``source_event_id`` of the
    ``code.run_started`` whose ``code.run_result`` is the answer.  So each
    command is answered by its own answer and no other's, however many
    commands one wake read.

    Nothing else says a command was answered.  Not the cursor: a tool's
    result, or a turn that was refused or failed, moves it past a message
    nobody read.  Not a model's request after it: a command is never the
    model's to read.

    Answers written before they were named carry no name.  One of those
    counts for the message it follows when a wake began between the two
    and the model was not asked: the wake that takes a command up writes
    ``harness.wake`` and then the answer, with no request between them.
    """
    runs: dict[Any, Any] = {}
    taken_up = False
    for event in events:
        if event.id is None or event.id <= typed_at:
            continue
        event_type = _event_type(event)
        data = getattr(event, "data", None) or {}
        if event_type == EventType.CODE_RUN_STARTED.value:
            runs[data.get("run_id")] = data.get("source_event_id")
        elif event_type == EventType.CODE_RUN_RESULT.value:
            if data.get("run_id") in runs and runs[data.get("run_id")] == typed_at:
                return True
        elif event_type == EventType.LLM_RESPONSE.value and "answers" in data:
            if data["answers"] == typed_at:
                return True
        elif event_type == EventType.HARNESS_WAKE.value:
            taken_up = True
        elif event_type == EventType.LLM_REQUEST.value:
            taken_up = False
        elif taken_up and event_type == EventType.LLM_RESPONSE.value:
            return True
    return False


def _left_behind(events: list[Any], typed_at: int, *, now: datetime, window: timedelta) -> bool:
    """Return True if the command typed at event *typed_at* lies behind a turn's end older than *window*.

    A command typed during a turn waits for that turn's end, and its own
    wake follows at once.  One still unanswered long after is from a log an
    older harness left, whose user has had the model's words for it since:
    it is not run.
    """
    return any(
        event.id is not None
        and event.id > typed_at
        and _event_type(event) == EventType.SESSION_COMPLETE.value
        and _aware(event.created_at) < now - window
        for event in events
    )


def _aware(moment: datetime) -> datetime:
    """*moment* in UTC: the store gives some timestamps without their zone."""
    return moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment


_REPORT_EVENT_TYPES = frozenset({
    EventType.WORKER_COMPLETE.value,
    EventType.WORKER_FAILED.value,
})
#: The messages the harness writes to give a goal its next turn.
_GOAL_TURN_MESSAGES = frozenset({"outcome_kickoff", "outcome_continuation"})


def _first_unread(events: list[Any], *, goal_in_flight: bool, is_plain_message: Any) -> int | None:
    """Return the id of the first event in *events* that still waits for the model to read it.

    A worker's report; a message of the user's own that is no command
    (*is_plain_message* says which); and, while a goal is in flight, the
    message that gives the goal its next turn.  A request reads what was
    written before it, so the unread ones are those after the log's last
    ``llm.request``.
    """
    first: int | None = None
    for event in events:
        event_type = _event_type(event)
        if event_type == EventType.LLM_REQUEST.value:
            first = None
        elif first is None and (
            event_type in _REPORT_EVENT_TYPES
            or is_plain_message(event)
            or goal_in_flight and _gives_a_goal_its_turn(event)
        ):
            first = event.id
    return first


#: What ends a turn of the model's for good: after one of these the turn is not one to go on with.
_TURN_END_EVENT_TYPES = frozenset({
    EventType.SESSION_COMPLETE.value,
    EventType.SESSION_FAIL.value,
    EventType.SESSION_PAUSE.value,
    EventType.SESSION_STOPPED.value,
})


def _turn_cut_off(events: list[Any]) -> bool:
    """Return True if *events* end in a turn of the model's that its worker's death cut off.

    The model was asked and has not answered, or its last answer called
    tools: the turn is to be gone on with.  An answer of the harness's to a
    command is no word of the model's and leaves the turn as it was.
    """
    cut_off = False
    for event in events:
        event_type = _event_type(event)
        if event_type == EventType.LLM_REQUEST.value:
            cut_off = True
        elif event_type == EventType.LLM_RESPONSE.value:
            data = getattr(event, "data", None) or {}
            if "answers" not in data:
                cut_off = bool((data.get("message") or {}).get("tool_calls"))
        elif event_type in _TURN_END_EVENT_TYPES:
            cut_off = False
    return cut_off


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


def _plain_message_unread(events: list[Any], *, is_plain_message: Any) -> bool:
    """Return True if a message the user wrote themselves, and no command, is in *events* with no model request after it."""
    unread = False
    for event in events:
        if _event_type(event) == EventType.LLM_REQUEST.value:
            unread = False
        elif is_plain_message(event):
            unread = True
    return unread
