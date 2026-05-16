"""Event-log inspection helpers for the task-layer dispatcher.

When the dispatcher's ``_finalize_ended_sessions`` step finds a Task in
``running`` whose ``current_session_id`` Session has ended (status no
longer ``active``), it consults the session's event log to decide which
terminal state the Task belongs in:

* If the last task-relevant event is ``WORKER_COMPLETE`` — successful
  completion.  ``Task.status='done'``, ``Task.result`` is filled from
  the event payload.
* If the last task-relevant event is ``TASK_BLOCKED`` — the worker
  self-blocked via ``task_block``.  The tool handler already wrote
  ``Task.status='blocked'`` inside its own transaction; the tick is a
  belt-and-suspenders no-op (the WHERE clause filtered it out anyway,
  but documented here in case finalize runs against an orphaned row).
* If neither event is present — crash / timeout / hard-kill.
  ``Task.status='ready'`` (retry) when ``attempt_count < max_attempts``,
  else ``Task.status='failed'`` with a ``TASK_FAILED`` event emitted to
  the parent session.
"""
from __future__ import annotations

from enum import Enum
from typing import Any

from surogates.session.events import EventType


class TaskAttemptOutcome(str, Enum):
    """Classification of a worker session's last attempt-relevant event."""

    COMPLETED = "completed"   # WORKER_COMPLETE found
    BLOCKED = "blocked"       # TASK_BLOCKED found (tool already mutated state)
    CRASHED = "crashed"       # no completion / block event present


# Event kinds that are *task-relevant* — i.e. that the finalize step
# uses to decide the Task's terminal state.  Other event kinds in the
# session log (USER_MESSAGE, LLM_RESPONSE, tool calls, …) are not
# attempt outcomes and are ignored.
_OUTCOME_EVENT_KINDS: frozenset[str] = frozenset({
    EventType.WORKER_COMPLETE.value,
    EventType.TASK_BLOCKED.value,
})


def classify_attempt_outcome(events: list[Any]) -> tuple[TaskAttemptOutcome, Any]:
    """Inspect a worker Session's event log and classify the attempt.

    Returns ``(outcome, last_relevant_event)`` where ``last_relevant_event``
    is the event row (or None when the outcome is ``CRASHED``).

    Events are inspected in reverse order; the *most recent* outcome
    event wins.  Tasks normally have at most one outcome event, but if
    the harness retried internally and emitted two, the latest reflects
    the final state of the attempt.
    """
    for event in reversed(events):
        kind = getattr(event, "kind", None) or getattr(event, "type", None)
        if kind in _OUTCOME_EVENT_KINDS:
            if kind == EventType.WORKER_COMPLETE.value:
                return TaskAttemptOutcome.COMPLETED, event
            if kind == EventType.TASK_BLOCKED.value:
                return TaskAttemptOutcome.BLOCKED, event
    return TaskAttemptOutcome.CRASHED, None


def extract_result_from_completion_event(event: Any) -> str | None:
    """Return the ``result`` field from a ``WORKER_COMPLETE`` event payload.

    The payload may be a dict (live event) or a JSON-encoded string (some
    ORM rows store ``data`` as text). Tolerates both shapes; returns
    ``None`` if the field is missing or parsing fails.
    """
    import json as _json

    raw = getattr(event, "payload", None)
    if raw is None:
        raw = getattr(event, "data", None)
    if raw is None:
        return None
    if isinstance(raw, str):
        try:
            raw = _json.loads(raw)
        except Exception:
            return None
    if not isinstance(raw, dict):
        return None
    val = raw.get("result")
    return str(val) if val is not None else None
