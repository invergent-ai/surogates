"""User feedback on model output.

Exposes ``POST /v1/sessions/{session_id}/events/{event_id}/feedback`` so
the web chat UI can record a thumbs-up or thumbs-down on any of:

- an ``expert.result`` event → emits ``EXPERT_ENDORSE`` / ``EXPERT_OVERRIDE``
- an ``llm.response`` event  → emits ``USER_FEEDBACK``

The endpoint is event-scoped: the caller tells us which specific turn
they're rating by referencing its event id.  That makes the API
idempotent per (user, event) and lets the training-data selector
correlate feedback to the trajectory it's labeling.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from surogates.session.events import EventType
from surogates.session.store import SessionNotFoundError, SessionStore
from surogates.tenant.auth.middleware import get_current_tenant
from surogates.tenant.context import TenantContext

logger = logging.getLogger(__name__)

router = APIRouter()

# Keep in sync with MAX_REASON_LENGTH in web/src/components/chat/tools/expert-tool.tsx.
_MAX_REASON_LENGTH = 500


class FeedbackRequest(BaseModel):
    rating: Literal["up", "down"]
    reason: str | None = Field(default=None, max_length=_MAX_REASON_LENGTH)


class FeedbackResponse(BaseModel):
    event_id: int
    event_type: str


def _get_session_store(request: Request) -> SessionStore:
    store: SessionStore | None = getattr(request.app.state, "session_store", None)
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Session store not available.",
        )
    return store


@router.post(
    "/sessions/{session_id}/events/{event_id}/feedback",
    response_model=FeedbackResponse,
    status_code=status.HTTP_201_CREATED,
)
async def submit_feedback(
    session_id: UUID,
    event_id: int,
    body: FeedbackRequest,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> FeedbackResponse:
    """Record a thumbs-up or thumbs-down on an assistant turn.

    Validates that the session belongs to the caller's org, that the
    referenced event exists and is either an ``expert.result`` or an
    ``llm.response``, and that the caller has not already rated it.
    On repeated calls from the same user the stored event is returned
    unchanged.  Expert results emit ``EXPERT_ENDORSE``/``EXPERT_OVERRIDE``
    (preserving the dedicated expert-feedback path that training and the
    expert feedback loop depend on); regular LLM responses emit
    ``USER_FEEDBACK``.
    """
    store = _get_session_store(request)

    try:
        session, target = await asyncio.gather(
            store.get_session(session_id),
            store.get_event_by_id(session_id, event_id),
        )
    except SessionNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session {session_id} not found.",
        )

    if session.org_id != tenant.org_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session {session_id} not found.",
        )

    if target is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Event {event_id} not found in session {session_id}.",
        )

    if target.type not in (
        EventType.EXPERT_RESULT.value,
        EventType.LLM_RESPONSE.value,
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Feedback can only be submitted on 'expert.result' or "
                f"'llm.response' events; got '{target.type}'."
            ),
        )

    prior = await store.find_user_feedback_on_event(
        session_id, event_id, tenant.user_id,
    )
    if prior is not None:
        return FeedbackResponse(
            event_id=prior.id or 0,
            event_type=prior.type,
        )

    event_data: dict = {
        "target_event_id": event_id,
        "rating": body.rating,
        "rated_by_user_id": str(tenant.user_id),
    }
    if target.type == EventType.EXPERT_RESULT.value:
        event_type = (
            EventType.EXPERT_ENDORSE
            if body.rating == "up"
            else EventType.EXPERT_OVERRIDE
        )
        event_data["expert"] = (target.data or {}).get("expert", "")
    else:  # LLM_RESPONSE
        event_type = EventType.USER_FEEDBACK

    if body.reason:
        event_data["reason"] = body.reason

    emitted_id = await store.emit_event(session_id, event_type, event_data)

    return FeedbackResponse(
        event_id=emitted_id,
        event_type=event_type.value,
    )
