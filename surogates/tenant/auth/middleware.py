"""FastAPI authentication middleware and dependency.

Provides two integration points:

1. ``get_current_tenant`` -- a FastAPI *Depends* callable that extracts the
   JWT from the ``Authorization`` header, validates it, loads the org and
   user from the database, and returns a fully-populated ``TenantContext``.
   Route handlers declare ``tenant: TenantContext = Depends(get_current_tenant)``
   to enforce authentication.

2. ``setup_auth_middleware`` -- installs an ASGI middleware on the FastAPI
   app that runs *before* routing.  It performs the same JWT validation on
   every request (except explicitly skipped paths) and sets the
   ``TenantContext`` context-var so that downstream code can call
   ``get_tenant()`` without an explicit parameter.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any
from uuid import UUID

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import JSONResponse, Response

from surogates.db.models import Org, User
from surogates.tenant.auth.jwt import InvalidTokenError, decode_token
from surogates.tenant.auth.service_account import (
    ResolvedServiceAccount,
    ServiceAccountStore,
    is_service_account_token,
)
from surogates.tenant.context import TenantContext, set_tenant

if TYPE_CHECKING:
    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from surogates.config import Settings

__all__ = [
    "LIVE_VIEW_TOKEN_COOKIE",
    "authenticate_websocket_tenant",
    "get_current_tenant",
    "setup_auth_middleware",
]

logger = logging.getLogger(__name__)

# Only API paths require authentication. Everything else (root, /assets/*,
# and all SPA fallback routes served by ``setup_frontend``) is public so the
# browser can load the app before the user signs in.
_PROTECTED_PATH_PREFIXES: tuple[str, ...] = ("/v1/",)

# API subpaths that are exempt from auth even though they live under /v1.
# The website channel routes authenticate anonymous visitors with a
# publishable key (bootstrap) or a signed session cookie (follow-up
# requests); neither is shaped as an ``Authorization: Bearer`` JWT, so
# they must bypass this middleware entirely.  The route handlers under
# ``/v1/website/*`` do their own origin + CSRF enforcement.
_PUBLIC_PATH_PREFIXES: tuple[str, ...] = (
    "/v1/auth/",
    "/v1/transparency",
    "/v1/website/",
)

# Path prefix for routes that service-account tokens are allowed to hit.
# Every other protected path requires a JWT carrying a real user identity.
_SERVICE_ACCOUNT_PATH_PREFIX: str = "/v1/api/"

_QUERY_TOKEN_PATH_RE = re.compile(
    r"^/v1/(api/)?sessions/[0-9a-fA-F-]{36}/"
    r"(?:events|browser/live(?:/.*)?|workspace/download)$",
)
_LIVE_VIEW_PATH_RE = re.compile(
    r"^/v1/(api/)?sessions/[0-9a-fA-F-]{36}/browser/live(?:/.*)?$",
)
LIVE_VIEW_TOKEN_COOKIE = "surogates_browser_live_token"


def _is_public(path: str) -> bool:
    """Return True when *path* should bypass the auth middleware."""
    if not any(path.startswith(p) for p in _PROTECTED_PATH_PREFIXES):
        return True
    return any(path.startswith(p) for p in _PUBLIC_PATH_PREFIXES)


def _is_service_account_path(path: str) -> bool:
    """Return True when the path is reachable with a service-account token."""
    return path.startswith(_SERVICE_ACCOUNT_PATH_PREFIX)


def _allows_query_token(path: str) -> bool:
    """Return True when a path may authenticate with ``?token=``.

    Query-token auth is limited to browser primitives that cannot attach
    custom headers: EventSource streams, live-view iframes/WebSockets, and
    workspace file downloads initiated by an ``<a download>`` link. Regular
    REST APIs must use the Authorization header.
    """
    return bool(_QUERY_TOKEN_PATH_RE.match(path))


def _allows_live_view_cookie(path: str) -> bool:
    return bool(_LIVE_VIEW_PATH_RE.match(path))


def _query_or_live_view_cookie_token(
    *,
    path: str,
    query_params: Mapping[str, str],
    cookies: Mapping[str, str],
) -> str | None:
    if not _allows_query_token(path):
        return None
    token = query_params.get("token")
    if token:
        return token
    if _allows_live_view_cookie(path):
        return cookies.get(LIVE_VIEW_TOKEN_COOKIE)
    return None


def _extract_bearer(auth_header: str) -> str:
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:]
    return ""


_bearer_scheme = HTTPBearer(auto_error=False)


# ------------------------------------------------------------------
# FastAPI dependency
# ------------------------------------------------------------------


async def get_current_tenant(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> TenantContext:
    """FastAPI dependency that enforces auth and returns ``TenantContext``.

    Accepts either a JWT access token or a service-account token.
    Service-account tokens (``surg_sk_…``) are only honoured on paths
    under :data:`_SERVICE_ACCOUNT_PATH_PREFIX`; presenting one anywhere
    else yields a 403.

    The token may be supplied in the ``Authorization: Bearer`` header or,
    for the small allow-list in :func:`_allows_query_token`, a ``?token=``
    query parameter.

    Attach to routes via ``Depends(get_current_tenant)``.
    """
    raw_token: str | None = None
    if credentials is not None:
        raw_token = credentials.credentials
    elif _allows_query_token(request.url.path):
        raw_token = _query_or_live_view_cookie_token(
            path=request.url.path,
            query_params=request.query_params,
            cookies=request.cookies,
        )

    if not raw_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authentication credentials.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    session_factory: async_sessionmaker = request.app.state.session_factory
    tenant_assets_root: str = request.app.state.settings.tenant_assets_root

    ctx = await _tenant_context_from_token(
        session_factory,
        raw_token,
        tenant_assets_root,
        path=request.url.path,
    )
    set_tenant(ctx)
    return ctx


async def authenticate_websocket_tenant(
    app: "FastAPI",
    *,
    path: str,
    token: str | None,
    cookies: Mapping[str, str] | None = None,
    authorization: str | None = None,
) -> TenantContext:
    """Authenticate a WebSocket and return its tenant context.

    Accepts (in priority order) an ``Authorization: Bearer`` header
    forwarded by an in-cluster proxy (e.g. the ops live-view forwarder
    which carries a service-account token), the explicit ``token``
    argument from a browser-supplied ``?token=`` query param, or — for
    paths in :func:`_allows_live_view_cookie` — the live-view cookie.
    """
    if not token and authorization:
        token = _extract_bearer(authorization) or None
    if not token:
        token = _query_or_live_view_cookie_token(
            path=path,
            query_params={},
            cookies=cookies or {},
        )
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authentication credentials.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    # Service-account tokens are gated by ``_tenant_context_from_token``
    # against ``_SERVICE_ACCOUNT_PATH_PREFIX``; for non-service-account
    # tokens, restrict WS auth to the same allow-list as HTTP query-token
    # auth so we don't widen the trust surface.
    if not is_service_account_token(token) and not _allows_query_token(path):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authentication credentials.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    session_factory: async_sessionmaker = app.state.session_factory
    tenant_assets_root: str = app.state.settings.tenant_assets_root
    ctx = await _tenant_context_from_token(
        session_factory,
        token,
        tenant_assets_root,
        path=path,
    )
    set_tenant(ctx)
    return ctx


async def _tenant_context_from_token(
    session_factory: async_sessionmaker,
    raw_token: str,
    tenant_assets_root: str,
    *,
    path: str,
) -> TenantContext:
    if is_service_account_token(raw_token):
        if not _is_service_account_path(path):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    "Service-account tokens may only be used on "
                    f"{_SERVICE_ACCOUNT_PATH_PREFIX}* routes."
                ),
            )
        ctx = await _build_service_account_context(
            session_factory, raw_token, tenant_assets_root
        )
        return ctx

    try:
        payload = decode_token(raw_token)
    except InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {exc}",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    token_type = payload.get("type")
    if token_type == "service_account_session":
        ctx = await _build_service_account_session_context(
            session_factory, payload, tenant_assets_root
        )
        return ctx

    if token_type == "channel_session":
        ctx = await _build_channel_session_context(
            session_factory, payload, tenant_assets_root,
        )
        return ctx

    if token_type != "access":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Only access tokens are accepted for API requests.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    org_id = UUID(payload["org_id"])
    user_id = UUID(payload["user_id"])

    async with session_factory() as session:
        org, user = await _load_org_and_user(session, org_id, user_id)

    if org is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Organisation not found.",
        )
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found.",
        )

    asset_root = f"{tenant_assets_root}/{org_id}"

    ctx = TenantContext(
        org_id=org_id,
        user_id=user_id,
        org_config=org.config or {},
        user_preferences=user.preferences or {},
        permissions=frozenset(payload.get("permissions", [])),
        asset_root=asset_root,
    )
    return ctx


async def _build_service_account_context(
    session_factory: async_sessionmaker,
    raw_token: str,
    tenant_assets_root: str,
) -> TenantContext:
    """Resolve a service-account token to a ``TenantContext``.

    Raises an HTTP 401 when the token is unknown, revoked, or its
    owning org has been deleted.  A cache hit skips the DB round-trip
    for the SA row — only the ``Org`` lookup remains.
    """
    store = ServiceAccountStore(session_factory)
    sa: ResolvedServiceAccount | None = await store.get_by_token(raw_token)
    if sa is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or revoked service-account token.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    org = await _load_org_or_401(session_factory, sa.org_id)
    return TenantContext(
        org_id=sa.org_id,
        user_id=None,
        org_config=org.config or {},
        user_preferences={},
        permissions=frozenset(),
        asset_root=f"{tenant_assets_root}/{sa.org_id}",
        service_account_id=sa.id,
    )


async def _build_channel_session_context(
    session_factory: async_sessionmaker,
    payload: dict[str, Any],
    tenant_assets_root: str,
) -> TenantContext:
    """Resolve a ``channel_session`` JWT to a :class:`TenantContext`.

    The session row is the authority — the JWT is a signed pointer
    into it.  Four independent invariants; any mismatch is 401:

    1. The ``sessions`` row referenced by ``session_id`` must exist.
    2. The row's ``org_id`` must match the JWT.
    3. The row's ``agent_id`` must match the JWT (defence in depth
       against an agent re-deployed under the same org with the same
       session id reused — practically impossible today, but the cost
       of checking is zero so we check).
    4. The row's ``channel`` must match the JWT.

    On success, returns a :class:`TenantContext` with neither
    ``user_id`` nor ``service_account_id`` set; only
    ``session_scope_id``.  Routes that require a user or
    service-account principal refuse this context via
    :func:`surogates.api.routes._shared.require_not_channel_principal`.
    """
    from surogates.db.models import Session as SessionModel

    org_id = UUID(payload["org_id"])
    agent_id = str(payload["agent_id"])
    session_id = UUID(payload["session_id"])
    channel = str(payload["channel"])

    async with session_factory() as db_session:
        session_row = await db_session.get(SessionModel, session_id)

    if session_row is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session not found.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if session_row.org_id != org_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Channel-session token org mismatch.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if session_row.agent_id != agent_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Channel-session token agent mismatch.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if session_row.channel != channel:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Channel-session token channel mismatch.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    org = await _load_org_or_401(session_factory, org_id)
    return TenantContext(
        org_id=org_id,
        user_id=None,
        org_config=org.config or {},
        user_preferences={},
        permissions=frozenset(),
        asset_root=f"{tenant_assets_root}/{org_id}",
        service_account_id=None,
        session_scope_id=session_id,
    )


async def _build_service_account_session_context(
    session_factory: async_sessionmaker,
    payload: dict[str, Any],
    tenant_assets_root: str,
) -> TenantContext:
    """Resolve a ``service_account_session`` JWT to a ``TenantContext``.

    Verifies the referenced service account still exists and is not
    revoked — a token issued to an SA that has since been revoked must
    stop working immediately, even within its JWT's lifetime.  The
    returned context carries ``session_scope_id`` so
    ``/v1/api/prompts`` can refuse it (session JWTs must not be usable
    to start new sessions).
    """
    org_id = UUID(payload["org_id"])
    service_account_id = UUID(payload["service_account_id"])
    session_id = UUID(payload["session_id"])

    store = ServiceAccountStore(session_factory)
    sa = await store.get_by_id(service_account_id, org_id)
    if sa is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Service account not found or revoked.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    org = await _load_org_or_401(session_factory, org_id)
    asset_root = f"{tenant_assets_root}/{org_id}"
    return TenantContext(
        org_id=org_id,
        user_id=None,
        org_config=org.config or {},
        user_preferences={},
        permissions=frozenset(),
        asset_root=asset_root,
        service_account_id=sa.id,
        session_scope_id=session_id,
    )


# ------------------------------------------------------------------
# ASGI middleware
# ------------------------------------------------------------------


def setup_auth_middleware(app: FastAPI, settings: Settings) -> None:
    """Attach JWT validation middleware to the FastAPI *app*.

    Must be called **after** ``app.state.session_factory`` has been set
    (typically in the application factory).  If the session factory is not
    yet available at import time, the middleware defers its lookup to the
    first request.
    """
    # We store a lazy reference so the middleware can be attached before
    # the DB engine is created (common in test fixtures).

    @app.middleware("http")
    async def _auth_middleware(
        request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        path = request.url.path

        if _is_public(path):
            return await call_next(request)

        auth_header = request.headers.get("authorization", "")
        token = _extract_bearer(auth_header)
        if not token:
            token = _query_or_live_view_cookie_token(
                path=path,
                query_params=request.query_params,
                cookies=request.cookies,
            ) or ""

        if not token:
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={"detail": "Missing authentication credentials."},
                headers={"WWW-Authenticate": "Bearer"},
            )

        session_factory: async_sessionmaker | None = getattr(
            request.app.state, "session_factory", None
        )
        if session_factory is None:
            logger.error("session_factory not set on app.state")
            return JSONResponse(
                status_code=500,
                content={"detail": "Server misconfiguration."},
            )

        try:
            ctx = await _tenant_context_from_token(
                session_factory,
                token,
                settings.tenant_assets_root,
                path=path,
            )
        except HTTPException as exc:
            return JSONResponse(
                status_code=exc.status_code,
                content={"detail": exc.detail},
                headers=exc.headers or {},
            )
        set_tenant(ctx)

        return await call_next(request)


# ------------------------------------------------------------------
# Shared helpers
# ------------------------------------------------------------------


async def _load_org(session: AsyncSession, org_id: UUID) -> Org | None:
    """Fetch the ``Org`` row for a resolved tenant."""
    result = await session.execute(select(Org).where(Org.id == org_id))
    return result.scalar_one_or_none()


async def _load_org_and_user(
    session: AsyncSession, org_id: UUID, user_id: UUID
) -> tuple[Org | None, User | None]:
    """Fetch the ``Org`` and ``User`` rows needed to build a context."""
    org = await _load_org(session, org_id)

    user_result = await session.execute(
        select(User).where(User.id == user_id, User.org_id == org_id)
    )
    user: User | None = user_result.scalar_one_or_none()

    return org, user


async def _load_org_or_401(
    session_factory: async_sessionmaker, org_id: UUID
) -> Org:
    """Return the ``Org`` for *org_id* or raise 401 when it's gone.

    Shared by the two SA-auth paths — both must 401 (not 404) when the
    referenced org has been deleted, since the caller authenticated
    against state that no longer exists.
    """
    async with session_factory() as session:
        org = await _load_org(session, org_id)
    if org is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Organisation not found.",
        )
    return org
