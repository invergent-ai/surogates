"""Admin CRUD for the encrypted credential vault.

Credentials live in the ``credentials`` table, encrypted at rest with
Fernet (AES-128-CBC + HMAC-SHA256).  MCP server registrations
reference them via ``credential_refs`` — the proxy resolves and
injects them at connection time so secrets never enter the sandbox.

Routes mount under ``/v1/admin/credentials``.  Every route requires the
``admin`` permission and a signed-in user; channel-session and
service-account principals are refused (see
:func:`._admin_scope.require_admin_principal`).

The plaintext value is **never** returned by any endpoint.
"""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from surogates.db.models import Org, ServiceAccount, User
from surogates.tenant.credentials import CredentialVault

from ._admin_scope import require_admin_principal

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(require_admin_principal)])


class CredentialCreate(BaseModel):
    org_id: UUID
    user_id: UUID | None = None
    service_account_id: UUID | None = None
    name: str = Field(..., min_length=1, max_length=128)
    value: str = Field(..., min_length=1, repr=False)


class CredentialInfo(BaseModel):
    """Metadata returned to clients — never includes the plaintext."""

    org_id: UUID
    user_id: UUID | None
    service_account_id: UUID | None = None
    name: str


def _require_credential_scope(
    user_id: UUID | None,
    service_account_id: UUID | None,
) -> None:
    if user_id is not None and service_account_id is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Credential scope accepts user_id or service_account_id, not both.",
        )


class CredentialListResponse(BaseModel):
    credentials: list[CredentialInfo]
    total: int


def _get_vault(request: Request) -> CredentialVault:
    vault = getattr(request.app.state, "credential_vault", None)
    if vault is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Credential vault is not configured.  Set "
                "SUROGATES_ENCRYPTION_KEY to enable."
            ),
        )
    return vault


@router.post(
    "/credentials",
    response_model=CredentialInfo,
)
async def create_credential(
    body: CredentialCreate,
    request: Request,
    response: Response,
) -> CredentialInfo:
    """Store or update a credential (upsert).

    Returns ``201 Created`` on insert, ``200 OK`` on update.  The
    plaintext is never echoed back.
    """
    _require_credential_scope(body.user_id, body.service_account_id)
    vault = _get_vault(request)

    session_factory = request.app.state.session_factory
    async with session_factory() as session:
        org = await session.get(Org, body.org_id)
        if org is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Organisation {body.org_id} not found.",
            )
        if body.user_id is not None:
            user = await session.get(User, body.user_id)
            if user is None or user.org_id != body.org_id:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"User {body.user_id} not found in org {body.org_id}.",
                )
        if body.service_account_id is not None:
            sa = await session.get(ServiceAccount, body.service_account_id)
            if sa is None or sa.org_id != body.org_id:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=(
                        f"Service account {body.service_account_id} not found "
                        f"in org {body.org_id}."
                    ),
                )

    _id, created = await vault.store(
        body.org_id, body.name, body.value,
        user_id=body.user_id, service_account_id=body.service_account_id,
    )

    response.status_code = (
        status.HTTP_201_CREATED if created else status.HTTP_200_OK
    )
    return CredentialInfo(
        org_id=body.org_id, user_id=body.user_id,
        service_account_id=body.service_account_id, name=body.name,
    )


@router.get("/credentials", response_model=CredentialListResponse)
async def list_credentials(
    request: Request,
    org_id: UUID | None = None,
    user_id: UUID | None = None,
    service_account_id: UUID | None = None,
    limit: int = 50,
    offset: int = 0,
) -> CredentialListResponse:
    """List credential names, optionally filtered.

    ``org_id``, ``user_id`` and ``service_account_id`` narrow the list;
    without them it spans every credential in the database.  ``total`` is
    the true unpaginated count.
    """
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    _require_credential_scope(user_id, service_account_id)

    vault = _get_vault(request)

    if org_id is None:
        rows, total = await vault.list_all(
            user_id=user_id, service_account_id=service_account_id,
            limit=limit, offset=offset,
        )
        credentials = [
            CredentialInfo(org_id=oid, user_id=uid, service_account_id=sid, name=name)
            for (oid, uid, sid, name) in rows
        ]
        return CredentialListResponse(credentials=credentials, total=total)

    names = await vault.list_names(
        org_id, user_id=user_id, service_account_id=service_account_id,
    )
    names_sorted = sorted(names)
    page = names_sorted[offset : offset + limit]
    credentials = [
        CredentialInfo(
            org_id=org_id, user_id=user_id,
            service_account_id=service_account_id, name=n,
        )
        for n in page
    ]
    return CredentialListResponse(credentials=credentials, total=len(names_sorted))


@router.delete(
    "/credentials",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_credential(
    request: Request,
    org_id: UUID,
    name: str,
    user_id: UUID | None = None,
    service_account_id: UUID | None = None,
) -> None:
    """Delete a credential by (org, user/service-account, name).

    ``user_id`` and ``service_account_id`` are optional and mutually
    exclusive.  Omitting both deletes the org-wide credential.  Returns 204 on
    success, 404 otherwise.
    """
    _require_credential_scope(user_id, service_account_id)
    vault = _get_vault(request)

    deleted = await vault.delete(
        org_id, name, user_id=user_id, service_account_id=service_account_id,
    )
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Credential {name!r} not found.",
        )
