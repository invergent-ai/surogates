"""Memory REST API — read and mutate persistent memory entries.

Reads from and writes to the same per-user R2 store the harness uses
at session time, keyed per-target (``"memory"`` / ``"user"``).  The
in-memory contract: one R2 object per (storage_key_prefix, user_id,
target), each carrying a JSON envelope with version + content.

All endpoints are tenant-scoped via ``TenantContext``; the agent
runtime context (resolved from ``?agent_id=`` or the Host header
subdomain slug) supplies the ``storage_key_prefix`` so every write
lands under the agent's slice of the shared memory bucket.
"""

from __future__ import annotations

import logging
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from surogates.api.routes._shared import require_not_channel_principal
from surogates.channels.memory_boundary import session_memory_boundary
from surogates.memory.r2_store import (
    MEMORY_TARGETS, R2MemoryStore,
)
from surogates.memory.store import scan_memory_content
from surogates.runtime import AgentRuntimeContext, agent_runtime_context_dep
from surogates.runtime.memory_protocol import memory_object_key
from surogates.tenant.auth.middleware import get_current_tenant
from surogates.tenant.context import TenantContext

logger = logging.getLogger(__name__)

router = APIRouter()

# Character limits — kept in sync with the harness memory tool's
# advisory ceilings so writes from the UI and the agent both surface
# the same "would exceed limit" guard.
_MEMORY_CHAR_LIMIT = 2200
_USER_CHAR_LIMIT = 1375


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class MemoryEntries(BaseModel):
    """Current state of both memory targets."""

    memory: list[str]
    user: list[str]
    memory_usage: str
    user_usage: str


class MemoryMutateRequest(BaseModel):
    action: Literal["add", "replace", "remove"]
    target: Literal["memory", "user"]
    content: str | None = None
    old_text: str | None = None


class MemoryMutateResponse(BaseModel):
    success: bool
    message: str | None = None
    error: str | None = None
    entries: list[str] | None = None
    usage: str | None = None
    entry_count: int | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _format_usage(entries: list[str], limit: int) -> str:
    """Format a usage string like '45% — 990/2200 chars'."""
    used = sum(len(e) for e in entries)
    pct = int(used / limit * 100) if limit > 0 else 0
    return f"{pct}% — {used}/{limit} chars"


def _char_limit(target: str) -> int:
    return _MEMORY_CHAR_LIMIT if target == "memory" else _USER_CHAR_LIMIT


async def _session_boundary(
    request: Request,
    tenant: TenantContext,
    agent_runtime: AgentRuntimeContext,
    session_id: UUID | None,
) -> str | None:
    """The memory boundary of ``session_id``, or ``None`` for no boundary.

    Without this the route composed its keys from ``storage_key_prefix``
    and the tenant's ``user_id`` alone, so a boundary-scoped session
    (any managed-channel thread, and every evaluation row) wrote through
    the API client into the per-user — or, for a service account whose
    ``user_id`` is ``None``, the org-*shared* — memory the agent really
    uses, while its worker read the boundary partition.  Writes vanished
    from the agent's point of view and landed in a store the session was
    supposed to be isolated from.

    ``session_id`` is optional so the Studio memory panel, which is
    talking about a user's memory rather than any one session, keeps
    working unchanged.  Every failure mode collapses into 404, matching
    the session routes: a caller must not be able to probe which session
    ids exist outside its scope.

    A caller that omits it entirely falls back to ``tenant.session_scope_id``
    — the session id baked into a worker-minted ``service_account_session``
    JWT (see ``create_service_account_session_token``).  Without this,
    enforcement depended on ``HarnessAPIClient`` also being told the session
    id and choosing to forward it as a query param: a client built without
    one (its constructor defaults ``session_id`` to ``None``) would silently
    write an evaluation session's memory into shared memory again, with no
    server-side signal.  A regular user JWT never carries
    ``session_scope_id``, so this leaves the Studio panel's own lookup
    (``session_id`` query param and JWT both absent) untouched.
    """
    if session_id is None:
        session_id = tenant.session_scope_id
    if session_id is None:
        return None
    from surogates.session.store import SessionNotFoundError

    store = getattr(request.app.state, "session_store", None)
    if store is None:
        raise HTTPException(
            status_code=503, detail="Session store not available.",
        )
    try:
        session = await store.get_session(session_id)
    except SessionNotFoundError:
        raise HTTPException(
            status_code=404, detail=f"Session {session_id} not found.",
        )
    if session.agent_id != agent_runtime.agent_id or not tenant.owns_session(
        session.org_id, session_id,
    ):
        raise HTTPException(
            status_code=404, detail=f"Session {session_id} not found.",
        )
    return session_memory_boundary(session)


async def _build_store(
    request: Request,
    tenant: TenantContext,
    agent_runtime: AgentRuntimeContext,
    session_id: UUID | None = None,
) -> R2MemoryStore:
    """Construct an R2MemoryStore for this request and load its blobs.

    Uses the same key layout the harness's worker uses (one R2 object
    per (boundary-or-user, target)) so writes from this endpoint and
    writes from the agent's memory tool land on the same bytes.
    """
    settings = request.app.state.settings
    bucket = (
        settings.storage.memory_bucket or settings.storage.bucket
    )
    if not bucket:
        raise HTTPException(
            status_code=503,
            detail="memory bucket is not configured",
        )
    boundary = await _session_boundary(
        request, tenant, agent_runtime, session_id,
    )
    user_id = str(tenant.user_id) if tenant.user_id is not None else None
    keys = {
        target: memory_object_key(
            storage_key_prefix=agent_runtime.storage_key_prefix,
            user_id=user_id,
            boundary=boundary,
            target=target,
        )
        for target in MEMORY_TARGETS
    }
    store = R2MemoryStore(
        backend=request.app.state.storage,
        bucket=bucket,
        keys=keys,
    )
    await store.load_from_r2()
    return store


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/memory", response_model=MemoryEntries)
async def get_memory(
    request: Request,
    session_id: UUID | None = Query(default=None),
    tenant: TenantContext = Depends(get_current_tenant),
    agent_runtime: AgentRuntimeContext = Depends(agent_runtime_context_dep),
) -> MemoryEntries:
    """Load current memory entries for both targets.

    ``session_id`` scopes the read to that session's memory boundary; the
    harness client always sends it so an agent reads the same partition it
    writes.  Absent (the Studio memory panel) keeps the per-user layout.
    """
    require_not_channel_principal(tenant)
    store = await _build_store(request, tenant, agent_runtime, session_id)

    memory_entries = store.get_entries("memory")
    user_entries = store.get_entries("user")

    return MemoryEntries(
        memory=memory_entries,
        user=user_entries,
        memory_usage=_format_usage(memory_entries, _MEMORY_CHAR_LIMIT),
        user_usage=_format_usage(user_entries, _USER_CHAR_LIMIT),
    )


@router.post("/memory", response_model=MemoryMutateResponse)
async def mutate_memory(
    body: MemoryMutateRequest,
    request: Request,
    session_id: UUID | None = Query(default=None),
    tenant: TenantContext = Depends(get_current_tenant),
    agent_runtime: AgentRuntimeContext = Depends(agent_runtime_context_dep),
) -> MemoryMutateResponse:
    """Add, replace, or remove a memory entry.

    ``session_id`` scopes the write to that session's memory boundary — see
    :func:`_session_boundary`.  Without it a boundary-scoped session's write
    lands in the agent's real per-user or org-shared memory.
    """
    require_not_channel_principal(tenant)
    store = await _build_store(request, tenant, agent_runtime, session_id)

    limit = _char_limit(body.target)
    current_entries = store.get_entries(body.target)

    if body.action == "add":
        if not body.content:
            raise HTTPException(
                status_code=422,
                detail="content is required for 'add' action.",
            )
        scan_err = scan_memory_content(body.content)
        if scan_err:
            return MemoryMutateResponse(success=False, error=scan_err)
        used = sum(len(e) for e in current_entries) + len(body.content)
        if used > limit:
            return MemoryMutateResponse(
                success=False,
                error=(
                    f"Would exceed {body.target} char limit "
                    f"({used}/{limit}). Remove or replace an entry first."
                ),
            )
        await store.add(body.target, body.content)

    elif body.action == "replace":
        if not body.old_text:
            raise HTTPException(
                status_code=422,
                detail="old_text is required for 'replace' action.",
            )
        if not body.content:
            raise HTTPException(
                status_code=422,
                detail="content is required for 'replace' action.",
            )
        scan_err = scan_memory_content(body.content)
        if scan_err:
            return MemoryMutateResponse(success=False, error=scan_err)
        match = next(
            (e for e in current_entries if body.old_text in e),
            None,
        )
        if match is None:
            return MemoryMutateResponse(
                success=False,
                error=(
                    f"No entry containing '{body.old_text[:50]}' "
                    f"found in {body.target}."
                ),
            )
        # Char-limit pre-check using the would-be content.
        used = (
            sum(len(e) for e in current_entries)
            - len(match) + len(body.content)
        )
        if used > limit:
            return MemoryMutateResponse(
                success=False,
                error=(
                    f"Would exceed {body.target} char limit "
                    f"({used}/{limit})."
                ),
            )
        result = await store.replace(body.target, match, body.content)
        if result.get("error"):
            return MemoryMutateResponse(success=False, error=result["error"])

    elif body.action == "remove":
        if not body.old_text:
            raise HTTPException(
                status_code=422,
                detail="old_text is required for 'remove' action.",
            )
        match = next(
            (e for e in current_entries if body.old_text in e),
            None,
        )
        if match is None:
            return MemoryMutateResponse(
                success=False,
                error=(
                    f"No entry containing '{body.old_text[:50]}' "
                    f"found in {body.target}."
                ),
            )
        # ``R2MemoryStore.remove`` strips ``old_text`` substring-style;
        # pass the full matching entry so the delimiter goes with it
        # rather than leaving a dangling separator.
        result = await store.remove(body.target, match)
        if result.get("error"):
            return MemoryMutateResponse(success=False, error=result["error"])

    else:
        raise HTTPException(
            status_code=422, detail=f"Unknown action '{body.action}'.",
        )

    entries = store.get_entries(body.target)
    return MemoryMutateResponse(
        success=True,
        message=f"Entry {'added' if body.action == 'add' else body.action + 'd'}.",
        entries=entries,
        usage=_format_usage(entries, limit),
        entry_count=len(entries),
    )
