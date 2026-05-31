"""Helpers shared across resource-CRUD route modules (skills, agents, ...).

Keeps the routes thin: raising a 422 from a validator's return string and
collapsing the loader's DB-vs-FS source taxonomy down to the tenancy
layer the frontend cares about are both one-liners needed by every
resource module, so they live here instead of being copied verbatim.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException, Request, status

from surogates.tenant.context import PrincipalKind, TenantContext

logger = logging.getLogger(__name__)


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


async def resolve_agent_bundle(request: Request):
    """Return the active agent's file bundle, or ``None``.

    Routes that surface ``/v1/skills``, ``/v1/agents``, and similar
    catalogue endpoints need to include the per-tenant Hub bundle's
    ``skills/`` and ``agents/`` subtrees in the catalogue so
    bundle-attached entries reach Studio and the chat slash menu.

    Resolution order mirrors ``agent_runtime_context_dep``:

    1. ``?agent_id=<id>`` query parameter — the path the ops proxy
       and Studio both take.
    2. ``Host`` header subdomain slug — per-tenant chat web apps
       at ``slug.example.com``.

    Returns ``None`` (never raises) when:

    * ``file_bundle_cache`` is not wired.
    * No ``agent_id`` is resolvable.
    * The agent has no published bundle yet
      (``bundle_hub_ref`` empty in the runtime config).
    * The Hub network round-trip fails — degrade gracefully rather
      than 500 the catalogue route.
    """
    cache = getattr(request.app.state, "file_bundle_cache", None)
    if cache is None:
        return None
    agent_id = request.query_params.get("agent_id")
    if not agent_id:
        host = request.headers.get("host", "")
        slug = host.split(".", 1)[0] if "." in host else None
        if slug and slug.lower() not in {"www", "api", "localhost"}:
            from surogates.runtime.resolver import _resolve_slug_to_agent_id
            try:
                agent_id = await _resolve_slug_to_agent_id(request, slug)
            except Exception:  # noqa: BLE001 — slug lookup is best-effort
                agent_id = None
    if not agent_id:
        return None
    try:
        return await cache.get(agent_id)
    except LookupError:
        return None
    except Exception:  # noqa: BLE001 — Hub network failure
        logger.warning(
            "bundle resolver: failed to load bundle for agent %s; "
            "falling back to disk layer 1",
            agent_id, exc_info=True,
        )
        return None
