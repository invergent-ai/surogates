"""Read-only coherence checks for a single session.

Answers the question the PROD debugging flow actually asks -- why is this
session not doing anything -- without opening a psql shell. Every check is a
read; nothing here mutates state.

Two kinds of problem earn a place:

* an invariant enforced only at *creation*, so a row written before the rule
  existed (or written directly) can violate it with nothing to notice;
* a config value the runtime silently ignores, so whoever set it has no way
  to learn it did nothing.

Restating what the event log already shows plainly does not earn a place.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from surogates.session.interactive_input import pending_input_for_session

logger = logging.getLogger(__name__)

__all__ = ["Finding", "diagnose_session"]


@dataclass(frozen=True)
class Finding:
    """One coherence problem. ``code`` is stable; ``detail`` is for humans."""

    code: str
    detail: str


def _outcome_is_active(outcome: Any) -> bool:
    """A ``/goal`` counts as active exactly as the mission path counts it."""
    return (
        isinstance(outcome, dict)
        and str(outcome.get("description") or "").strip() != ""
        and str(outcome.get("status") or "active") == "active"
    )


def _age_seconds(value: Any) -> float | None:
    """Seconds since *value*, tolerating the naive UTC Postgres hands back."""
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - value).total_seconds()


async def diagnose_session(store: Any, session_id: UUID) -> list[Finding]:
    """Return every coherence problem found for *session_id*."""
    from surogates.tools.builtin.ask_user_question import (
        ASK_USER_QUESTION_MAX_WAIT_SECONDS,
    )

    try:
        session = await store.get_session(session_id)
    except Exception:
        return [Finding("session_not_found", f"no session {session_id}")]

    findings: list[Finding] = []
    config = getattr(session, "config", None) or {}

    # Waiting on a human is the most common reason a session looks dead.
    try:
        pending = await pending_input_for_session(store, session_id=session_id)
    except Exception:
        logger.warning("doctor: pending-input lookup failed", exc_info=True)
        pending = None
    if pending:
        age = _age_seconds(pending.get("created_at"))
        asked = len(pending.get("questions") or [])
        findings.append(Finding(
            "waiting_on_user",
            f"{asked} question(s) open on tool call "
            f"{pending.get('tool_call_id') or '?'}"
            + (f", asked {age / 60:.0f} min ago" if age is not None else ""),
        ))
        if age is not None and age > ASK_USER_QUESTION_MAX_WAIT_SECONDS:
            findings.append(Finding(
                "input_request_expired",
                "the asking tool call already timed out; an answer now has "
                "nowhere to go. Reply in the session instead.",
            ))

    # Exclusivity is enforced when a mission is created (missions/commands.py),
    # never afterwards, so a legacy or hand-written row can hold both.
    if _outcome_is_active(config.get("outcome")) and config.get("active_mission_id"):
        findings.append(Finding(
            "objective_conflict",
            "an active /goal and an active mission are both set; only one "
            "evaluator loop per session is supported.",
        ))

    # Mirrors the guard in orchestrator/worker.py:_resolve_iteration_budget --
    # anything unusable there is silently replaced by the platform default.
    raw = config.get("max_iterations")
    if raw is not None and (
        isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0
    ):
        findings.append(Finding(
            "unusable_max_iterations",
            f"config sets max_iterations={raw!r}, which the worker cannot "
            "use; it falls back to the platform default.",
        ))

    return findings
