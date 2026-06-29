"""Durable helpers for resolving ask_user_question from channel surfaces."""

from __future__ import annotations

from sqlalchemy import func, select, update

from surogates.db.models import InboxItem
from surogates.session.events import EventType


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
    }


async def resolve_input_response(
    store,
    *,
    session_id,
    tool_call_id: str,
    responses: list[dict],
) -> bool:
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
            .values(
                status="responded",
                responded_at=func.now(),
                updated_at=func.now(),
            ),
        )
        await db.commit()

    if not getattr(result, "rowcount", 0):
        return False

    await store.emit_event(
        session_id,
        EventType.ASK_USER_QUESTION_RESPONSE,
        {"tool_call_id": tc_id, "responses": responses},
    )
    return True
