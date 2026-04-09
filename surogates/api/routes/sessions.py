"""Session CRUD and message sending."""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from surogates.session.events import EventType
from surogates.session.models import Session
from surogates.session.store import SessionNotFoundError, SessionStore
from surogates.tenant.auth.middleware import get_current_tenant
from surogates.tenant.context import TenantContext

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class WorkspaceConfig(BaseModel):
    mode: str = "ephemeral"


class CreateSessionRequest(BaseModel):
    model: str = "gpt-4o"
    system: str | None = None
    workspace: WorkspaceConfig = Field(default_factory=WorkspaceConfig)
    config: dict = Field(default_factory=dict)


class CreateSessionResponse(BaseModel):
    id: UUID
    status: str
    channel: str
    model: str | None = None


class SendMessageRequest(BaseModel):
    content: str


class SendMessageResponse(BaseModel):
    event_id: int
    status: str = "processing"


class ListSessionsResponse(BaseModel):
    sessions: list[Session]
    total: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_session_store(request: Request) -> SessionStore:
    """Retrieve the SessionStore from app state."""
    store: SessionStore | None = getattr(request.app.state, "session_store", None)
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Session store not available.",
        )
    return store


async def _get_session_for_tenant(
    store: SessionStore, session_id: UUID, tenant: TenantContext
) -> Session:
    """Fetch a session and verify it belongs to the tenant's org."""
    try:
        session = await store.get_session(session_id)
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

    return session


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post(
    "/sessions",
    response_model=CreateSessionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_session(
    body: CreateSessionRequest,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> CreateSessionResponse:
    """Create a new session for the authenticated user."""
    store = _get_session_store(request)

    config = body.config.copy()
    if body.system:
        config["system"] = body.system
    config["workspace"] = body.workspace.model_dump()

    session = await store.create_session(
        user_id=tenant.user_id,
        org_id=tenant.org_id,
        channel="web",
        model=body.model,
        config=config,
    )

    return CreateSessionResponse(
        id=session.id,
        status=session.status,
        channel=session.channel,
        model=session.model,
    )


@router.post(
    "/sessions/{session_id}/messages",
    response_model=SendMessageResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def send_message(
    session_id: UUID,
    body: SendMessageRequest,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> SendMessageResponse:
    """Send a user message to a session, triggering agent processing."""
    store = _get_session_store(request)
    session = await _get_session_for_tenant(store, session_id, tenant)

    if session.status not in ("active", "idle"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Session is in '{session.status}' state and cannot accept messages.",
        )

    # Emit the user message event.
    event_id = await store.emit_event(
        session_id,
        EventType.USER_MESSAGE,
        {"content": body.content},
    )

    # Enqueue the session for processing.
    redis = request.app.state.redis
    await redis.zadd(
        "surogates:work_queue",
        {str(session_id): 0},  # priority 0
    )

    return SendMessageResponse(event_id=event_id)


@router.get("/sessions/{session_id}", response_model=Session)
async def get_session(
    session_id: UUID,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> Session:
    """Retrieve metadata for a single session."""
    store = _get_session_store(request)
    return await _get_session_for_tenant(store, session_id, tenant)


@router.get("/sessions", response_model=ListSessionsResponse)
async def list_sessions(
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
    limit: int = 50,
    offset: int = 0,
) -> ListSessionsResponse:
    """List the authenticated user's sessions (paginated, newest first)."""
    store = _get_session_store(request)

    if limit < 1:
        limit = 1
    if limit > 200:
        limit = 200
    if offset < 0:
        offset = 0

    sessions = await store.list_sessions(
        org_id=tenant.org_id,
        user_id=tenant.user_id,
        limit=limit,
        offset=offset,
    )

    # For total count we fetch one extra page to determine if there are more.
    # A production system would use COUNT(*) but this avoids a second query
    # for the common case.
    total = offset + len(sessions)

    return ListSessionsResponse(sessions=sessions, total=total)


@router.post("/sessions/{session_id}/pause", response_model=Session)
async def pause_session(
    session_id: UUID,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> Session:
    """Pause an active session."""
    store = _get_session_store(request)
    session = await _get_session_for_tenant(store, session_id, tenant)

    if session.status != "active":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot pause session in '{session.status}' state.",
        )

    await store.emit_event(session_id, EventType.SESSION_PAUSE, {})
    await store.update_session_status(session_id, "paused")

    return await store.get_session(session_id)


@router.post("/sessions/{session_id}/resume", response_model=Session)
async def resume_session(
    session_id: UUID,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> Session:
    """Resume a paused session."""
    store = _get_session_store(request)
    session = await _get_session_for_tenant(store, session_id, tenant)

    if session.status != "paused":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot resume session in '{session.status}' state.",
        )

    await store.emit_event(session_id, EventType.SESSION_RESUME, {})
    await store.update_session_status(session_id, "active")

    # Re-enqueue so the worker picks it up.
    redis = request.app.state.redis
    await redis.zadd("surogates:work_queue", {str(session_id): 0})

    return await store.get_session(session_id)


@router.delete(
    "/sessions/{session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_session(
    session_id: UUID,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> None:
    """Archive (soft-delete) a session."""
    store = _get_session_store(request)
    await _get_session_for_tenant(store, session_id, tenant)

    await store.update_session_status(session_id, "archived")
