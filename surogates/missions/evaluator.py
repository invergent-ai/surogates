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

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from textwrap import dedent
from typing import Any
from uuid import UUID

from sqlalchemy import select

from surogates.session.events import EventType

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
    since: datetime,
) -> bool:
    """True iff a terminal task has completed strictly after ``since``.

    ``since`` is a hard lower bound — callers pass
    ``mission.last_evaluation_at or mission.created_at`` so the first
    evaluator pass only counts tasks completed after the mission was
    created, not pre-existing terminal rows with the same mission_id.
    """
    from surogates.db.models import Task

    async with session_factory() as db:
        stmt = (
            select(Task.id)
            .where(
                Task.mission_id == mission_id,
                Task.status.in_(("done", "failed", "cancelled")),
                Task.completed_at.isnot(None),
                Task.completed_at > since,
            )
            .limit(1)
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

    # Use ``last_evaluation_at`` when present, else fall back to the
    # mission's ``created_at``. This prevents the first evaluator pass
    # from firing on terminal tasks that pre-date the mission (possible
    # with seed data or under-edited mission_id stamps); only work that
    # happened *after* the mission was defined counts as evidence.
    since = mission.last_evaluation_at or mission.created_at

    if await _has_recent_terminal_task(
        mission_id,
        session_factory=session_factory,
        since=since,
    ):
        return EvaluationDecision(should=True, trigger="task_terminal")

    if response_claims_completion(coordinator_last_response):
        return EvaluationDecision(should=True, trigger="completion_claim")

    return EvaluationDecision(should=False, trigger="no_trigger")


# ---------------------------------------------------------------------------
# Prompt building + verdict handling
# ---------------------------------------------------------------------------


# Caps on prompt content so a chatty coordinator response or a huge
# completed-tasks block can't blow past the judge's context window.
_RESPONSE_MAX_CHARS: int = 16_384
_RESULT_MAX_CHARS: int = 400
_TASKS_BLOCK_LIMIT: int = 20


_SYSTEM_PROMPT = dedent("""\
    You are the rubric judge for a Surogates Mission. Read the rubric and
    the structured workstream state, then decide whether the rubric is
    satisfied. Be strict — only return `satisfied` when concrete evidence
    in the completed mission tasks demonstrates the rubric was met
    (typically `result_metadata` from a verifier task).

    Respond with a single JSON object, no prose around it:

        {"result": "satisfied" | "needs_revision" | "blocked" | "failed",
         "explanation": "<1-3 sentences>",
         "feedback": "<actionable feedback for the coordinator if needs_revision; empty otherwise>"}

    Verdict guidance:
    - "satisfied": evidence backs rubric completion.
    - "needs_revision": work is in progress or incomplete; feedback names
      what's missing or wrong.
    - "blocked": the rubric cannot be progressed without external input
      that the coordinator has not yet requested (rare; usually the
      coordinator should call ``worker_block`` instead and this verdict
      should be reserved for true dead-ends).
    - "failed": the rubric is unreachable from current state (e.g. data
      is impossible, contradictory rubric).

    Do not honour completion claims in prose alone. The coordinator's
    response may contain `[[mission-complete]]` as a hint that you should
    look closely; the verdict still depends on evidence from the
    completed mission tasks block.
""").strip()


async def build_evaluator_prompt(
    *,
    mission_id: UUID,
    coordinator_last_response: str | None,
    session_factory: Any,
    mission_store: Any,
) -> str:
    """Render the user-side prompt the judge LLM consumes.

    Includes four blocks: rubric, coordinator's latest response,
    completed mission tasks (with result + result_metadata), in-flight
    mission tasks. Each task block is bounded to the most recent
    ``_TASKS_BLOCK_LIMIT`` rows.
    """
    from surogates.db.models import Task

    mission = await mission_store.get(mission_id)
    response_excerpt = (coordinator_last_response or "")[:_RESPONSE_MAX_CHARS]

    async with session_factory() as db:
        completed_rows = (await db.execute(
            select(Task)
            .where(
                Task.mission_id == mission_id,
                Task.status == "done",
            )
            .order_by(Task.completed_at.desc().nulls_last())
            .limit(_TASKS_BLOCK_LIMIT)
        )).scalars().all()
        in_flight_rows = (await db.execute(
            select(Task)
            .where(
                Task.mission_id == mission_id,
                Task.status.in_(("todo", "ready", "running", "blocked")),
            )
            .order_by(Task.created_at.desc())
            .limit(_TASKS_BLOCK_LIMIT)
        )).scalars().all()

    def _render_completed(rows: list[Any]) -> str:
        if not rows:
            return "(none)"
        lines: list[str] = []
        for t in rows:
            short_id = str(t.id)[:8]
            label = t.agent_def_name or "worker"
            result = (t.result or "")[:_RESULT_MAX_CHARS]
            meta = json.dumps(t.result_metadata) if t.result_metadata else "{}"
            lines.append(
                f"- T{short_id} ({label}) goal={t.goal!r}: "
                f"result={result!r}; metadata={meta}"
            )
        return "\n".join(lines)

    def _render_in_flight(rows: list[Any]) -> str:
        if not rows:
            return "(none)"
        lines: list[str] = []
        for t in rows:
            short_id = str(t.id)[:8]
            label = t.agent_def_name or "worker"
            lines.append(
                f"- T{short_id} ({label}) goal={t.goal!r}: "
                f"status={t.status}; attempts={t.attempt_count}"
            )
        return "\n".join(lines)

    prompt = dedent("""\
        # Mission rubric

        {rubric}

        # Coordinator's latest response

        {response}

        # Completed mission tasks ({n_done})

        {completed_block}

        # In-flight mission tasks ({n_in_flight})

        {in_flight_block}

        # Verdict

        Return JSON only.
    """).format(
        rubric=mission.rubric,
        response=response_excerpt or "(empty)",
        n_done=len(completed_rows),
        completed_block=_render_completed(completed_rows),
        n_in_flight=len(in_flight_rows),
        in_flight_block=_render_in_flight(in_flight_rows),
    )
    return prompt


def evaluator_system_prompt() -> str:
    """The system message for the judge LLM call."""
    return _SYSTEM_PROMPT


_CONTINUATION_TEMPLATE = dedent("""\
    [Continuing toward your mission]

    Description: {description}

    Rubric:
    {rubric}

    Evaluator verdict: needs_revision
    Evaluator feedback: {feedback}

    Current mission state:
    - {n_done} task(s) completed
    - {n_in_flight} task(s) in flight (running/ready/todo/blocked)
    - Iteration {iteration}/{max_iterations}

    Inspect the mission task tree via ``worker_context`` on a recent
    child if you need detail. Then either:
      (a) spawn one or more corrective tasks (via ``spawn_task``) to
          address the evaluator's feedback, OR
      (b) call ``worker_block`` on your own session with a question if
          you need human input, OR
      (c) call ``worker_complete`` on your own session with a failure
          summary if you believe the rubric cannot be satisfied.

    Do NOT claim completion in prose alone. The evaluator only honours a
    completion claim when a verifier task's result_metadata supports it,
    or when you explicitly mark completion with ``[[mission-complete]]``
    on its own line.
""").strip()


async def apply_verdict(
    *,
    mission_id: UUID,
    verdict: dict[str, Any],
    coordinator_session_id: UUID,
    session_store: Any,
    mission_store: Any,
    trigger: str,
) -> None:
    """Record the evaluator's verdict and act on it.

    Writes the ``last_evaluation_*`` fields, emits the
    ``mission.evaluation.end`` event, then dispatches by verdict:

    * ``satisfied`` / ``blocked`` / ``failed`` → set the matching status
      (terminal), clear the session's ``active_mission_id`` so the
      coordinator wakes free of mission context.
    * ``needs_revision`` → increment iteration. If at or past
      ``max_iterations`` → status ``max_iterations_reached``. Else
      emit ``mission.continuation`` + a synthetic user.message with the
      continuation prompt so the coordinator wakes with revised guidance.
    """
    result = verdict.get("result", "needs_revision")
    explanation = verdict.get("explanation", "") or ""
    feedback = verdict.get("feedback", "") or ""

    await mission_store.record_evaluation(
        mission_id, result=result, explanation=explanation, feedback=feedback,
    )

    await session_store.emit_event(
        coordinator_session_id, EventType.MISSION_EVALUATION_END,
        {
            "mission_id": str(mission_id),
            "trigger": trigger,
            "result": result,
            "explanation": explanation,
            "feedback": feedback,
        },
    )

    if result in ("satisfied", "blocked", "failed"):
        await mission_store.set_status(mission_id, result)
        await session_store.clear_session_config_key(
            coordinator_session_id, "active_mission_id",
        )
        return

    if result != "needs_revision":
        logger.warning(
            "Unknown mission evaluator verdict %r for mission %s; "
            "treating as needs_revision",
            result, mission_id,
        )

    new_iter = await mission_store.increment_iteration(mission_id)
    mission = await mission_store.get(mission_id)
    if new_iter >= mission.max_iterations:
        await mission_store.set_status(mission_id, "max_iterations_reached")
        await session_store.clear_session_config_key(
            coordinator_session_id, "active_mission_id",
        )
        return

    # Re-fetch counts to render an up-to-date continuation prompt.
    from sqlalchemy import func as _func

    from surogates.db.models import Task

    async with mission_store._sf() as db:
        n_done = int(await db.scalar(
            select(_func.count(Task.id)).where(
                Task.mission_id == mission_id, Task.status == "done",
            )
        ) or 0)
        n_in_flight = int(await db.scalar(
            select(_func.count(Task.id)).where(
                Task.mission_id == mission_id,
                Task.status.in_(("todo", "ready", "running", "blocked")),
            )
        ) or 0)

    continuation = _CONTINUATION_TEMPLATE.format(
        description=mission.description,
        rubric=mission.rubric,
        feedback=feedback or explanation,
        n_done=n_done,
        n_in_flight=n_in_flight,
        iteration=new_iter,
        max_iterations=mission.max_iterations,
    )
    await session_store.emit_event(
        coordinator_session_id, EventType.MISSION_CONTINUATION,
        {"mission_id": str(mission_id), "iteration": new_iter},
    )
    await session_store.emit_event(
        coordinator_session_id, EventType.USER_MESSAGE,
        {"content": continuation, "synthetic": "mission_continuation"},
    )
