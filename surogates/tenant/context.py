"""Tenant context propagation via contextvars.

Every authenticated request establishes a ``TenantContext`` that travels
implicitly through the async call-chain so that business logic never
needs an explicit *org_id* parameter.
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass
from uuid import UUID

__all__ = [
    "TenantContext",
    "get_tenant",
    "set_tenant",
]


@dataclass(frozen=True)
class TenantContext:
    """Immutable snapshot of the current tenant + user identity."""

    org_id: UUID
    user_id: UUID
    org_config: dict
    user_preferences: dict
    permissions: frozenset[str]
    asset_root: str  # path to tenant's asset directory


_tenant_ctx: contextvars.ContextVar[TenantContext] = contextvars.ContextVar(
    "tenant_ctx"
)


def get_tenant() -> TenantContext:
    """Return the active ``TenantContext``.

    Raises ``LookupError`` when called outside an authenticated request
    (i.e. the context variable has never been set in this async task).
    """
    return _tenant_ctx.get()


def set_tenant(ctx: TenantContext) -> contextvars.Token[TenantContext]:
    """Bind *ctx* as the current tenant context.

    Returns a reset token that callers can pass to
    ``_tenant_ctx.reset()`` to restore the previous value.
    """
    return _tenant_ctx.set(ctx)
