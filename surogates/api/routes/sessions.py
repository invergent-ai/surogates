"""Session CRUD and message sending."""

from __future__ import annotations

import base64
import logging
from typing import Any
from uuid import UUID

from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text as _sql_text

from surogates.api.session_guards import require_user_writable_session
from surogates.config import INTERRUPT_CHANNEL_PREFIX, enqueue_session
from surogates.session.events import EventType
from surogates.session.models import Session
from surogates.session.provisioning import create_agent_session
from surogates.session.store import SessionNotFoundError, SessionStore
from surogates.storage.tenant import session_workspace_prefix
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

API_CHANNEL = "api"


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class CreateSessionRequest(BaseModel):
    system: str | None = None
    config: dict = Field(default_factory=dict)


_ALLOWED_IMAGE_MIMES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
_MAX_IMAGES_PER_MESSAGE = 5
_MAX_IMAGE_BYTES = 20_000_000  # 20 MB raw


class ImageBlock(BaseModel):
    """A single image attachment on a user message."""

    data: str  # base64-encoded or data: URL
    mime_type: str = "image/png"

    @field_validator("mime_type")
    @classmethod
    def _validate_mime(cls, v: str) -> str:
        if v not in _ALLOWED_IMAGE_MIMES:
            raise ValueError(f"Unsupported image type: {v}")
        return v


class SendMessageRequest(BaseModel):
    content: str
    images: list[ImageBlock] | None = None
    # Free-form per-message metadata.  Used by the platform copilot to
    # carry UI ``view_context`` (the page the user is currently looking
    # at) so the harness can inject a transient system note for the
    # next LLM turn.  The shape is intentionally open — the harness
    # only reads keys it understands and ignores the rest.
    metadata: dict[str, Any] | None = None

    @field_validator("images")
    @classmethod
    def _validate_images(
        cls,
        v: list[ImageBlock] | None,
    ) -> list[ImageBlock] | None:
        if not v:
            return v
        if len(v) > _MAX_IMAGES_PER_MESSAGE:
            raise ValueError(
                f"Maximum {_MAX_IMAGES_PER_MESSAGE} images per message",
            )
        for img in v:
            raw = img.data
            if raw.startswith("data:"):
                _, _, raw = raw.partition(",")
            try:
                decoded = base64.b64decode(raw, validate=True)
            except Exception:
                raise ValueError("Invalid base64 image data")
            if len(decoded) > _MAX_IMAGE_BYTES:
                raise ValueError(
                    f"Image exceeds {_MAX_IMAGE_BYTES // 1_000_000}MB limit",
                )
        return v


class SendMessageResponse(BaseModel):
    event_id: int
    status: str = "processing"


_MAX_SESSION_TITLE_LEN = 256


class UpdateSessionRequest(BaseModel):
    """Body for ``PATCH /sessions/{id}`` — user-driven rename."""

    title: str = Field(..., min_length=1, max_length=_MAX_SESSION_TITLE_LEN)

    @field_validator("title")
    @classmethod
    def _validate_title(cls, v: str) -> str:
        cleaned = v.strip()
        if not cleaned:
            raise ValueError("title must be non-empty")
        if len(cleaned) > _MAX_SESSION_TITLE_LEN:
            raise ValueError(
                f"title exceeds {_MAX_SESSION_TITLE_LEN} characters",
            )
        return cleaned


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
    run_kind: str | None = None  # derived from channel/config, e.g. dynamic_loop
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


def _require_service_account_api_route(
    request: Request,
    tenant: TenantContext,
    *,
    allow_session_scoped: bool = True,
) -> UUID | None:
    """For ``/v1/api/*`` aliases, require a service-account principal."""
    if not request.url.path.startswith("/v1/api/"):
        return None
    if tenant.service_account_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This endpoint requires a service-account token.",
        )
    if not allow_session_scoped and tenant.session_scope_id is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Session-scoped service-account tokens cannot create sessions.",
        )
    return tenant.service_account_id


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


async def _create_session(
    body: CreateSessionRequest,
    request: Request,
    tenant: TenantContext,
    *,
    channel: str,
    user_id: UUID | None,
    service_account_id: UUID | None,
) -> Session:
    """Create a chat session for either the web or service-account channel."""
    store = _get_session_store(request)

    # Model is always set from server config — not user-selectable.
    # ``LLMSettings.model`` carries its own default; if the deployment
    # actively cleared it, that's a misconfiguration we surface up
    # front rather than silently substituting a model that may not
    # exist on the configured provider.
    settings = request.app.state.settings
    if not settings.llm.model:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="LLM model is not configured (settings.llm.model is empty).",
        )
    model = settings.llm.model

    config = body.config.copy()
    if body.system:
        config["system"] = body.system

    session = await create_agent_session(
        store=store,
        storage=request.app.state.storage,
        settings=settings,
        user_id=user_id,
        org_id=tenant.org_id,
        agent_id=settings.agent_id,
        channel=channel,
        model=model,
        config=config,
        service_account_id=service_account_id,
    )

    return session


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post(
    "/sessions",
    response_model=Session,
    status_code=status.HTTP_201_CREATED,
)
async def create_session(
    body: CreateSessionRequest,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> Session:
    """Create a new session for the authenticated user."""
    return await _create_session(
        body,
        request,
        tenant,
        channel="web",
        user_id=tenant.user_id,
        service_account_id=None,
    )


@router.post(
    "/api/sessions",
    response_model=Session,
    status_code=status.HTTP_201_CREATED,
)
async def create_api_session(
    body: CreateSessionRequest,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> Session:
    """Create a new API-channel session for a service-account client."""
    service_account_id = _require_service_account_api_route(
        request,
        tenant,
        allow_session_scoped=False,
    )
    if service_account_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This endpoint requires a service-account token.",
        )
    return await _create_session(
        body,
        request,
        tenant,
        channel=API_CHANNEL,
        user_id=None,
        service_account_id=service_account_id,
    )


@router.post(
    "/api/sessions/{session_id}/messages",
    response_model=SendMessageResponse,
    status_code=status.HTTP_202_ACCEPTED,
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
    _require_service_account_api_route(request, tenant)
    store = _get_session_store(request)
    session = await _get_session_for_tenant(request, session_id, tenant)
    require_user_writable_session(session)

    if session.status not in ("active", "idle", "failed", "paused", "completed"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Session is in '{session.status}' state and cannot accept messages.",
        )

    # Reset terminal/interrupted sessions back to active on new message.
    # A ``SESSION_RESUME`` event must land too -- the web client's
    # ``terminalRef`` latch stays set after *any* terminal event
    # (session.pause / session.fail / session.complete / session.done)
    # and suppresses the running indicators for every subsequent
    # ``llm.delta`` / ``llm.thinking``.  Without the resume event the
    # UI reports "stopped" while deltas for the new turn stream in,
    # regardless of whether the previous terminal state was pause, fail,
    # or completed.
    if session.status in ("failed", "paused", "completed"):
        await store.update_session_status(session_id, "active")
        await store.emit_event(session_id, EventType.SESSION_RESUME, {})

    # Screen user message for prompt injection (AGT PromptInjectionDetector).
    injection_source = (
        "api_channel" if session.channel == API_CHANNEL else "web_channel"
    )
    injection_result = _get_injection_detector().detect(
        body.content,
        source=injection_source,
    )
    if injection_result.is_injection:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(f"Message blocked: {injection_result.explanation}"),
        )

    # Emit the user message event.
    logger.info(
        "send_message: content_len=%d images=%s",
        len(body.content),
        len(body.images) if body.images else 0,
    )
    event_data: dict = {"content": body.content}
    if body.images:
        event_data["images"] = [
            {"data": img.data, "mime_type": img.mime_type} for img in body.images
        ]
    if body.metadata is not None:
        event_data["metadata"] = body.metadata
    event_id = await store.emit_event(
        session_id,
        EventType.USER_MESSAGE,
        event_data,
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


@router.get("/api/sessions/{session_id}", response_model=Session)
@router.get("/sessions/{session_id}", response_model=Session)
async def get_session(
    session_id: UUID,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> Session:
    """Retrieve metadata for a single session."""
    _require_service_account_api_route(request, tenant)
    return await _get_session_for_tenant(request, session_id, tenant)


def _tree_node_from_row(row: dict) -> SessionTreeNode:
    """Convert a ``v_session_tree``-joined row into a :class:`SessionTreeNode`.

    Promotes ``session.config["agent_type"]`` to a first-class field so
    the UI can render sub-agent badges without a second round-trip.
    """
    config = row["config"] or {}
    return SessionTreeNode(
        id=row["session_id"],
        parent_id=row.get("parent_id"),
        root_session_id=row["root_session_id"],
        depth=row["depth"],
        agent_id=row["agent_id"],
        agent_type=config.get("agent_type"),
        run_kind=_session_run_kind(row["channel"], config),
        channel=row["channel"],
        status=row["status"],
        title=row.get("title"),
        model=row.get("model"),
        message_count=row["message_count"],
        tool_call_count=row["tool_call_count"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _session_run_kind(channel: str, config: dict) -> str | None:
    if channel == "scheduled" and config.get("scheduled_dynamic_loop") is True:
        return "dynamic_loop"
    if channel == "scheduled":
        return "scheduled"
    return None


@router.get(
    "/sessions/{session_id}/tree",
    response_model=SessionTreeResponse,
)
async def get_session_tree(
    session_id: UUID,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> SessionTreeResponse:
    """Return the full delegation tree containing *session_id*.

    The response contains every session that shares a root with
    *session_id*: the root, the input session itself, and every
    sub-agent / delegation child of the root, up to
    :data:`_MAX_TREE_NODES` to keep payloads bounded.  Passing a
    sub-agent id returns the same tree as passing its root id, so the
    UI can anchor the sidebar tree on whichever node the user clicked
    without losing siblings.

    Authorization: the input session must belong to this tenant and
    agent.  Other sessions in the tree inherit the root's tenant
    (enforced by ``sessions.org_id`` constraints at session creation
    time), so no per-node authorization is needed beyond the org/agent
    filter applied below.

    Each node carries the session's ``agent_type`` when set (via
    ``session.config.agent_type``) so the frontend can display badges
    for sub-agent types without extra lookups.
    """
    await _get_session_for_tenant(request, session_id, tenant)

    session_factory = request.app.state.session_factory
    agent_id = request.app.state.settings.agent_id

    # ``v_session_tree`` walks the entire forest from every root, so reusing
    # it twice (once for the input → root lookup, once for the descendant
    # walk) doubles a forest-wide cost.  Walk up from :sid to the root, then
    # walk down from that root, in a single pair of bounded recursive CTEs.
    async with session_factory() as db:
        result = await db.execute(
            _sql_text(
                "WITH RECURSIVE up AS ("
                "  SELECT id, parent_id FROM sessions WHERE id = :sid "
                "  UNION ALL "
                "  SELECT s.id, s.parent_id "
                "  FROM sessions s JOIN up u ON s.id = u.parent_id"
                "), "
                "root_id AS (SELECT id FROM up WHERE parent_id IS NULL), "
                "down AS ("
                "  SELECT s.id AS session_id, s.id AS root_session_id, "
                "         s.parent_id, 0 AS depth, s.org_id, s.agent_id, "
                "         s.channel, s.status, s.title, s.model, "
                "         s.created_at, s.updated_at, s.config, "
                "         s.message_count, s.tool_call_count "
                "  FROM sessions s, root_id WHERE s.id = root_id.id "
                "  UNION ALL "
                "  SELECT s.id, d.root_session_id, s.parent_id, d.depth + 1, "
                "         s.org_id, s.agent_id, s.channel, s.status, "
                "         s.title, s.model, s.created_at, s.updated_at, "
                "         s.config, s.message_count, s.tool_call_count "
                "  FROM sessions s JOIN down d ON s.parent_id = d.session_id"
                ") "
                "SELECT * FROM down "
                "WHERE org_id = :org_id AND agent_id = :agent_id "
                "ORDER BY depth, created_at "
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


@router.post("/api/sessions/{session_id}/pause", response_model=Session)
@router.post("/sessions/{session_id}/pause", response_model=Session)
async def pause_session(
    session_id: UUID,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> Session:
    """Pause an active session."""
    _require_service_account_api_route(request, tenant)
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
    require_user_writable_session(session)

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


@router.post("/api/sessions/{session_id}/retry", response_model=Session)
@router.post("/sessions/{session_id}/retry", response_model=Session)
async def retry_session(
    session_id: UUID,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> Session:
    """Retry a failed (or paused) session.

    The retry path re-enqueues the session for ``wake()``.  The harness
    replays from the durable cursor, so the last user message is still
    in scope and the LLM is called again — same code path as a normal
    wake.  Emits ``SESSION_RESUME`` with ``source=user_retry`` so audit
    queries can distinguish user-initiated retries from pause/resume
    flows.
    """
    _require_service_account_api_route(request, tenant)
    store = _get_session_store(request)
    session = await _get_session_for_tenant(request, session_id, tenant)
    require_user_writable_session(session)

    if session.status not in ("failed", "paused"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot retry session in '{session.status}' state.",
        )

    await store.emit_event(
        session_id,
        EventType.SESSION_RESUME,
        {"source": "user_retry"},
    )
    await store.update_session_status(session_id, "active")
    await enqueue_session(request.app.state.redis, session.agent_id, session_id)

    return await store.get_session(session_id)


async def _cleanup_archived_workspaces(
    storage,
    archived_sessions: list[Session],
) -> None:
    """Bulk-delete each archived session's workspace objects.

    Runs out-of-band via ``BackgroundTasks`` so the HTTP response returns
    as soon as the DB archive completes. Failures are logged, not raised
    — the periodic cleanup job sweeps anything left behind.
    """
    for archived_session in archived_sessions:
        storage_bucket = (archived_session.config or {}).get("storage_bucket")
        if not storage_bucket:
            logger.warning(
                "Archived session %s has no agent bucket; skipping "
                "workspace cleanup",
                archived_session.id,
            )
            continue
        prefix = session_workspace_prefix(archived_session.id)
        try:
            deleted = await storage.delete_prefix(storage_bucket, prefix)
            logger.info(
                "Cleaned workspace for archived session %s (%d objects)",
                archived_session.id,
                deleted,
            )
        except Exception:
            logger.warning(
                "Failed to delete workspace prefix %s in bucket %s",
                prefix,
                storage_bucket,
                exc_info=True,
            )


async def _destroy_deleted_session_browser(request: Request, session_id: UUID) -> None:
    session_id_str = str(session_id)
    browser_pool = getattr(request.app.state, "browser_pool", None)
    if browser_pool is not None:
        try:
            await browser_pool.destroy_for_session(session_id_str)
        except Exception:
            logger.warning(
                "Failed to destroy browser sandbox for deleted session %s",
                session_id,
                exc_info=True,
            )

    browser_backend = getattr(request.app.state, "browser_backend", None)
    if browser_backend is not None and hasattr(browser_backend, "destroy_for_session"):
        try:
            await browser_backend.destroy_for_session(session_id_str)
        except Exception:
            logger.warning(
                "Failed to destroy backend browser resources for deleted session %s",
                session_id,
                exc_info=True,
            )

    browser_registry = getattr(request.app.state, "browser_registry", None)
    if browser_registry is not None:
        try:
            await browser_registry.delete(session_id_str)
        except Exception:
            logger.warning(
                "Failed to delete browser registry entry for deleted session %s",
                session_id,
                exc_info=True,
            )


@router.patch("/api/sessions/{session_id}", response_model=Session)
@router.patch("/sessions/{session_id}", response_model=Session)
async def update_session(
    session_id: UUID,
    body: UpdateSessionRequest,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> Session:
    """Update mutable session metadata.

    Currently supports user-driven title renames.  The new title
    overwrites any prior value (auto-generated or user-set), in contrast
    to the harness's background ``update_session_title_if_empty`` which
    only fills an empty title.
    """
    _require_service_account_api_route(request, tenant)
    store = _get_session_store(request)
    session = await _get_session_for_tenant(request, session_id, tenant)
    require_user_writable_session(session)

    await store.update_session_title(session_id, body.title)
    return await store.get_session(session_id)


@router.delete(
    "/api/sessions/{session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
@router.delete(
    "/sessions/{session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_session(
    session_id: UUID,
    request: Request,
    background_tasks: BackgroundTasks,
    tenant: TenantContext = Depends(get_current_tenant),
) -> None:
    """Archive (soft-delete) a session and delete its workspace storage."""
    _require_service_account_api_route(request, tenant)
    store = _get_session_store(request)
    session = await _get_session_for_tenant(request, session_id, tenant)
    require_user_writable_session(session)

    archived_sessions = await store.archive_session_tree_and_delete_schedules(
        session_id,
        org_id=session.org_id,
        agent_id=session.agent_id,
    )

    for archived_session in archived_sessions:
        await _destroy_deleted_session_browser(request, archived_session.id)

    # Interrupt workers so active parent/child harnesses stop processing and
    # destroy any sandbox/browser pods.
    redis = request.app.state.redis
    import json as _json

    for archived_session in archived_sessions:
        await redis.publish(
            f"{INTERRUPT_CHANNEL_PREFIX}:{archived_session.id}",
            _json.dumps({"reason": "session deleted"}),
        )

    # The primary session must have a bucket so we can validate the route up
    # front; child workspaces with missing buckets are tolerated and skipped
    # in the background cleanup.
    primary_bucket = (session.config or {}).get("storage_bucket")
    if not primary_bucket:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Session {session_id} has no agent bucket.",
        )

    # Workspace cleanup runs after the response is sent. The session is
    # already archived in the DB, so it disappears from the UI immediately;
    # the cleanup CronJob (jobs/cleanup_sessions.py) sweeps anything left
    # behind if this task fails or the process crashes.
    background_tasks.add_task(
        _cleanup_archived_workspaces,
        request.app.state.storage,
        archived_sessions,
    )
