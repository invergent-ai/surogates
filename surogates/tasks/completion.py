"""Event-log inspection helpers for the task-layer dispatcher.

When the dispatcher's ``_finalize_ended_sessions`` step finds a Task in
``running`` whose ``current_session_id`` Session has ended (status no
longer ``active``), it consults the session's event log to decide which
terminal state the Task belongs in:

* If the last task-relevant event is ``WORKER_COMPLETE`` — successful
  completion.  ``Task.status='done'``, ``Task.result`` is filled from
  the event payload.
* If the last task-relevant event is ``TASK_BLOCKED`` — the worker
  self-blocked via ``worker_block``.  The tool handler already wrote
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


async def fetch_prior_attempt_summaries(
    session_factory: Any,
    session_store: Any,
    task_id: Any,
    *,
    exclude_session_id: Any | None = None,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Fetch summary records for prior Session attempts of a Task.

    Returns a list of dicts ``[{outcome, summary, session_id}, …]``
    ordered earliest-first, capped at ``limit`` entries (most recent
    ones win when truncated — useful for the retry-context injection
    where the latest failures are most informative).

    ``outcome`` is the string form of :class:`TaskAttemptOutcome`
    (``"completed"`` / ``"blocked"`` / ``"crashed"``).  ``summary`` is
    the most relevant prose for that outcome: the WORKER_COMPLETE
    result text for completed, the block reason for blocked, or a
    generic crashed-with-no-completion-event placeholder otherwise.

    Used by ``_create_session_for_task`` to inject a "Prior attempts"
    section into the next attempt's initial USER_MESSAGE so the
    retried worker can avoid repeating what already failed.
    """
    from sqlalchemy import select as _sel

    from surogates.db.models import Session as _ORMSession

    async with session_factory() as db:
        stmt = (
            _sel(_ORMSession)
            .where(_ORMSession.task_id == task_id)
            .order_by(_ORMSession.created_at)
        )
        if exclude_session_id is not None:
            stmt = stmt.where(_ORMSession.id != exclude_session_id)
        sessions = (await db.execute(stmt)).scalars().all()

    out: list[dict[str, Any]] = []
    for sess in sessions:
        try:
            events = await session_store.get_events(sess.id)
        except Exception:
            events = []
        outcome, last_event = classify_attempt_outcome(events)
        entry: dict[str, Any] = {
            "session_id": str(sess.id),
            "outcome": outcome.value,
            "summary": None,
        }
        if outcome is TaskAttemptOutcome.COMPLETED and last_event is not None:
            entry["summary"] = extract_result_from_completion_event(last_event)
        elif outcome is TaskAttemptOutcome.BLOCKED and last_event is not None:
            import json as _json
            raw = getattr(last_event, "payload", None) or getattr(last_event, "data", None) or {}
            if isinstance(raw, str):
                try:
                    raw = _json.loads(raw)
                except Exception:
                    raw = {}
            entry["summary"] = (raw or {}).get("reason")
        else:
            entry["summary"] = "(no completion event — likely crashed or timed out)"
        out.append(entry)

    # Cap to the most recent ``limit`` so deep retry chains stay bounded.
    if len(out) > limit:
        out = out[-limit:]
    return out


def render_prior_attempts_section(prior: list[dict[str, Any]]) -> str:
    """Render the prior-attempts summary as a markdown section for the
    worker's initial USER_MESSAGE.

    Returns the section *body* (no leading/trailing blanks); the caller
    is responsible for prepending its own delimiter ("\\n\\n## ...").
    Returns an empty string when ``prior`` is empty so callers can
    unconditionally concatenate without worrying about empty headers.
    """
    if not prior:
        return ""
    lines = []
    for i, entry in enumerate(prior, 1):
        summary = entry.get("summary") or "(no summary recorded)"
        lines.append(f"- Attempt {i} ({entry['outcome']}): {summary}")
    return "\n".join(lines)


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
