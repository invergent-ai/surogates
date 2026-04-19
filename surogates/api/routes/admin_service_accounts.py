"""Admin CRUD for service-account API keys.

Routes mount under ``/v1/admin/service-accounts``.  ``admin``
permission is required for cross-org operations; regular users may
only manage service accounts scoped to their own org.

The raw token is returned **exactly once** on creation and cannot be
recovered later.  List and delete operations never expose the secret —
only a short display prefix (``surg_sk_abcd…``).
"""

from __future__ import annotations

import logging
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from surogates.db.models import Org, ServiceAccount
from surogates.tenant.auth.middleware import get_current_tenant
from surogates.tenant.auth.service_account import ServiceAccountStore
from surogates.tenant.context import TenantContext

logger = logging.getLogger(__name__)

router = APIRouter()


def _require_admin(tenant: TenantContext) -> None:
    """Enforce platform-admin access to service-account management.

    Issuance must be admin-only: service-account tokens are long-lived
    credentials that can submit prompts until revoked, so a compromised
    non-admin user must not be able to bootstrap persistent programmatic
    access by minting their own.
    """
    if "admin" not in tenant.permissions:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Managing service accounts requires the 'admin' permission.",
        )


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class ServiceAccountCreate(BaseModel):
    org_id: UUID
    name: str = Field(..., min_length=1, max_length=128)


class ServiceAccountInfo(BaseModel):
    """Metadata returned to clients — never includes the raw token."""

    id: UUID
    org_id: UUID
    name: str
    token_prefix: str
    created_at: datetime
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None


class ServiceAccountCreated(ServiceAccountInfo):
    """The one-time response that exposes the raw token.

    Surface this to the caller immediately — the plaintext cannot be
    recovered from the database afterwards.
    """

    token: str


class ServiceAccountListResponse(BaseModel):
    service_accounts: list[ServiceAccountInfo]
    total: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _store(request: Request) -> ServiceAccountStore:
    return ServiceAccountStore(request.app.state.session_factory)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post(
    "/service-accounts",
    response_model=ServiceAccountCreated,
    status_code=status.HTTP_201_CREATED,
)
async def create_service_account(
    body: ServiceAccountCreate,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> ServiceAccountCreated:
    """Issue a new service-account API key.

    Requires the ``admin`` permission.  The response includes the raw
    ``token`` exactly once — callers must store it, it is not
    recoverable.
    """
    _require_admin(tenant)

    session_factory = request.app.state.session_factory
    async with session_factory() as session:
        org = await session.get(Org, body.org_id)
        if org is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Organisation {body.org_id} not found.",
            )

    issued = await _store(request).create(org_id=body.org_id, name=body.name)

    return ServiceAccountCreated(
        id=issued.id,
        org_id=issued.org_id,
        name=issued.name,
        token_prefix=issued.token_prefix,
        created_at=issued.created_at,
        token=issued.token,
    )


@router.get(
    "/service-accounts",
    response_model=ServiceAccountListResponse,
)
async def list_service_accounts(
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
    org_id: UUID | None = None,
) -> ServiceAccountListResponse:
    """List service accounts for an org.

    Requires the ``admin`` permission.  Omitting ``org_id`` defaults to
    the admin's own org.  Tokens are never returned.
    """
    _require_admin(tenant)
    target_org = org_id if org_id is not None else tenant.org_id

    rows = await _store(request).list_for_org(target_org)
    infos = [
        ServiceAccountInfo(
            id=r.id,
            org_id=r.org_id,
            name=r.name,
            token_prefix=r.token_prefix,
            created_at=r.created_at,
            last_used_at=r.last_used_at,
            revoked_at=r.revoked_at,
        )
        for r in rows
    ]
    return ServiceAccountListResponse(service_accounts=infos, total=len(infos))


@router.delete(
    "/service-accounts/{service_account_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def revoke_service_account(
    service_account_id: UUID,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> None:
    """Revoke a service-account token.

    Requires the ``admin`` permission.  Revocation is immediate —
    subsequent requests carrying the token return 401.  The row is
    loaded first so the org scope comes from the database, not the
    client; already-revoked or unknown ids return 404.
    """
    _require_admin(tenant)

    session_factory = request.app.state.session_factory
    async with session_factory() as session:
        sa = await session.get(ServiceAccount, service_account_id)
    if sa is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Service account {service_account_id} not found.",
        )

    revoked = await _store(request).revoke(
        service_account_id=service_account_id, org_id=sa.org_id
    )
    if not revoked:
        # Row existed but was already revoked — 404 so repeated delete
        # calls don't silently succeed after the first one.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Service account {service_account_id} not found.",
        )
