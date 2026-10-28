"""Browser-profile CRUD + setup/capture routes.

The router is dual-mounted (``/v1`` and ``/v1/api``) so both the web app
(``/v1/browser-profiles`` after its proxy strips ``/api``) and the ops proxy
(``/v1/api/browser-profiles``) resolve, mirroring the feedback router.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError

from surogates.api.routes.browser import reaches_its_browser
from surogates.browser.base import BrowserCreditsExhaustedError
from surogates.browser.client import KernelBrowserClient
from surogates.browser.profiles import BrowserProfileRow
from surogates.db.ops_credits import assert_browser_minutes_available
from surogates.runtime.agent_principal import find_agent_principal
from surogates.runtime.resolver import resolve_agent_id_soft
from surogates.session.provisioning import create_agent_session
from surogates.session.store import SessionNotFoundError
from surogates.tenant.auth.middleware import get_current_tenant
from surogates.tenant.context import TenantContext
from surogates.tools.owner_scope import is_ops_chat_service_account

logger = logging.getLogger(__name__)

router = APIRouter()

# A browser-setup session is interactive-only: the user logs in by hand and
# saves. The pod self-terminates at this deadline; nothing persists without an
# explicit capture.
_SETUP_TTL_SECONDS = 15 * 60


class CreateProfileRequest(BaseModel):
    name: str | None = None


class RenameProfileRequest(BaseModel):
    name: str | None = None
    # Only an agent-owned profile: the channels whose sessions use it unbound.
    use_in_channels: list[str] | None = Field(default=None, max_length=32)


#: ``me``: the caller's own login.  ``agent``: one the agent owns, usable by
#: every session of the agent; managed only by its operators (through ops).
Owner = Literal["me", "agent"]


class BrowserProfileOut(BaseModel):
    id: str
    name: str
    source: str
    cookie_domains: list[str]
    created_at: str
    last_used_at: str | None
    has_state: bool
    owner: Owner = "me"
    use_in_channels: list[str] = []

    @classmethod
    def of(cls, row: BrowserProfileRow, owner: Owner = "me") -> "BrowserProfileOut":
        return cls(
            id=str(row.id),
            name=row.name,
            source=row.source,
            cookie_domains=row.cookie_domains,
            created_at=row.created_at.isoformat(),
            last_used_at=(
                row.last_used_at.isoformat() if row.last_used_at else None
            ),
            has_state=row.has_state,
            owner=owner,
            use_in_channels=row.use_in_channels,
        )


def _principal(tenant: TenantContext) -> tuple[UUID | None, UUID | None]:
    """Resolve ``(user_id, service_account_id)`` — exactly one is non-null."""
    if tenant.user_id is not None:
        return tenant.user_id, None
    if tenant.service_account_id is not None:
        return None, tenant.service_account_id
    raise HTTPException(
        status_code=403, detail="Browser profiles require a user principal."
    )


async def _owner_principal(
    request: Request, tenant: TenantContext, owner: Owner,
) -> tuple[UUID | None, UUID | None]:
    """The principal that owns the profiles *owner* names for this caller."""
    if owner == "me":
        return _principal(tenant)
    if not await is_ops_chat_service_account(
        request.app.state.session_store, tenant.service_account_id,
    ):
        raise HTTPException(
            status_code=403, detail="Only the agent's operators manage its own browser logins.",
        )
    agent_id = await resolve_agent_id_soft(request)
    if not agent_id:
        raise HTTPException(status_code=400, detail="no agent_id in request")
    principal = await find_agent_principal(request.app.state.session_factory, tenant.org_id, agent_id)
    if principal is None:
        raise HTTPException(
            status_code=404, detail="This agent has no identity of its own to keep a browser login.",
        )
    return None, principal.id


def _store(request: Request):
    store = getattr(request.app.state, "browser_profile_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="Browser profiles unavailable.")
    return store


@router.get("/browser-profiles")
async def list_profiles(
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
    owner: Owner = Query("me"),
) -> list[BrowserProfileOut]:
    user_id, sa_id = await _owner_principal(request, tenant, owner)
    rows = await _store(request).list(
        tenant.org_id, user_id=user_id, service_account_id=sa_id
    )
    return [BrowserProfileOut.of(r, owner) for r in rows]


@router.post("/browser-profiles", status_code=201)
async def create_profile(
    body: CreateProfileRequest,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
    owner: Owner = Query("me"),
) -> BrowserProfileOut:
    user_id, sa_id = await _owner_principal(request, tenant, owner)
    try:
        row = await _store(request).create(
            tenant.org_id,
            user_id=user_id,
            service_account_id=sa_id,
            name=(body.name or "Profile").strip() or "Profile",
        )
    except IntegrityError as exc:
        raise HTTPException(
            status_code=409, detail="A profile with that name already exists."
        ) from exc
    return BrowserProfileOut.of(row, owner)


@router.patch("/browser-profiles/{profile_id}")
async def update_profile(
    profile_id: UUID,
    body: RenameProfileRequest,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
    owner: Owner = Query("me"),
) -> dict[str, bool]:
    user_id, sa_id = await _owner_principal(request, tenant, owner)
    store = _store(request)
    if body.use_in_channels is not None:
        if owner != "agent":
            raise HTTPException(
                status_code=422,
                detail="Only a login the agent owns is used without being chosen; a personal one is chosen per chat.",
            )
        channels = [c.strip() for c in body.use_in_channels]
        if any(not c or len(c) > 32 or not c.replace("_", "").isalpha() for c in channels):
            raise HTTPException(status_code=422, detail="Channels are names like slack, whatsapp or scheduled.")
        if not await store.set_use_in_channels(
            profile_id, tenant.org_id, service_account_id=sa_id, channels=channels,
        ):
            raise HTTPException(status_code=404, detail="Profile not found")
    if body.name is not None:
        ok = await store.rename(
            profile_id,
            tenant.org_id,
            user_id=user_id,
            service_account_id=sa_id,
            name=body.name.strip(),
        )
        if not ok:
            raise HTTPException(status_code=404, detail="Profile not found")
    return {"renamed": body.name is not None, "updated": True}


@router.delete("/browser-profiles/{profile_id}", status_code=204)
async def delete_profile(
    profile_id: UUID,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
    owner: Owner = Query("me"),
) -> None:
    user_id, sa_id = await _owner_principal(request, tenant, owner)
    await _store(request).delete(
        profile_id, tenant.org_id, user_id=user_id, service_account_id=sa_id
    )


class SetupSessionRequest(BaseModel):
    owner_user_id: str | None = None
    agent_id: str | None = None
    setup_spec: dict | None = None


@router.post("/browser-profiles/{profile_id}/setup-session")
async def create_setup_session(
    profile_id: UUID,
    body: SetupSessionRequest,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
    owner_kind: Owner = Query("me", alias="owner"),
) -> dict[str, str]:
    # The setup browser is always the caller's own, whoever will own the login.
    user_id, sa_id = _principal(tenant)
    owner_user_id, owner_sa_id = await _owner_principal(request, tenant, owner_kind)
    store = _store(request)

    # The profile must exist and belong to its owner before we burn a pod.
    rows = await store.list(tenant.org_id, user_id=owner_user_id, service_account_id=owner_sa_id)
    if not any(r.id == profile_id for r in rows):
        raise HTTPException(status_code=404, detail="Profile not found")

    owner = body.owner_user_id or (
        str(tenant.user_id) if tenant.user_id else None
    )
    if not owner:
        raise HTTPException(
            status_code=403, detail="Setup requires an owner user id."
        )

    setup_spec = body.setup_spec or {}
    if setup_spec.get("proxy") is not None:
        raise HTTPException(
            status_code=400, detail="Egress proxy is not yet supported."
        )

    # Pre-check browser-minutes so the user gets a clean 402 instead of the
    # worker silently failing to provision (the live view would never connect).
    # No-ops when the ops DB isn't configured.
    try:
        await assert_browser_minutes_available(str(tenant.org_id))
    except BrowserCreditsExhaustedError as exc:
        raise HTTPException(status_code=402, detail=str(exc)) from exc

    # Create the browser_setup session and wake it. Provisioning + the control
    # grant happen in the **worker** — the only process that owns a BrowserPool
    # and the fleet credentials — whose loop short-circuits browser_setup
    # sessions (provision + grant control, no agent loop). The owner travels in
    # the config so the worker can grant control to the right user.
    settings = request.app.state.settings
    session = await create_agent_session(
        store=request.app.state.session_store,
        storage=request.app.state.storage,
        settings=settings,
        org_id=tenant.org_id,
        user_id=user_id,
        agent_id=body.agent_id or "browser-setup",
        channel="browser_setup",
        config={
            "browser": {
                "profile_id": str(profile_id),
                "setup_spec": setup_spec,
                "setup_owner_user_id": owner,
                "setup_ttl_seconds": _SETUP_TTL_SECONDS,
            }
        },
        service_account_id=sa_id,
    )
    sid = str(session.id)
    await request.app.state.session_wake(sid)

    expires_at = datetime.now(timezone.utc) + timedelta(seconds=_SETUP_TTL_SECONDS)
    return {"session_id": sid, "expires_at": expires_at.isoformat()}


class CaptureRequest(BaseModel):
    owner_user_id: str | None = None


@router.post("/browser-profiles/{profile_id}/capture")
async def capture_profile(
    profile_id: UUID,
    session_id: UUID,
    body: CaptureRequest,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
    owner_kind: Owner = Query("me", alias="owner"),
) -> BrowserProfileOut:
    user_id, sa_id = await _owner_principal(request, tenant, owner_kind)
    store = _store(request)

    owner = body.owner_user_id or (
        str(tenant.user_id) if tenant.user_id else None
    )
    if not owner:
        raise HTTPException(
            status_code=403, detail="Capture requires an owner user id."
        )

    try:
        session = await request.app.state.session_store.get_session(session_id)
    except SessionNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Session not found") from exc
    # Its browser is its own user's: another member of the org is answered as for no session.
    if not reaches_its_browser(tenant, session):
        raise HTTPException(status_code=404, detail="Session not found")
    # Capture is restricted to the dedicated setup session bound to this
    # profile — it cannot export an arbitrary agent session even if the caller
    # transiently holds its control lease.
    if session.channel != "browser_setup":
        raise HTTPException(status_code=409, detail="Not a browser-setup session.")
    bound = str((session.config or {}).get("browser", {}).get("profile_id"))
    if bound != str(profile_id):
        raise HTTPException(
            status_code=409, detail="Session is not bound to this profile."
        )

    holder = await request.app.state.browser_control.held_by(str(session_id))
    if holder != owner:
        raise HTTPException(status_code=403, detail="Caller does not hold control.")

    resolved = await request.app.state.browser_resolver.resolve(
        str(session_id), expected_org_id=str(tenant.org_id)
    )
    if resolved is None:
        raise HTTPException(status_code=404, detail="No browser for session")

    # The only CDP call permitted while a user-control lease is held: export the
    # post-login cookies + storage the human just established.
    client = KernelBrowserClient(resolved.endpoint.rest_url)
    try:
        state = await client.storage_state()
    finally:
        await client.close()

    row = await store.save_capture(
        profile_id,
        tenant.org_id,
        user_id=user_id,
        service_account_id=sa_id,
        storage_state=state,
    )

    # Release the setup browser promptly: flip the session terminal and re-wake
    # so the worker (which owns the pool) destroys it instead of letting it idle
    # to its pod deadline. Best-effort — the pod TTL is the backstop.
    try:
        await request.app.state.session_store.update_session_status(
            session_id, "completed"
        )
        await request.app.state.session_wake(str(session_id))
    except Exception:
        logger.warning(
            "browser_setup teardown wake failed for %s", session_id, exc_info=True
        )

    return BrowserProfileOut.of(row, owner_kind)
