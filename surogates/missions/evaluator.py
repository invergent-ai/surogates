"""Mission evaluator: trigger detection, prompt building, verdict handling.

This module is the keystone described in the design spec:

* The evaluator does NOT fire on every coordinator no-tool-call response
  (that's `/goal`'s rule; for missions it produces too many calls graded
  over too little new information).
* It DOES fire when a mission-linked task transitions to a terminal
  state (a real workstream change), or when the coordinator emits the
  explicit ``[[mission-complete]]`` marker on its own line.
* It is rate-limited at 30 seconds per mission to bound cost when many
  tasks complete in burst.

Prompt building + verdict handling come in Task 9 (appended to this
same module so the evaluator's public surface stays in one place).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select

logger = logging.getLogger(__name__)


# Triggers an evaluator pass when present on its own line in the
# coordinator's no-tool-call response.
_COMPLETION_MARKER_RE = re.compile(
    r"(?m)^\s*\[\[\s*mission-complete\s*\]\]\s*$",
)


@dataclass(slots=True)
class EvaluationDecision:
    """Result of :func:`should_evaluate`."""

    should: bool
    trigger: str  # "task_terminal" | "completion_claim" | "rate_limited" | "no_trigger"


def response_claims_completion(response: str | None) -> bool:
    """True iff the response contains ``[[mission-complete]]`` on its
    own line (whitespace allowed).

    The marker must be alone on a line; embedded uses inside running
    prose (e.g. "I'll mark with [[mission-complete]] later") do not
    trigger the evaluator.
    """
    if not response:
        return False
    return _COMPLETION_MARKER_RE.search(response) is not None


async def _has_recent_terminal_task(
    mission_id: UUID,
    *,
    session_factory: Any,
    since: datetime | None,
) -> bool:
    """True iff a terminal task has completed since the last evaluation."""
    from surogates.db.models import Task

    async with session_factory() as db:
        stmt = (
            select(Task.id)
            .where(
                Task.mission_id == mission_id,
                Task.status.in_(("done", "failed", "cancelled")),
            )
            .limit(1)
        )
        if since is not None:
            stmt = stmt.where(
                Task.completed_at.isnot(None),
                Task.completed_at > since,
            )
        row = await db.scalar(stmt)
    return row is not None


async def should_evaluate(
    *,
    mission_id: UUID,
    coordinator_last_response: str | None,
    session_factory: Any,
    mission_store: Any,
    rate_limit_seconds: int = 30,
) -> EvaluationDecision:
    """Decide whether to fire the mission evaluator now.

    Order:
    1. If the mission was evaluated within ``rate_limit_seconds``: skip
       (returns ``rate_limited``).
    2. If a mission-linked task reached a terminal state after
       ``last_evaluation_at``: fire with trigger ``task_terminal``.
    3. If the coordinator's last response contains the completion
       marker: fire with trigger ``completion_claim``.
    4. Otherwise: skip (``no_trigger``).

    The rate-limit check runs first so the cheapest negative path is
    fast (single SELECT on the mission row); only when the limit is
    clear do we run the more expensive task-table lookup.
    """
    if await mission_store.recently_evaluated(
        mission_id, window_seconds=rate_limit_seconds,
    ):
        return EvaluationDecision(should=False, trigger="rate_limited")

    mission = await mission_store.get(mission_id)

    if await _has_recent_terminal_task(
        mission_id,
        session_factory=session_factory,
        since=mission.last_evaluation_at,
    ):
        return EvaluationDecision(should=True, trigger="task_terminal")

    if response_claims_completion(coordinator_last_response):
        return EvaluationDecision(should=True, trigger="completion_claim")

    return EvaluationDecision(should=False, trigger="no_trigger")
