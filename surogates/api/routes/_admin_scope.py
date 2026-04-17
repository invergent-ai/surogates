"""Shared tenant-scope authorization helper for admin routes."""

from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException, status

from surogates.tenant.context import TenantContext


def require_tenant_scope(
    tenant: TenantContext,
    org_id: UUID,
    user_id: UUID | None,
    *,
    resource: str,
) -> None:
    """Raise 403 unless the tenant may manage the given (org, user) scope.

    Platform admins bypass all checks.  Regular users are pinned to
    their own org, and within that org they may only manage org-wide
    rows (``user_id`` is ``None``) or their own user-scoped rows.
    ``resource`` is interpolated into the 403 detail for a helpful
    message.
    """
    if "admin" in tenant.permissions:
        return

    if tenant.org_id != org_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Cannot manage {resource} outside your organisation.",
        )

    if user_id is not None and user_id != tenant.user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Cannot manage another user's {resource}.",
        )
