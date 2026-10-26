"""Shared authorization for the ``/v1/admin`` routers.

:func:`require_admin_principal` is the router-level dependency of every
admin router: org and user management, channel identities, MCP servers,
credentials and service accounts.  A new admin router attaches it too.
:func:`is_admin_principal` is the same test as a plain answer, for a route
that lets an admin do one thing more than another caller.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, status

from surogates.tenant.auth.middleware import get_current_tenant
from surogates.tenant.context import PrincipalKind, TenantContext


def is_admin_principal(tenant: TenantContext) -> bool:
    """True for a signed-in user holding the ``admin`` permission.

    Channel-session and service-account principals are never admin
    principals, whatever permissions they carry.
    """
    kind, _ = tenant.principal()
    return kind is PrincipalKind.USER and "admin" in tenant.permissions


async def require_admin_principal(
    tenant: TenantContext = Depends(get_current_tenant),
) -> TenantContext:
    """Router dependency: let a signed-in user holding ``admin`` through.

    Channel-session and service-account principals (bare or
    session-scoped) are refused outright, whatever permissions they
    carry: they never use the admin routes.  Any other caller needs the
    ``admin`` permission.
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
    if not is_admin_principal(tenant):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This endpoint requires the 'admin' permission.",
        )
    return tenant
