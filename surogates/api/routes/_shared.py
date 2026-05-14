"""Helpers shared across resource-CRUD route modules (skills, agents, ...).

Keeps the routes thin: raising a 422 from a validator's return string and
collapsing the loader's DB-vs-FS source taxonomy down to the tenancy
layer the frontend cares about are both one-liners needed by every
resource module, so they live here instead of being copied verbatim.
"""

from __future__ import annotations

from fastapi import HTTPException, status

from surogates.tenant.context import PrincipalKind, TenantContext


def raise_validation(err: str | None) -> None:
    """Raise HTTP 422 when *err* is truthy; no-op otherwise."""
    if err:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=err,
        )


def normalize_source(source: str) -> str:
    """Collapse DB-backed source variants to the tenancy-layer name.

    The loader distinguishes ``org``/``org_db`` and ``user``/``user_db``
    so merge precedence is deterministic, but API consumers only care
    about the tenancy layer, not the storage backend.
    """
    if source == "org_db":
        return "org"
    if source == "user_db":
        return "user"
    return source


def require_not_channel_principal(tenant: TenantContext) -> None:
    """Refuse anonymous-channel sessions on shared-storage routes.

    :class:`~surogates.storage.tenant.TenantStorage` maps
    ``user_id=None`` to ``shared/*`` paths for skills, agents, and
    memory.  Service-account contexts use that semantics intentionally
    (org-wide assets owned by the SA principal), but anonymous-channel
    sessions must not — a leaked website JWT would otherwise be a
    write-anywhere credential against org-shared storage.  Routes that
    read or mutate that storage call this helper to refuse
    :class:`PrincipalKind.CHANNEL` *before* doing any work, and *only*
    that kind: user and service-account principals pass through
    unchanged.
    """
    kind, _ = tenant.principal()
    if kind is PrincipalKind.CHANNEL:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "This endpoint is not available to anonymous-channel "
                "sessions."
            ),
        )
