"""Shared authorization for the admin MCP-server and credential routes."""

from __future__ import annotations

from fastapi import Depends, HTTPException, status

from surogates.tenant.auth.middleware import get_current_tenant
from surogates.tenant.context import PrincipalKind, TenantContext


async def require_admin_principal(
    tenant: TenantContext = Depends(get_current_tenant),
) -> TenantContext:
    """Router dependency: let a signed-in user holding ``admin`` through.

    Channel-session and service-account principals (bare or
    session-scoped) are refused outright, whatever permissions they
    carry: they never administer MCP servers or credentials.  Any other
    caller needs the ``admin`` permission.
    """
    kind, _ = tenant.principal()
    if kind is not PrincipalKind.USER:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "This endpoint is not available to channel-session or "
                "service-account principals."
            ),
        )
    if "admin" not in tenant.permissions:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This endpoint requires the 'admin' permission.",
        )
    return tenant
