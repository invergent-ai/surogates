"""Durable helpers for resolving ask_user_question from channel surfaces."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import func, select, update

from surogates.db.models import Event, InboxItem
from surogates.session.events import EventType

logger = logging.getLogger(__name__)


def valid_tool_call_id(tool_call_id: str) -> str | None:
    value = (tool_call_id or "").strip()
    if not value or len(value) > 128 or any(ch in value for ch in "\r\n\0"):
        return None
    return value


async def pending_input_for_session(
    store,
    *,
    session_id,
    tool_call_id: str | None = None,
) -> dict | None:
    tc_id = valid_tool_call_id(tool_call_id) if tool_call_id is not None else None
    if tool_call_id is not None and tc_id is None:
        return None

    stmt = (
        select(InboxItem)
        .where(
            InboxItem.session_id == session_id,
            InboxItem.kind == "input_required",
            InboxItem.status == "pending",
        )
        .order_by(InboxItem.created_at.desc())
        .limit(1)
    )
    if tc_id is not None:
        stmt = stmt.where(InboxItem.action_ref["tool_call_id"].as_string() == tc_id)

    async with store._sf() as db:
        row = (await db.execute(stmt)).scalar_one_or_none()

    if row is None:
        return None
    payload = row.payload or {}
    return {
        "tool_call_id": (row.action_ref or {}).get("tool_call_id", ""),
        "questions": payload.get("questions") or [],
        "context": payload.get("context", ""),
        # The blocked tool waits at most its own timeout; callers that
        # convert free-text messages into answers use this to ignore
        # rows orphaned by a tool timeout on a still-active session
        # (the expire sweeper only clears terminal sessions).
        "created_at": row.created_at,
    }


def _answer_is_on_menu(answer: str, choices: list[dict]) -> bool:
    target = (answer or "").strip().lower()
    if not target:
        return False
    for choice in choices:
        label = (choice.get("label") or "").strip()
        if label and label.lower() == target:
            return True
    return False


def derive_is_other(questions: list[dict], responses: list[dict]) -> list[dict]:
    """Return *responses* with ``is_other`` recomputed from *questions*.

    ``is_other`` means "the user went off the menu", which is a fact
    about what the agent asked, not something a client is in a position
    to assert.  Every surface that submits an answer used to decide it
    independently -- four clients and two channel parsers, agreeing only
    by inspection -- and one of them getting it wrong silently corrupted
    the transcript and the training data derived from it.  Deciding it
    here, from the questions actually stored for the tool call, leaves
    one definition and makes the submitted flag advisory.

    Answers are matched to their question by prompt, falling back to
    position for a client that rewrote the prompt text.  A question we
    cannot identify keeps the flag it arrived with: refusing to guess
    is better than overwriting a correct value with a made-up one.
    """
    by_prompt: dict[str, dict] = {}
    for question in questions:
        prompt = question.get("prompt")
        if isinstance(prompt, str):
            by_prompt.setdefault(prompt.strip(), question)

    derived: list[dict] = []
    for index, response in enumerate(responses):
        row = dict(response)
        question = by_prompt.get(str(row.get("question") or "").strip())
        if question is None and index < len(questions):
            question = questions[index]
        if question is None:
            derived.append(row)
            continue
        choices = question.get("choices") or []
        row["is_other"] = bool(choices) and not _answer_is_on_menu(
            str(row.get("answer") or ""), choices,
        )
        derived.append(row)
    return derived


async def resolve_input_response(
    store,
    *,
    session_id,
    tool_call_id: str,
    responses: list[dict],
    questions: list[dict] | None = None,
) -> int | None:
    """Claim the pending inbox item and emit the response event.

    Returns the emitted event id (truthy) when this call resolved the
    question, ``None`` when there was nothing pending to resolve — the
    inbox-row update is the atomic claim, so two surfaces racing on the
    same answer produce exactly one response event.

    ``questions`` is the asked payload, used to settle ``is_other``
    server-side; callers that already hold it pass it in, and the rest
    have it looked up here so no channel can skip the check.
    """
    tc_id = valid_tool_call_id(tool_call_id)
    if tc_id is None:
        return None

    if questions is None:
        pending = await pending_input_for_session(
            store, session_id=session_id, tool_call_id=tc_id,
        )
        questions = (pending or {}).get("questions") or []
    responses = derive_is_other(questions, responses)

    async with store._sf() as db:
        result = await db.execute(
            update(InboxItem)
            .where(
                InboxItem.session_id == session_id,
                InboxItem.kind == "input_required",
                InboxItem.action_ref["tool_call_id"].as_string() == tc_id,
                InboxItem.status == "pending",
            )
            .values(
                status="responded",
                responded_at=func.now(),
                updated_at=func.now(),
            ),
        )
        await db.commit()
        updated = bool(getattr(result, "rowcount", 0))

    if not updated:
        return None

    try:
        return await store.emit_event(
            session_id,
            EventType.ASK_USER_QUESTION_RESPONSE,
            {"tool_call_id": tc_id, "responses": responses},
        )
    except Exception:
        # The claim committed but the response event did not: without a
        # revert the row reads "responded" while the tool keeps waiting
        # and no later attempt can convert — a wedged question. Putting
        # the row back lets the caller fall back to a normal message
        # and the user answer again via any surface.
        try:
            async with store._sf() as db:
                await db.execute(
                    update(InboxItem)
                    .where(
                        InboxItem.session_id == session_id,
                        InboxItem.kind == "input_required",
                        InboxItem.action_ref["tool_call_id"].as_string()
                        == tc_id,
                        InboxItem.status == "responded",
                    )
                    .values(
                        status="pending",
                        responded_at=None,
                        updated_at=func.now(),
                    ),
                )
                await db.commit()
        except Exception:
            logger.warning(
                "Failed to revert claim for tool_call_id=%s after emit "
                "failure; the question stays claimed until answered via "
                "the form",
                tc_id,
                exc_info=True,
            )
        raise


async def response_event_exists(
    store,
    *,
    session_id,
    tool_call_id: str,
) -> bool:
    """Whether a response event was already recorded for the tool call.

    The form's respond route emits its event BEFORE its best-effort
    inbox claim, so a row can read ``pending`` although the question is
    answered — converters must not eat the next message for it. One
    indexed probe (``idx_events_session_type``) instead of scanning the
    session's response history.
    """
    async with store._sf() as db:
        row = await db.execute(
            select(Event.id)
            .where(
                Event.session_id == session_id,
                Event.type == EventType.ASK_USER_QUESTION_RESPONSE.value,
                Event.data["tool_call_id"].as_string() == tool_call_id,
            )
            .limit(1),
        )
        return row.scalar_one_or_none() is not None


async def try_resolve_text_answer(
    store,
    *,
    session_id,
    text: str,
    max_age_seconds: float | None = None,
) -> int | None:
    """Resolve free-form *text* as the answer to the live pending question.

    The full guard set for surfaces that convert typed messages into
    answers: nothing pending → ``None``; the pending row older than
    ``max_age_seconds`` → ``None`` (the blocked tool has timed out, and
    converting would feed a consumer that no longer exists); a response
    event already recorded for the tool call → ``None`` (see
    :func:`response_event_exists`). On success returns the response
    event id. Callers treat ``None`` as "deliver the text as a normal
    message".
    """
    pending = await pending_input_for_session(store, session_id=session_id)
    if pending is None:
        return None
    created_at = pending.get("created_at")
    if created_at is not None and max_age_seconds is not None:
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - created_at).total_seconds()
        if age > max_age_seconds:
            return None
    tool_call_id = pending.get("tool_call_id", "")
    if await response_event_exists(
        store, session_id=session_id, tool_call_id=tool_call_id,
    ):
        return None
    from surogates.channels.platforms.telegram_interactive import (
        resolve_text_answer,
    )

    questions = pending.get("questions") or []
    return await resolve_input_response(
        store,
        session_id=session_id,
        tool_call_id=tool_call_id,
        responses=resolve_text_answer(questions, text),
        questions=questions,
    )
