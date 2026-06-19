"""Browser-profile CRUD + setup/capture routes (mounted under ``/v1``)."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from surogates.browser.profiles import BrowserProfileRow
from surogates.tenant.auth.middleware import get_current_tenant
from surogates.tenant.context import TenantContext

router = APIRouter()


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
