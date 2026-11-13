"""Mission evaluator hook and judge helpers for the harness loop."""

from __future__ import annotations

import logging
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel

from surogates.harness.message_utils import message_texts
from surogates.harness.outcomes import EVALUATOR_MAX_TOKENS, VerdictReason
from surogates.harness.structured_output import generate_structured, parse_json_object
from surogates.session.events import EventType

logger = logging.getLogger(__name__)


class MissionJudgeParseError(ValueError):
    """Raised when the mission judge returns non-JSON or malformed JSON.

    The harness hook records a parse-failure verdict on the mission row
    and emits a parse-failed evaluation.end event instead of treating
    the malformed response as a regular needs_revision verdict; three
    consecutive parse failures pause the mission per the
    :class:`~surogates.missions.store.MissionStore` contract.
    """


async def _maybe_run_mission_evaluator(
    *,
    session_id: UUID,
    coordinator_last_response: str | None,
    session_store: Any,
    session_factory: Any,
    mission_store: Any,
    judge: Any,
) -> str | None:
    """Run the mission evaluator iff the session has an active mission
    and a trigger condition fires, and return the continuation prompt a
    ``needs_revision`` verdict wrote, if any.

    ``judge`` is an async callable ``(system_prompt, user_prompt) -> dict``
    that returns the parsed verdict JSON. Tests inject a stub; production
    wires it via :func:`_build_mission_judge` below.
    """
    from surogates.missions.evaluator import (
        apply_verdict,
        build_evaluator_prompt,
        evaluator_system_prompt,
        should_evaluate,
    )

    active = await mission_store.get_active_for_session(session_id)
    if active is None or active.status != "active":
        return

    decision = await should_evaluate(
        mission_id=active.id,
        coordinator_last_response=coordinator_last_response,
        session_factory=session_factory,
        mission_store=mission_store,
    )
    if not decision.should:
        return

    await session_store.emit_event(
        session_id, EventType.MISSION_EVALUATION_START,
        {
            "mission_id": str(active.id),
            "iteration": active.iteration,
            "trigger": decision.trigger,
        },
    )

    user_prompt = await build_evaluator_prompt(
        mission_id=active.id,
        coordinator_last_response=coordinator_last_response,
        session_factory=session_factory,
        mission_store=mission_store,
    )
    try:
        verdict = await judge(evaluator_system_prompt(), user_prompt)
    except MissionJudgeParseError as exc:
        failures = await mission_store.record_parse_failure(active.id)
        await session_store.emit_event(
            session_id, EventType.MISSION_EVALUATION_END,
            {
                "mission_id": str(active.id),
                "iteration": active.iteration,
                "trigger": decision.trigger,
                "result": "needs_revision",
                "explanation": "judge parse failure",
                "feedback": str(exc)[:500],
                "parse_failed": True,
                "parse_failures": failures,
            },
        )
        return
    except Exception as exc:
        # Transport-level failure (provider outage, rate limit, timeout).
        # Do NOT synthesize a needs_revision verdict — that would burn
        # one of the mission's max_iterations on something that wasn't a
        # real evaluator turn. Emit a transport-failed evaluation.end
        # event so the dashboard can surface the outage, and return.
        # The next no-tool-call response triggers another attempt; the
        # rate-limit guard prevents tight retries.
        logger.warning(
            "Mission %s evaluator judge call failed (transport): %s",
            active.id, exc,
        )
        await session_store.emit_event(
            session_id, EventType.MISSION_EVALUATION_END,
            {
                "mission_id": str(active.id),
                "iteration": active.iteration,
                "trigger": decision.trigger,
                "result": "transport_failed",
                "explanation": "judge call failed (transport)",
                "feedback": str(exc)[:500],
                "transport_failed": True,
            },
        )
        return

    # The judge does not mint the terminal verdict on its own word. An
    # agent's status is machine-written by the harness, so it is evidence
    # the coordinator cannot author.
    from surogates.missions.verdict_policy import adjust_mission_verdict

    done, total = await mission_store.agent_counts(active.id)
    verdict = adjust_mission_verdict(
        verdict, completed_agents=done, total_agents=total,
    )

    return await apply_verdict(
        mission_id=active.id,
        verdict=verdict,
        coordinator_session_id=session_id,
        session_store=session_store,
        mission_store=mission_store,
        trigger=decision.trigger,
    )


def _parse_judge_json(raw: str) -> dict[str, Any]:
    """Tolerant JSON extraction for the mission judge.

    Delegates to :func:`parse_json_object` (fences, prose around the
    object, reasoning-model preambles) and raises ``ValueError`` when
    no object can be extracted so the caller can surface the error as
    a ``MissionJudgeParseError``.
    """
    parsed = parse_json_object(raw)
    if parsed is None:
        raise ValueError("judge returned non-object or unparsable JSON")
    return parsed


class _MissionVerdict(BaseModel):
    """Structured shape the judge must return.

    Used both for ``outlines``-backed constrained generation (preferred,
    via :func:`generate_structured`) and for tolerant fallback parsing
    when outlines isn't installed or fails to coerce the model's output.
    Keeping the schema in one place means the prompt's documented JSON
    shape and the parser's expected shape stay in lockstep.

    ``proposed_rubric`` / ``refinement_evidence`` are the judge's
    *authorship* of a replacement rubric. They carry no authority: only
    ``/mission accept`` applies one, and it reads the text from the
    recorded event, not from here. Both default empty so a judge response
    written before this existed still validates. See
    docs/superpowers/specs/2026-08-11-mission-objective-refinement-design.md.

    The reasons come before ``result`` because guided decoding emits keys in
    declaration order, and ``explanation`` is required because a strict schema
    otherwise accepts ``{"result": "satisfied"}``.
    """

    explanation: VerdictReason
    feedback: str = ""
    result: Literal["satisfied", "needs_revision", "blocked", "failed"]
    proposed_rubric: str = ""
    refinement_evidence: str = ""


def _build_mission_judge(
    *,
    llm_client: Any,
    eval_model: str,
    structured_generator: Any | None = None,
) -> Any:
    """Return an async ``(system, user) -> dict`` judge bound to ``llm_client``.

    Prefers ``outlines``-backed structured generation against the
    :class:`_MissionVerdict` schema so the LLM cannot emit malformed
    JSON or omit required fields. Falls back to a free-form chat
    completion with tolerant JSON extraction when structured generation
    is unavailable (no outlines, provider doesn't support it) — the
    fallback also reads ``reasoning_content`` for reasoning-mode models
    (GLM, DeepSeek) that leave ``content`` empty.

    A parse / coercion failure raises :class:`MissionJudgeParseError`
    so :func:`_maybe_run_mission_evaluator` can distinguish parse vs.
    transport failure and record the right counter.
    """

    if structured_generator is None:
        structured_generator = generate_structured

    async def judge(system_prompt: str, user_prompt: str) -> dict[str, Any]:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        # Preferred path: constrain the LLM to the verdict schema.
        # Returns None when outlines isn't available, the provider
        # isn't supported, or coercion fails — fall through to the
        # tolerant free-form parser in that case.
        try:
            verdict = await structured_generator(
                llm_client=llm_client,
                model=eval_model,
                messages=messages,
                output_model=_MissionVerdict,
                max_tokens=EVALUATOR_MAX_TOKENS,
                temperature=0,
            )
        except Exception as exc:
            logger.debug(
                "Mission judge structured generation raised %r; "
                "falling back to free-form JSON parsing",
                exc,
            )
            verdict = None
        if verdict is not None:
            return verdict.model_dump()

        # Fallback: free-form completion + tolerant parser.
        resp = await llm_client.chat.completions.create(
            model=eval_model,
            messages=messages,
            temperature=0.0,
            max_tokens=EVALUATOR_MAX_TOKENS,
        )
        try:
            message = resp.choices[0].message
        except (AttributeError, IndexError) as exc:
            raise MissionJudgeParseError(
                f"judge returned an unexpected shape: {exc}",
            ) from exc
        raw = next(iter(message_texts(message)), "")
        if not raw or not str(raw).strip():
            raise MissionJudgeParseError("judge returned empty content")
        try:
            parsed = _parse_judge_json(str(raw))
            # Validate against the verdict schema so the caller always
            # sees the documented shape (or a parse error, never a
            # silently-malformed dict).
            return _MissionVerdict.model_validate(parsed).model_dump()
        except ValueError as exc:
            raise MissionJudgeParseError(str(exc)) from exc

    return judge
