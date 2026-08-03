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


def match_choice_label(text: str, choices: list[dict]) -> str | None:
    """The choice *text* selects, matched case- and padding-insensitively.

    The one definition of "is this answer on the menu", shared by the
    free-text channel resolver and the server-side ``is_other`` check so
    the two cannot drift.
    """
    target = (text or "").strip().lower()
    if not target:
        return None
    for choice in choices:
        label = (choice.get("label") or "").strip()
        if label and label.lower() == target:
            return label
    return None


async def asked_questions(
    store,
    *,
    session_id,
    tool_call_id: str,
) -> list[dict]:
    """The questions stored for a tool call, however far along it is.

    Prefers the pending inbox row, then falls back to the ask itself in
    the event log.  The row is claimed the moment anyone answers, so a
    lookup that only consulted it would go blind exactly when two
    surfaces answer at once — the case the derivation exists for.  The
    event log is append-only and cannot be claimed.
    """
    tc_id = valid_tool_call_id(tool_call_id)
    if tc_id is None:
        return []

    pending = await pending_input_for_session(
        store, session_id=session_id, tool_call_id=tc_id,
    )
    if pending is not None:
        return pending.get("questions") or []

    async with store._sf() as db:
        row = await db.execute(
            select(Event.data)
            .where(
                Event.session_id == session_id,
                Event.type == EventType.INBOX_INPUT_REQUIRED.value,
                Event.data["tool_call_id"].as_string() == tc_id,
            )
            .order_by(Event.id.desc())
            .limit(1),
        )
        data = row.scalar_one_or_none() or {}
    return data.get("questions") or []


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

    Position decides first, confirmed by the prompt: every producer
    emits one response per question in order, and a batch may repeat a
    prompt, so matching on text alone would answer the second "Continue?"
    against the first one's menu.  Only when the prompt at that position
    disagrees do we look it up by text, which covers a client that
    reordered or rewrote it.  A question we cannot identify at all keeps
    the flag it arrived with: refusing to guess is better than
    overwriting a correct value with a made-up one.
    """
    by_prompt: dict[str, dict] = {}
    for question in questions:
        prompt = question.get("prompt")
        if isinstance(prompt, str):
            by_prompt.setdefault(prompt.strip(), question)

    derived: list[dict] = []
    for index, response in enumerate(responses):
        row = dict(response)
        asked = str(row.get("question") or "").strip()

        question = None
        if index < len(questions):
            at_index = questions[index]
            prompt = at_index.get("prompt")
            if isinstance(prompt, str) and prompt.strip() == asked:
                question = at_index
        if question is None:
            question = by_prompt.get(asked)
        if question is None and index < len(questions):
            question = questions[index]
        if question is None:
            derived.append(row)
            continue
        choices = question.get("choices") or []
        row["is_other"] = bool(choices) and match_choice_label(
            str(row.get("answer") or ""), choices,
        ) is None
        derived.append(row)
    return derived


async def resolve_input_response(
    store,
    *,
    session_id,
    tool_call_id: str,
    responses: list[dict],
) -> int | None:
    """Claim the pending inbox item and emit the response event.

    Returns the emitted event id (truthy) when this call resolved the
    question, ``None`` when there was nothing pending to resolve — the
    inbox-row update is the atomic claim, so two surfaces racing on the
    same answer produce exactly one response event.

    ``is_other`` is settled here against the asked payload, so no
    channel can submit a flag that goes unchecked.
    """
    tc_id = valid_tool_call_id(tool_call_id)
    if tc_id is None:
        return None

    responses = derive_is_other(
        await asked_questions(store, session_id=session_id, tool_call_id=tc_id),
        responses,
    )

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


async def expire_input_request(
    store,
    *,
    session_id,
    tool_call_id: str,
) -> bool:
    """Retire the pending row for a question nobody is waiting on.

    Called when the blocked tool gives up — a timeout, or a session that
    was paused or completed under it. Until then the row is the only
    thing standing between a user and an answer that would be recorded
    with no consumer, and the tool is the one that knows the exact
    moment: the sweeper's periodic pass is a backstop for the case where
    the worker died without reaching this call, not the primary path.
    """
    tc_id = valid_tool_call_id(tool_call_id)
    if tc_id is None:
        return False

    async with store._sf() as db:
        result = await db.execute(
            update(InboxItem)
            .where(
                InboxItem.session_id == session_id,
                InboxItem.kind == "input_required",
                InboxItem.action_ref["tool_call_id"].as_string() == tc_id,
                InboxItem.status == "pending",
            )
            .values(status="expired", updated_at=func.now()),
        )
        await db.commit()
    return bool(getattr(result, "rowcount", 0))


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

    return await resolve_input_response(
        store,
        session_id=session_id,
        tool_call_id=tool_call_id,
        responses=resolve_text_answer(pending.get("questions") or [], text),
    )
