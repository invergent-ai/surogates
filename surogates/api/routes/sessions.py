"""Session CRUD and message sending."""

from __future__ import annotations

import logging
from uuid import UUID, uuid4

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import text as _sql_text

from surogates.config import INTERRUPT_CHANNEL_PREFIX, enqueue_session
from surogates.session.events import EventType
from surogates.session.models import Session
from surogates.session.store import SessionNotFoundError, SessionStore
from surogates.tenant.auth.middleware import get_current_tenant
from surogates.tenant.context import TenantContext

logger = logging.getLogger(__name__)

# Lazy singleton for the AGT PromptInjectionDetector — screens user
# messages before they enter the event log.
_injection_detector = None


def _get_injection_detector():
    """Return a shared PromptInjectionDetector instance."""
    global _injection_detector
    if _injection_detector is None:
        from agent_os.prompt_injection import PromptInjectionDetector
        _injection_detector = PromptInjectionDetector()
    return _injection_detector

router = APIRouter()


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class CreateSessionRequest(BaseModel):
    system: str | None = None
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


class SessionTreeNode(BaseModel):
    """One node in the session tree -- a session plus its lineage metadata."""

    id: UUID
    parent_id: UUID | None = None
    root_session_id: UUID
    depth: int
    agent_id: str
    agent_type: str | None = None  # from session.config.agent_type
    channel: str
    status: str
    title: str | None = None
    model: str | None = None
    message_count: int = 0
    tool_call_count: int = 0
    created_at: datetime
    updated_at: datetime


class SessionTreeResponse(BaseModel):
    nodes: list[SessionTreeNode]
    total: int


class SessionChildrenResponse(BaseModel):
    children: list[SessionTreeNode]
    total: int


# Safety cap on tree depth to keep responses bounded.
_MAX_TREE_NODES: int = 200


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
    request: Request,
    session_id: UUID,
    tenant: TenantContext,
) -> Session:
    """Fetch a session and verify it belongs to the tenant's org and this agent.

    Also enforces session-scoped JWTs (the worker-minted
    ``service_account_session`` token type) — such a context may only
    touch the one session its token was minted for.  All failure
    modes — missing, wrong org, wrong agent, cross-session — collapse
    into the same 404 so callers cannot probe session existence across
    scopes.
    """
    store = _get_session_store(request)
    try:
        session = await store.get_session(session_id)
    except SessionNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session {session_id} not found.",
        )

    agent_id = request.app.state.settings.agent_id
    if session.agent_id != agent_id or not tenant.owns_session(
        session.org_id, session_id
    ):
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

    # Model is always set from server config — not user-selectable.
    settings = request.app.state.settings
    model = settings.llm.model or "gpt-5.4"

    config = body.config.copy()
    if body.system:
        config["system"] = body.system

    # Each session gets its own storage bucket for workspace files.
    session_id = uuid4()
    session_bucket = f"session-{session_id}"

    storage = request.app.state.storage
    await storage.create_bucket(session_bucket)

    config["workspace_bucket"] = session_bucket
    # Workspace path for the sandbox:
    # - LocalBackend: resolves to the bucket directory on disk
    # - S3Backend: /workspace (s3fs-fuse mount point inside the sandbox pod)
    config["workspace_path"] = storage.resolve_bucket_path(session_bucket)

    session = await store.create_session(
        session_id=session_id,
        user_id=tenant.user_id,
        org_id=tenant.org_id,
        agent_id=settings.agent_id,
        channel="web",
        model=model,
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
    session = await _get_session_for_tenant(request, session_id, tenant)

    if session.status not in ("active", "idle", "failed", "paused"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Session is in '{session.status}' state and cannot accept messages.",
        )

    # Reset failed/paused sessions back to active on new message.
    if session.status in ("failed", "paused"):
        await store.update_session_status(session_id, "active")

    # Screen user message for prompt injection (AGT PromptInjectionDetector).
    injection_result = _get_injection_detector().detect(
        body.content, source="web_channel"
    )
    if injection_result.is_injection:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Message blocked: {injection_result.explanation}"
            ),
        )

    # Emit the user message event.
    event_id = await store.emit_event(
        session_id,
        EventType.USER_MESSAGE,
        {"content": body.content},
    )

    # Enqueue the session for processing on its agent's dedicated queue.
    await enqueue_session(request.app.state.redis, session.agent_id, session_id)

    return SendMessageResponse(event_id=event_id)


@router.post(
    "/sessions/{session_id}/confirm-disclosure",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def confirm_disclosure(
    session_id: UUID,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> None:
    """Confirm AI disclosure for a session (EU AI Act Art. 50).

    Must be called before the agent can execute tools when transparency
    enforcement is enabled.  Typically called by the frontend after
    showing the AI disclosure notice to the user.
    """
    await _get_session_for_tenant(request, session_id, tenant)

    governance = getattr(request.app.state, "governance_gate", None)
    if governance is not None:
        governance.confirm_disclosure(str(session_id))


@router.get("/sessions/{session_id}", response_model=Session)
async def get_session(
    session_id: UUID,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> Session:
    """Retrieve metadata for a single session."""
    return await _get_session_for_tenant(request, session_id, tenant)


def _tree_node_from_row(row: dict) -> SessionTreeNode:
    """Convert a ``v_session_tree``-joined row into a :class:`SessionTreeNode`.

    Promotes ``session.config["agent_type"]`` to a first-class field so
    the UI can render sub-agent badges without a second round-trip.
    """
    return SessionTreeNode(
        id=row["session_id"],
        parent_id=row.get("parent_id"),
        root_session_id=row["root_session_id"],
        depth=row["depth"],
        agent_id=row["agent_id"],
        agent_type=row["config"].get("agent_type"),
        channel=row["channel"],
        status=row["status"],
        title=row.get("title"),
        model=row.get("model"),
        message_count=row["message_count"],
        tool_call_count=row["tool_call_count"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


@router.get(
    "/sessions/{session_id}/tree", response_model=SessionTreeResponse,
)
async def get_session_tree(
    session_id: UUID,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> SessionTreeResponse:
    """Return the full recursive descendant tree rooted at *session_id*.

    The response contains the session itself plus every sub-agent /
    delegation child, up to :data:`_MAX_TREE_NODES` to keep payloads
    bounded.  Authorization: the root session must belong to this
    tenant and agent.  Children inherit the root's tenant (enforced by
    ``sessions.org_id`` constraints at session creation time), so no
    per-child authorization is needed.

    Each node carries the session's ``agent_type`` when set (via
    ``session.config.agent_type``) so the frontend can display badges
    for sub-agent types without extra lookups.
    """
    await _get_session_for_tenant(request, session_id, tenant)

    session_factory = request.app.state.session_factory
    agent_id = request.app.state.settings.agent_id

    async with session_factory() as db:
        result = await db.execute(
            _sql_text(
                "SELECT t.*, s.config, s.message_count, s.tool_call_count "
                "FROM v_session_tree t "
                "JOIN sessions s ON s.id = t.session_id "
                "WHERE t.root_session_id = :sid "
                "AND s.org_id = :org_id "
                "AND s.agent_id = :agent_id "
                "ORDER BY t.depth, t.created_at "
                "LIMIT :limit"
            ),
            {
                "sid": session_id,
                "org_id": tenant.org_id,
                "agent_id": agent_id,
                "limit": _MAX_TREE_NODES,
            },
        )
        nodes = [_tree_node_from_row(dict(r._mapping)) for r in result]

    return SessionTreeResponse(nodes=nodes, total=len(nodes))


@router.get(
    "/sessions/{session_id}/children",
    response_model=SessionChildrenResponse,
)
async def get_session_children(
    session_id: UUID,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> SessionChildrenResponse:
    """Return the direct children (one level) of a session.

    Useful for incrementally expanding the session tree in the UI
    without fetching the full descendant subtree up-front.
    Authorization: the parent session must belong to this tenant and
    agent; child rows inherit tenancy.
    """
    await _get_session_for_tenant(request, session_id, tenant)

    session_factory = request.app.state.session_factory
    agent_id = request.app.state.settings.agent_id

    async with session_factory() as db:
        result = await db.execute(
            _sql_text(
                "SELECT t.*, s.config, s.message_count, s.tool_call_count "
                "FROM v_session_tree t "
                "JOIN sessions s ON s.id = t.session_id "
                "WHERE t.parent_id = :sid "
                "AND s.org_id = :org_id "
                "AND s.agent_id = :agent_id "
                "ORDER BY s.created_at"
            ),
            {
                "sid": session_id,
                "org_id": tenant.org_id,
                "agent_id": agent_id,
            },
        )
        children = [_tree_node_from_row(dict(r._mapping)) for r in result]

    return SessionChildrenResponse(children=children, total=len(children))


@router.get("/sessions", response_model=ListSessionsResponse)
async def list_sessions(
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
    limit: int = 50,
    offset: int = 0,
) -> ListSessionsResponse:
    """List the authenticated user's sessions for this agent, newest first."""
    store = _get_session_store(request)
    settings = request.app.state.settings

    if limit < 1:
        limit = 1
    if limit > 200:
        limit = 200
    if offset < 0:
        offset = 0

    sessions = await store.list_sessions(
        org_id=tenant.org_id,
        user_id=tenant.user_id,
        agent_id=settings.agent_id,
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
    session = await _get_session_for_tenant(request, session_id, tenant)

    if session.status not in ("active", "processing", "paused"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot pause session in '{session.status}' state.",
        )

    # Only emit event + update status if not already paused.
    if session.status != "paused":
        await store.emit_event(session_id, EventType.SESSION_PAUSE, {})
        await store.update_session_status(session_id, "paused")

    # Always publish the interrupt signal — the harness may still be
    # running even if the DB status is already "paused" (race condition
    # between status update and harness loop iteration).
    redis = request.app.state.redis
    import json as _json
    await redis.publish(
        f"{INTERRUPT_CHANNEL_PREFIX}:{session_id}",
        _json.dumps({"reason": "paused by user"}),
    )

    return await store.get_session(session_id)


@router.post("/sessions/{session_id}/resume", response_model=Session)
async def resume_session(
    session_id: UUID,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> Session:
    """Resume a paused session."""
    store = _get_session_store(request)
    session = await _get_session_for_tenant(request, session_id, tenant)

    if session.status != "paused":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot resume session in '{session.status}' state.",
        )

    await store.emit_event(session_id, EventType.SESSION_RESUME, {})
    await store.update_session_status(session_id, "active")

    # Re-enqueue so the worker picks it up.
    await enqueue_session(request.app.state.redis, session.agent_id, session_id)

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
    """Archive (soft-delete) a session and delete its workspace storage."""
    store = _get_session_store(request)
    await _get_session_for_tenant(request, session_id, tenant)

    await store.update_session_status(session_id, "archived")

    # Interrupt the worker so it stops processing and destroys the sandbox pod.
    redis = request.app.state.redis
    import json as _json
    await redis.publish(
        f"{INTERRUPT_CHANNEL_PREFIX}:{session_id}",
        _json.dumps({"reason": "session deleted"}),
    )

    # Delete the session's storage bucket (workspace files).
    storage = request.app.state.storage
    session_bucket = f"session-{session_id}"
    try:
        await storage.delete_bucket(session_bucket)
    except Exception:
        logger.warning("Failed to delete storage bucket %s", session_bucket, exc_info=True)
