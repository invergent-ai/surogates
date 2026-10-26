"""Helpers shared across resource-CRUD route modules (skills, agents, ...).

Keeps the routes thin: raising a 422 from a validator's return string and
collapsing the loader's DB-vs-FS source taxonomy down to the tenancy
layer the frontend cares about are both one-liners needed by every
resource module, so they live here instead of being copied verbatim.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import HTTPException, Request, status

from surogates.api.routes._admin_auth import is_admin_principal
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


def _refuse_unless_shared_layer_standing(tenant: TenantContext, refusal: str) -> None:
    """Let an org-wide service account or an admin principal through; refuse the rest.

    An org-wide service account is one bound to no agent
    (``tenant.service_account_agent_id is None``), like the control plane's
    own machine identities.  A service account bound to one agent is a
    customer's API key, handed to a third party: it never changes what the
    whole organization shares.  The binding is carried by the bare key and by
    the session token of the same account alike.  *refusal* is the 403's
    detail.
    """
    kind, _ = tenant.principal()
    org_wide_service_account = (
        kind is PrincipalKind.JUDGE and tenant.service_account_agent_id is None
    )
    if org_wide_service_account or is_admin_principal(tenant):
        return
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=refusal)


def require_shared_layer_writer(
    tenant: TenantContext, found: dict[str, Any], label: str,
) -> None:
    """Refuse a change to an item that is not in the caller's own layer.

    *found* is what ``TenantStorage.skill_exists`` or ``agent_exists``
    returned: ``{"key_prefix": ..., "layer": "user" | "org"}``.  An item in
    the ``"user"`` layer belongs to the caller, so anyone may change it.  An
    item in any other layer, or with none, is shared by the whole org, and
    only an org-wide service account (one bound to no agent) or an admin
    principal (:func:`~surogates.api.routes._admin_auth.is_admin_principal`)
    may change it.  *label* names the item in the refusal, e.g.
    ``"Skill 'notes'"``.
    """
    if found.get("layer") == "user":
        return
    _refuse_unless_shared_layer_standing(
        tenant,
        f"{label} is shared by the whole organization: only an admin "
        "or an org-wide service account can change it.",
    )


def require_shared_layer_creator(tenant: TenantContext, label: str) -> None:
    """Refuse a create whose target is the org-shared layer to a caller without standing.

    :class:`~surogates.storage.tenant.TenantStorage` writes a new skill or
    sub-agent to the caller's own layer when the caller has a user, and to
    the org-shared layer when it has none.  A create with a user is the
    caller's own and passes.  A create with no user lands in the layer the
    whole org shares, so it needs the standing
    :func:`require_shared_layer_writer` asks for.  *label* names what is
    created, e.g. ``"A skill"``.
    """
    if tenant.user_id is not None:
        return
    _refuse_unless_shared_layer_standing(
        tenant,
        f"{label} created here would be shared by the whole organization: "
        "only an admin or an org-wide service account can create it.",
    )


async def resolve_agent_id(request: Request) -> str | None:
    """The request's active agent id, or ``None``.

    ``?agent_id=`` query parameter first (ops proxy and Studio), then
    the ``Host`` subdomain slug (per-tenant chat web apps). Best-effort:
    never raises.
    """
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
    return agent_id or None


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
    agent_id = await resolve_agent_id(request)
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


async def resolve_system_bundle(request):
    """Return the shared system-skills bundle or ``None``.

    Mirror of :func:`resolve_agent_bundle` for the cluster-wide
    ``platform/system-skills`` bundle.  Catalogue routes
    (``/v1/skills``) call both so admin / Studio see the same
    catalog the runtime will assemble at session-start.

    Never raises; degrades to ``None`` when:

    * ``system_bundle_cache`` is not wired on ``app.state`` (older
      deploys that haven't picked up the wiring yet),
    * ``platform/system-skills`` has no published ``v*`` tag (boot
      before the operator has run ``seed-builtin-skills``),
    * the Hub network round-trip fails — the route still renders
      whatever the per-agent bundle and DB layers can produce.
    """
    cache = getattr(request.app.state, "system_bundle_cache", None)
    if cache is None:
        return None
    try:
        return await cache.get()
    except LookupError:
        return None
    except Exception:  # noqa: BLE001 — Hub network failure
        logger.warning(
            "system bundle resolver: Hub round-trip failed; "
            "catalog route will render without Layer 1a",
            exc_info=True,
        )
        return None


#: How recent a sign-in must be to add or restore a computer: a stolen
#: session that only refreshes cannot give itself a device.
RECENT_SIGN_IN_S = 10 * 60
#: How far ahead of this server's clock a sign-in's time may be, for the clocks: no further.
SIGN_IN_CLOCK_SKEW_S = 60


def require_recent_sign_in(tenant: TenantContext) -> None:
    """Raise 403 ``recent_sign_in_required`` unless the user signed in within RECENT_SIGN_IN_S."""
    age = None if tenant.auth_time is None else time.time() - tenant.auth_time
    if age is None or age > RECENT_SIGN_IN_S or age < -SIGN_IN_CLOCK_SKEW_S:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "recent_sign_in_required",
                "message": "Sign in again: adding or restoring a computer needs a sign-in from the last 10 minutes.",
            },
        )
