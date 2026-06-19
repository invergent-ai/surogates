"""Browser-profile CRUD + setup/capture routes (mounted under ``/v1``)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from surogates.browser.base import BrowserCreditsExhaustedError, BrowserSpec
from surogates.browser.client import KernelBrowserClient
from surogates.browser.profiles import BrowserProfileRow
from surogates.session.provisioning import create_agent_session
from surogates.tenant.auth.middleware import get_current_tenant
from surogates.tenant.context import TenantContext

router = APIRouter()

# A browser-setup session is interactive-only: the user logs in by hand and
# saves. The pod self-terminates at this deadline; nothing persists without an
# explicit capture.
_SETUP_TTL_SECONDS = 15 * 60


class CreateProfileRequest(BaseModel):
    name: str | None = None


class RenameProfileRequest(BaseModel):
    name: str


class BrowserProfileOut(BaseModel):
    id: str
    name: str
    source: str
    cookie_domains: list[str]
    created_at: str
    last_used_at: str | None
    has_state: bool

    @classmethod
    def of(cls, row: BrowserProfileRow) -> "BrowserProfileOut":
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


def _store(request: Request):
    store = getattr(request.app.state, "browser_profile_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="Browser profiles unavailable.")
    return store


@router.get("/api/browser-profiles")
async def list_profiles(
    request: Request, tenant: TenantContext = Depends(get_current_tenant)
) -> list[BrowserProfileOut]:
    user_id, sa_id = _principal(tenant)
    rows = await _store(request).list(
        tenant.org_id, user_id=user_id, service_account_id=sa_id
    )
    return [BrowserProfileOut.of(r) for r in rows]


@router.post("/api/browser-profiles", status_code=201)
async def create_profile(
    body: CreateProfileRequest,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> BrowserProfileOut:
    user_id, sa_id = _principal(tenant)
    row = await _store(request).create(
        tenant.org_id,
        user_id=user_id,
        service_account_id=sa_id,
        name=(body.name or "Profile").strip() or "Profile",
    )
    return BrowserProfileOut.of(row)


@router.patch("/api/browser-profiles/{profile_id}")
async def rename_profile(
    profile_id: UUID,
    body: RenameProfileRequest,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> dict[str, bool]:
    user_id, sa_id = _principal(tenant)
    ok = await _store(request).rename(
        profile_id,
        tenant.org_id,
        user_id=user_id,
        service_account_id=sa_id,
        name=body.name.strip(),
    )
    if not ok:
        raise HTTPException(status_code=404, detail="Profile not found")
    return {"renamed": True}


@router.delete("/api/browser-profiles/{profile_id}", status_code=204)
async def delete_profile(
    profile_id: UUID,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> None:
    user_id, sa_id = _principal(tenant)
    await _store(request).delete(
        profile_id, tenant.org_id, user_id=user_id, service_account_id=sa_id
    )


class SetupSessionRequest(BaseModel):
    owner_user_id: str | None = None
    agent_id: str | None = None
    setup_spec: dict | None = None


@router.post("/api/browser-profiles/{profile_id}/setup-session")
async def create_setup_session(
    profile_id: UUID,
    body: SetupSessionRequest,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> dict[str, str]:
    user_id, sa_id = _principal(tenant)
    store = _store(request)

    # The profile must exist and belong to the caller before we burn a pod.
    rows = await store.list(tenant.org_id, user_id=user_id, service_account_id=sa_id)
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

    # Create a browser-only session: no harness wake is enqueued, so the agent
    # loop never runs — the human drives the browser through the live view.
    settings = request.app.state.settings
    session = await create_agent_session(
        store=request.app.state.session_store,
        storage=request.app.state.storage,
        settings=settings,
        org_id=tenant.org_id,
        user_id=user_id,
        agent_id=body.agent_id or "browser-setup",
        channel="browser_setup",
        model=settings.llm.model,
        config={
            "browser": {"profile_id": str(profile_id), "setup_spec": setup_spec}
        },
        service_account_id=sa_id,
    )
    sid = str(session.id)

    pool = request.app.state.browser_pool
    try:
        await pool.ensure(
            session_id=sid,
            org_id=str(tenant.org_id),
            user_id=owner,
            spec=BrowserSpec(active_deadline_seconds=_SETUP_TTL_SECONDS),
        )
    except BrowserCreditsExhaustedError as exc:
        # ``ensure`` runs the browser-minutes credit guard before provisioning,
        # so a setup browser meters minutes like any live browser. Surface a
        # top-up signal instead of a 500.
        raise HTTPException(status_code=402, detail=str(exc)) from exc

    await request.app.state.browser_control.acquire(sid, owner)

    expires_at = datetime.now(timezone.utc) + timedelta(seconds=_SETUP_TTL_SECONDS)
    return {"session_id": sid, "expires_at": expires_at.isoformat()}


class CaptureRequest(BaseModel):
    owner_user_id: str | None = None


@router.post("/api/browser-profiles/{profile_id}/capture")
async def capture_profile(
    profile_id: UUID,
    session_id: UUID,
    body: CaptureRequest,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> BrowserProfileOut:
    user_id, sa_id = _principal(tenant)
    store = _store(request)

    owner = body.owner_user_id or (
        str(tenant.user_id) if tenant.user_id else None
    )
    if not owner:
        raise HTTPException(
            status_code=403, detail="Capture requires an owner user id."
        )

    session = await request.app.state.session_store.get_session(session_id)
    if session is None:
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
    return BrowserProfileOut.of(row)
