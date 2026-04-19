"""Feedback on assistant turns.

Exposes ``POST /sessions/{session_id}/events/{event_id}/feedback`` so
every principal that can reach Surogates can record a rating on:

- an ``expert.result`` event → emits ``EXPERT_ENDORSE`` / ``EXPERT_OVERRIDE``
- an ``llm.response`` event  → emits ``USER_FEEDBACK``

The router is mounted twice by the app: under ``/v1`` (interactive users
via JWT, e.g. the web chat UI clicking thumbs) and under ``/v1/api``
(service-account clients such as automated judges grading pipeline
output).  The handler is source-agnostic — it records ``source: "user"``
for JWT callers and ``source: "judge"`` for service-account callers so
downstream training-data selectors can weight the two differently.
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
from surogates.tenant.context import PrincipalKind, TenantContext

logger = logging.getLogger(__name__)

router = APIRouter()

# Keep in sync with MAX_REASON_LENGTH in web/src/components/chat/tools/expert-tool.tsx.
_MAX_REASON_LENGTH = 500
_MAX_RATIONALE_LENGTH = 10_000


class FeedbackRequest(BaseModel):
    """Rating on an assistant turn.

    The minimum payload is ``rating``; everything else is optional
    passthrough persisted on the event so training-data selectors can
    read richer signals when the client supplies them (typically a
    judge).  ``reason`` is the short human-style explanation the web UI
    collects; ``rationale`` is the longer free-form text a judge
    produces; ``score`` and ``criteria`` carry numeric grades when the
    principal is model-based.
    """

    rating: Literal["up", "down"]
    reason: str | None = Field(default=None, max_length=_MAX_REASON_LENGTH)
    score: float | None = Field(default=None, ge=0.0, le=1.0)
    criteria: dict[str, float] | None = None
    rationale: str | None = Field(default=None, max_length=_MAX_RATIONALE_LENGTH)


class FeedbackResponse(BaseModel):
    event_id: int
    event_type: str
    source: PrincipalKind


def _get_session_store(request: Request) -> SessionStore:
    store: SessionStore | None = getattr(request.app.state, "session_store", None)
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Session store not available.",
        )
    return store


# JSONB payload keys used both here and by `find_feedback_on_event` /
# the audit views — kept as module-level names so a future rename
# touches one place.
_RATED_BY_USER_ID = "rated_by_user_id"
_RATED_BY_SERVICE_ACCOUNT_ID = "rated_by_service_account_id"

_PRINCIPAL_EVENT_KEY: dict[PrincipalKind, str] = {
    PrincipalKind.USER: _RATED_BY_USER_ID,
    PrincipalKind.JUDGE: _RATED_BY_SERVICE_ACCOUNT_ID,
}


def _require_sa_for_api_prefix(
    request: Request, tenant: TenantContext,
) -> None:
    """Refuse JWT callers on the ``/v1/api/*`` mount of this route.

    The same handler is mounted under both ``/v1`` (web UI / interactive
    JWTs) and ``/v1/api`` (automated judges with SA tokens).  Without
    this guard a user JWT could also reach the ``/v1/api`` URL and land
    feedback labelled ``source="user"`` under the programmatic prefix —
    correct-but-confusing.  Mirror the stricter ``_require_service_account``
    check from ``prompts.py`` so the two programmatic routes agree.
    """
    if request.url.path.startswith("/v1/api/") and tenant.service_account_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "The /v1/api/ feedback route requires a service-account "
                "token; interactive users should POST to /v1/..."
            ),
        )


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
    """Record a rating on an ``expert.result`` or ``llm.response`` event.

    Validates that the session belongs to the caller's org (and to the
    caller's session scope, for session-scoped SA JWTs), that the
    referenced event exists and is a supported type, and that the
    caller has not already rated it.  On repeated calls from the same
    principal the stored event is returned unchanged.  Expert results
    emit ``EXPERT_ENDORSE``/``EXPERT_OVERRIDE`` (preserving the
    dedicated expert-feedback path that training and the expert
    feedback loop depend on); regular LLM responses emit
    ``USER_FEEDBACK``.
    """
    _require_sa_for_api_prefix(request, tenant)
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

    if not tenant.owns_session(session.org_id, session_id):
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

    source, principal_id = tenant.principal()
    prior = await store.find_feedback_on_event(
        session_id,
        event_id,
        user_id=tenant.user_id,
        service_account_id=tenant.service_account_id,
    )
    if prior is not None:
        return FeedbackResponse(
            event_id=prior.id or 0,
            event_type=prior.type,
            source=source,
        )

    # ``source`` duplicates what the ``rated_by_*_id`` shape already
    # encodes, but persisting it makes the JSONB audit views trivial
    # (plain COALESCE vs. nested CASE) and lets selectors filter on one
    # column.
    event_data: dict = {
        "target_event_id": event_id,
        "rating": body.rating,
        "source": source.value,
        _PRINCIPAL_EVENT_KEY[source]: str(principal_id),
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
    if body.rationale:
        event_data["rationale"] = body.rationale
    if body.score is not None:
        event_data["score"] = body.score
    if body.criteria:
        event_data["criteria"] = body.criteria

    emitted_id = await store.emit_event(session_id, event_type, event_data)

    return FeedbackResponse(
        event_id=emitted_id,
        event_type=event_type.value,
        source=source,
    )
