"""Mission evaluator: trigger detection, prompt building, verdict handling.

This module is the keystone described in the design spec:

* The evaluator does NOT fire on every coordinator no-tool-call response
  (that's `/goal`'s rule; for missions it produces too many calls graded
  over too little new information).
* It DOES fire when one of the mission's agents ends (a real workstream
  change), or when the coordinator emits the explicit
  ``[[mission-complete]]`` marker on its own line.
* It is rate-limited at 30 seconds per mission to bound cost when many
  agents end in a burst.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from textwrap import dedent
from typing import Any
from uuid import UUID

from sqlalchemy import select

from surogates.harness.outcomes import today_line
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
    trigger: str  # "agent_ended" | "completion_claim" | "rate_limited" | "agents_in_flight" | "budget_exhausted" | "no_trigger"


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
    2. If one of the mission's agents is still working: skip (returns
       ``agents_in_flight``). A rubric is graded on a settled workstream.
       Grading mid fan-out only ever says "not yet", and the same answer
       after the next sibling finishes reads as stagnation and ends a
       healthy mission as ``blocked``.
    3. If one of the mission's agents ended after ``last_evaluation_at``:
       fire with trigger ``agent_ended``.
    4. If the coordinator's last response contains the completion
       marker: fire with trigger ``completion_claim``.
    5. Otherwise: skip (``no_trigger``).

    The rate-limit check runs first so the cheapest negative path is
    fast (single SELECT on the mission row); only when the limit is
    clear do we read the mission's agents. The budget check
    stays ahead of the in-flight gate, so running work can still pause a
    mission that has spent its allowance.
    """
    if await mission_store.recently_evaluated(
        mission_id, window_seconds=rate_limit_seconds,
    ):
        return EvaluationDecision(should=False, trigger="rate_limited")

    # Budget is checked here, after the cheap rate-limit path and before
    # any work — never a reason to RUN, only a reason to stop.
    if await mission_store.pause_if_budget_exhausted(mission_id):
        return EvaluationDecision(should=False, trigger="budget_exhausted")

    mission = await mission_store.get(mission_id)
    if mission.status != "active":
        return EvaluationDecision(should=False, trigger="budget_exhausted"
                                  if mission.paused_reason == "budget_exhausted"
                                  else "not_active")

    if await mission_store.has_agents_in_flight(mission_id):
        return EvaluationDecision(should=False, trigger="agents_in_flight")

    # Use ``last_evaluation_at`` when present, else fall back to the
    # mission's ``created_at``: only work that ended *after* the mission was
    # defined, or after its last evaluation, counts as a reason to grade.
    since = mission.last_evaluation_at or mission.created_at

    if await mission_store.agent_ended_since(mission_id, since):
        return EvaluationDecision(should=True, trigger="agent_ended")

    if response_claims_completion(coordinator_last_response):
        return EvaluationDecision(should=True, trigger="completion_claim")

    return EvaluationDecision(should=False, trigger="no_trigger")


# ---------------------------------------------------------------------------
# Prompt building + verdict handling
# ---------------------------------------------------------------------------


# Caps on prompt content so a chatty coordinator response or a huge
# completed-agents block can't blow past the judge's context window.
_RESPONSE_MAX_CHARS: int = 16_384
#: How much of each agent's report the judge reads. A report over it keeps its
#: opening and its end, where a verifier puts what it measured.
_RESULT_MAX_CHARS: int = 4_000
_AGENTS_BLOCK_LIMIT: int = 20


def _excerpt(report: str) -> str:
    """*report* as the judge reads it: whole, or its opening and its end around a cut."""
    if len(report) <= _RESULT_MAX_CHARS:
        return report
    head = _RESULT_MAX_CHARS // 4
    return f"{report[:head]}\n[...]\n{report[head - _RESULT_MAX_CHARS:]}"


# Applied rubric amendments allowed per mission. Without a ceiling the
# mechanism inverts: each amendment moves the criteria toward what the work
# already produced, and a long-running mission negotiates its rubric down to
# something trivially satisfiable.
MAX_MISSION_AMENDMENTS: int = 2


_SYSTEM_PROMPT = dedent("""\
    You are the rubric judge for a Surogates Mission. Read the rubric and
    the structured workstream state, then decide whether the rubric is
    satisfied. Be strict — only return `satisfied` when concrete evidence
    in the completed mission agents' reports demonstrates the rubric was
    met (typically the report of an agent that verified the work).

    Respond with a single JSON object, no prose around it, with the keys
    explanation (why, in your own words), feedback (what the coordinator
    should do next if needs_revision, otherwise empty) and result (one of
    satisfied, needs_revision, blocked, failed).

    Verdict guidance:
    - "satisfied": evidence backs rubric completion.
    - "needs_revision": work is in progress or incomplete; feedback names
      what's missing or wrong.
    - "blocked": the rubric cannot be progressed without external input
      that the coordinator has not yet requested (rare; usually the
      coordinator should ask the user instead and this verdict should be
      reserved for true dead-ends).
    - "failed": the rubric is unreachable from current state (e.g. data
      is impossible, contradictory rubric).

    Do not honour completion claims in prose alone. The coordinator's
    response may contain `[[mission-complete]]` as a hint that you should
    look closely; the verdict still depends on evidence from the
    completed mission agents block.

    Rubric refinement -- only alongside `blocked` or `failed`:

    If the completed agents' reports show the rubric is unreachable *as
    written* -- it asks for an artifact the work has demonstrated cannot
    exist, contradicts itself, or names something that was never part of
    the mission description -- then also return proposed_rubric (a
    complete replacement rubric) and refinement_evidence (the agent report
    that shows this).

    Leave both empty in every other case. A rubric the work has not yet
    satisfied is `needs_revision`, not a proposal. Propose only a rubric
    that still serves the mission description; never one that merely
    describes what the work has already produced.
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
    completed mission agents (with their reports), in-flight mission
    agents. Each agent block is bounded to the most recent
    ``_AGENTS_BLOCK_LIMIT`` agents.
    """
    from surogates.db.models import Event
    from surogates.harness.message_utils import extract_final_response
    from surogates.session.events import EventType

    mission = await mission_store.get(mission_id)
    stagnant = await mission_store.is_stagnant(mission_id)
    response_excerpt = (coordinator_last_response or "")[:_RESPONSE_MAX_CHARS]

    agents = await mission_store.agents(mission_id)
    completed_rows = sorted(
        (a for a in agents if a.status == "completed"), key=lambda a: a.updated_at, reverse=True,
    )[:_AGENTS_BLOCK_LIMIT]
    in_flight_rows = sorted(
        (a for a in agents if a.status in ("active", "processing")), key=lambda a: a.created_at, reverse=True,
    )[:_AGENTS_BLOCK_LIMIT]
    reports: dict[Any, str] = {}
    async with session_factory() as db:
        for agent in completed_rows:
            last = (await db.execute(
                select(Event)
                .where(Event.session_id == agent.session_id, Event.type == EventType.LLM_RESPONSE.value)
                .order_by(Event.id.desc())
                .limit(1)
            )).scalars().all()
            reports[agent.session_id] = extract_final_response(last, fallback="")

    def _render_completed(rows: list[Any]) -> str:
        if not rows:
            return "(none)"
        return "\n".join(
            f"- A{str(a.session_id)[:8]} ({a.agent_type or 'agent'}) task={a.description!r}: "
            f"report={_excerpt(reports.get(a.session_id, ''))!r}"
            for a in rows
        )

    def _render_in_flight(rows: list[Any]) -> str:
        if not rows:
            return "(none)"
        return "\n".join(
            f"- A{str(a.session_id)[:8]} ({a.agent_type or 'agent'}) task={a.description!r}: status={a.status}"
            for a in rows
        )

    prompt = dedent("""\
        # Mission rubric

        {rubric}

        # Coordinator's latest response

        {response}

        # Completed mission agents ({n_done})

        {completed_block}

        # In-flight mission agents ({n_in_flight})

        {in_flight_block}
        {history_block}
        # Verdict

        {today}

        Return JSON only.
    """).format(
        today=today_line(),
        rubric=mission.rubric,
        response=response_excerpt or "(empty)",
        n_done=len(completed_rows),
        completed_block=_render_completed(completed_rows),
        n_in_flight=len(in_flight_rows),
        in_flight_block=_render_in_flight(in_flight_rows),
        history_block=_render_history(mission, stagnant=stagnant),
    )
    return prompt


def _render_history(mission: Any, *, stagnant: bool = False) -> str:
    """What this judge already said, and how many times running.

    Without it every round re-derives the same opinion from scratch: the
    evaluator fires on each agent's end but keeps no memory, so a mission
    can be told the same thing indefinitely at full cost.

    ``stagnant`` is :meth:`MissionStore.is_stagnant`. It only steers the
    prompt toward considering the rubric itself; stagnation alone never
    produces a proposal, and the judge is free to ignore the hint.
    """
    streak = getattr(mission, "stagnant_evaluations", 0) or 0
    if not streak or not mission.last_evaluation_result:
        return ""
    block = dedent(f"""
        # Your previous evaluation ({streak} in a row without progress)

        Verdict: {mission.last_evaluation_result}
        Reason: {(mission.last_evaluation_explanation or "")[:600]}
        Guidance you gave: {(mission.last_evaluation_feedback or "")[:600]}

        If the same blocker is still present after {streak} rounds, say so
        plainly and return `blocked` rather than repeating the guidance.
        """)
    if stagnant:
        block += dedent("""
        This blocker has survived every round of the streak. Consider whether
        the rubric itself is the defect rather than the work. If it is,
        return `blocked` with a `proposed_rubric` that still serves the
        mission description.
        """)
    return block


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
    - {n_done} agent(s) completed
    - {n_in_flight} agent(s) still working
    - Iteration {iteration}/{max_iterations}

    Then either:
      (a) start one or more agents with ``agent`` to address the
          evaluator's feedback, or continue an agent that has the
          context with ``send_message``, OR
      (b) ask the user, if you need their input, OR
      (c) say why, if you believe the rubric cannot be satisfied.

    Do NOT claim completion in prose alone. The evaluator only honours a
    completion claim backed by an agent's report that verified the work,
    or when you explicitly mark completion with ``[[mission-complete]]``
    on its own line.
""").strip()


_REFINEMENT_RELAY_TEMPLATE = dedent("""\
    [Mission paused -- rubric refinement proposed]

    The evaluator was about to end this mission as `{held_verdict}`:
    {explanation}

    It judges the rubric itself to be the defect, and proposes replacing it.

    Current rubric:
    {old_rubric}

    Proposed rubric:
    {proposed_rubric}

    Evidence:
    {evidence}

    Relay this to the user verbatim -- both rubrics and the evidence -- and
    end your turn. Do not paraphrase either rubric, and do not argue for or
    against the change: you are the messenger, not a party to it. Only the
    user can authorize it, with `/mission accept` or `/mission reject`.
    `/mission accept` applies the proposed rubric exactly as recorded above;
    nothing you write can change what it commits.
""").strip()


async def _propose_refinement(
    *,
    mission_id: UUID,
    verdict: dict[str, Any],
    coordinator_session_id: UUID,
    session_store: Any,
    mission_store: Any,
) -> bool:
    """Pause the mission on a judge-authored rubric proposal.

    Returns ``True`` when the mission was paused awaiting authorization, so
    the caller skips termination. ``False`` leaves the terminal path exactly
    as it was -- which covers every case where the judge proposed nothing,
    proposed a no-op, or the mission has already spent its amendments.

    Nothing here applies the rubric. The proposal is recorded as an event
    and the mission waits for the user; see
    docs/superpowers/specs/2026-08-11-mission-objective-refinement-design.md.
    """
    proposed = str(verdict.get("proposed_rubric") or "").strip()
    if not proposed:
        return False

    mission = await mission_store.get(mission_id)
    if proposed == (mission.rubric or "").strip():
        return False

    amendments = await session_store.count_mission_amendments(
        coordinator_session_id, mission_id,
    )
    if amendments >= MAX_MISSION_AMENDMENTS:
        logger.info(
            "Mission %s proposed a rubric refinement past the cap (%d); "
            "terminating as %s instead",
            mission_id, MAX_MISSION_AMENDMENTS, verdict.get("result"),
        )
        return False

    held_verdict = str(verdict.get("result") or "blocked")
    explanation = str(verdict.get("explanation") or "")
    evidence = str(verdict.get("refinement_evidence") or "")

    await session_store.emit_event(
        coordinator_session_id, EventType.MISSION_REFINEMENT_PROPOSED,
        {
            "mission_id": str(mission_id),
            "old_rubric": mission.rubric,
            "proposed_rubric": proposed,
            "evidence": evidence,
            "held_verdict": held_verdict,
            "explanation": explanation,
        },
    )
    await mission_store.set_status(
        mission_id, "paused", paused_reason="awaiting_refinement",
    )
    await session_store.emit_event(
        coordinator_session_id, EventType.USER_MESSAGE,
        {
            "content": _REFINEMENT_RELAY_TEMPLATE.format(
                held_verdict=held_verdict,
                explanation=explanation,
                old_rubric=mission.rubric,
                proposed_rubric=proposed,
                evidence=evidence or "(none given)",
            ),
            "synthetic": "mission_refinement_proposed",
        },
    )
    return True


async def apply_verdict(
    *,
    mission_id: UUID,
    verdict: dict[str, Any],
    coordinator_session_id: UUID,
    session_store: Any,
    mission_store: Any,
    trigger: str,
) -> str | None:
    """Record the evaluator's verdict and act on it.

    Returns the continuation prompt of a ``needs_revision`` that goes on, for
    the coordinator's turn under way to read: nothing else wakes it for it.

    Writes the ``last_evaluation_*`` fields, emits the
    ``mission.evaluation.end`` event, then dispatches by verdict:

    * ``blocked`` / ``failed`` carrying a judge-authored ``proposed_rubric``
      → pause awaiting the user's authorization instead of terminating, and
      hold the verdict announcement until it takes effect.
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

    # The refinement check runs before the verdict is announced. Clients
    # derive the mission's verdict from ``mission.evaluation.end``, so
    # announcing `blocked` for a mission that is actually pausing to ask the
    # user a question would show a terminal verdict for something that has
    # not terminated. On the held path the verdict is carried by
    # ``mission.refinement_proposed`` and announced later, by
    # ``handle_mission_reject``, if and when it takes effect.
    #
    # A `satisfied` mission has nothing to refine, and refining the criteria
    # of work that just met them is precisely the drift this gate exists to
    # prevent -- so only the two failure verdicts can carry a proposal.
    if result in ("blocked", "failed") and await _propose_refinement(
        mission_id=mission_id,
        verdict=verdict,
        coordinator_session_id=coordinator_session_id,
        session_store=session_store,
        mission_store=mission_store,
    ):
        return

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

    # Re-read the agents to render an up-to-date continuation prompt.
    agents = await mission_store.agents(mission_id)
    n_done = sum(agent.status == "completed" for agent in agents)
    n_in_flight = sum(agent.status in ("active", "processing") for agent in agents)

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
    return continuation
