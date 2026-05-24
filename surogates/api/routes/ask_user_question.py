"""ask_user_question response endpoint -- receives the user's answers to
an ``ask_user_question`` tool call and emits
:attr:`~surogates.session.events.EventType.ASK_USER_QUESTION_RESPONSE`.

The worker's ask_user_question tool handler polls the event log for the
matching ``tool_call_id`` and returns the answers to the LLM.
"""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, update

from surogates.api.session_guards import require_user_writable_session
from surogates.db.models import InboxItem
from surogates.session.events import EventType
from surogates.session.store import SessionNotFoundError, SessionStore
from surogates.tenant.auth.middleware import get_current_tenant
from surogates.tenant.context import TenantContext

logger = logging.getLogger(__name__)

router = APIRouter()


# Limits -- keep synced with the schema constants in ask_user_question.py.
_MAX_ANSWER_LENGTH = 2000
_MAX_PROMPT_LENGTH = 1000
_MAX_RESPONSES = 5


class AskUserQuestionAnswer(BaseModel):
    """A single answer to one question in an ask_user_question batch."""

    question: str = Field(..., min_length=1, max_length=_MAX_PROMPT_LENGTH)
    answer: str = Field(..., min_length=1, max_length=_MAX_ANSWER_LENGTH)
    # When the user chose the "Other" free-form option rather than a
    # predefined choice.  Recorded so the transcript preserves the user's
    # intent and downstream training data can distinguish the two paths.
    is_other: bool = False

    @field_validator("question", "answer")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()


class AskUserQuestionResponseRequest(BaseModel):
    """Batch submission for one ask_user_question tool call."""

    responses: list[AskUserQuestionAnswer] = Field(
        ..., min_length=1, max_length=_MAX_RESPONSES,
    )


class AskUserQuestionResponseReply(BaseModel):
    event_id: int


def _get_session_store(request: Request) -> SessionStore:
    store: SessionStore | None = getattr(request.app.state, "session_store", None)
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Session store not available.",
        )
    return store


def _require_service_account_api_route(
    request: Request,
    tenant: TenantContext,
) -> None:
    """For ``/v1/api/*`` aliases, require a service-account principal."""
    if (
        request.url.path.startswith("/v1/api/")
        and tenant.service_account_id is None
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This endpoint requires a service-account token.",
        )


@router.post(
    "/api/sessions/{session_id}/ask_user_question/{tool_call_id}/respond",
    response_model=AskUserQuestionResponseReply,
    status_code=status.HTTP_201_CREATED,
)
@router.post(
    "/sessions/{session_id}/ask_user_question/{tool_call_id}/respond",
    response_model=AskUserQuestionResponseReply,
    status_code=status.HTTP_201_CREATED,
)
async def respond_to_ask_user_question(
    session_id: UUID,
    tool_call_id: str,
    body: AskUserQuestionResponseRequest,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> AskUserQuestionResponseReply:
    """Record the user's answers and unblock the worker's
    ask_user_question handler."""
    _require_service_account_api_route(request, tenant)
    store = _get_session_store(request)

    try:
        session = await store.get_session(session_id)
    except SessionNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session {session_id} not found.",
        )
    if not tenant.owns_session(session.org_id, session_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session {session_id} not found.",
        )
    require_user_writable_session(session)

    # Sanity-check the tool_call_id format -- short, printable, no newlines.
    tc_id = tool_call_id.strip()
    if not tc_id or len(tc_id) > 128 or any(c in tc_id for c in "\r\n\0"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid tool_call_id.",
        )

    payload = {
        "tool_call_id": tc_id,
        "responses": [r.model_dump() for r in body.responses],
    }
    event_id = await store.emit_event(
        session_id,
        EventType.ASK_USER_QUESTION_RESPONSE,
        payload,
    )
    logger.info(
        "ask_user_question response recorded for session=%s tool_call_id=%s event_id=%s",
        session_id, tc_id, event_id,
    )
    try:
        async with store._sf() as db:
            await db.execute(
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
                )
            )
            await db.commit()
    except Exception:
        logger.exception("Failed to mark ask_user_question inbox item responded.")
    return AskUserQuestionResponseReply(event_id=event_id)
