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
    "get_current_tenant",
    "setup_auth_middleware",
]

logger = logging.getLogger(__name__)

# Only API paths require authentication. Everything else (root, /assets/*,
# and all SPA fallback routes served by ``setup_frontend``) is public so the
# browser can load the app before the user signs in.
_PROTECTED_PATH_PREFIXES: tuple[str, ...] = ("/v1/",)

# API subpaths that are exempt from auth even though they live under /v1.
_PUBLIC_PATH_PREFIXES: tuple[str, ...] = (
    "/v1/auth/",
    "/v1/transparency",
)

# Path prefix for routes that service-account tokens are allowed to hit.
# Every other protected path requires a JWT carrying a real user identity.
_SERVICE_ACCOUNT_PATH_PREFIX: str = "/v1/api/"


def _is_public(path: str) -> bool:
    """Return True when *path* should bypass the auth middleware."""
    if not any(path.startswith(p) for p in _PROTECTED_PATH_PREFIXES):
        return True
    return any(path.startswith(p) for p in _PUBLIC_PATH_PREFIXES)


def _is_service_account_path(path: str) -> bool:
    """Return True when the path is reachable with a service-account token."""
    return path.startswith(_SERVICE_ACCOUNT_PATH_PREFIX)

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

    The token may be supplied in the ``Authorization: Bearer`` header
    or a ``?token=`` query parameter (SSE/WebSocket clients).

    Attach to routes via ``Depends(get_current_tenant)``.
    """
    raw_token: str | None = None
    if credentials is not None:
        raw_token = credentials.credentials
    else:
        raw_token = request.query_params.get("token")

    if not raw_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authentication credentials.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    session_factory: async_sessionmaker = request.app.state.session_factory
    tenant_assets_root: str = request.app.state.settings.tenant_assets_root

    if is_service_account_token(raw_token):
        if not _is_service_account_path(request.url.path):
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
        set_tenant(ctx)
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
        set_tenant(ctx)
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
    set_tenant(ctx)
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

        # Extract token from Authorization header or ?token= query param.
        # SSE (EventSource) and WebSocket clients cannot set headers, so
        # they pass the JWT as a query parameter instead.
        auth_header = request.headers.get("authorization", "")
        if auth_header.lower().startswith("bearer "):
            token = auth_header[7:]
        else:
            token = request.query_params.get("token", "")

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

        # Service-account token branch.
        if is_service_account_token(token):
            if not _is_service_account_path(path):
                return JSONResponse(
                    status_code=status.HTTP_403_FORBIDDEN,
                    content={
                        "detail": (
                            "Service-account tokens may only be used on "
                            f"{_SERVICE_ACCOUNT_PATH_PREFIX}* routes."
                        )
                    },
                )
            try:
                ctx = await _build_service_account_context(
                    session_factory, token, settings.tenant_assets_root
                )
            except HTTPException as exc:
                return JSONResponse(
                    status_code=exc.status_code,
                    content={"detail": exc.detail},
                    headers=exc.headers or {},
                )
            set_tenant(ctx)
            return await call_next(request)

        # JWT branch.
        try:
            payload = decode_token(token)
        except InvalidTokenError as exc:
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={"detail": f"Invalid token: {exc}"},
                headers={"WWW-Authenticate": "Bearer"},
            )

        token_type = payload.get("type")

        if token_type == "service_account_session":
            try:
                ctx = await _build_service_account_session_context(
                    session_factory, payload, settings.tenant_assets_root
                )
            except HTTPException as exc:
                return JSONResponse(
                    status_code=exc.status_code,
                    content={"detail": exc.detail},
                    headers=exc.headers or {},
                )
            set_tenant(ctx)
            return await call_next(request)

        if token_type != "access":
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={
                    "detail": "Only access tokens are accepted for API requests."
                },
                headers={"WWW-Authenticate": "Bearer"},
            )

        org_id = UUID(payload["org_id"])
        user_id = UUID(payload["user_id"])

        async with session_factory() as session:
            org, user = await _load_org_and_user(session, org_id, user_id)

        if org is None or user is None:
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={"detail": "Organisation or user not found."},
                headers={"WWW-Authenticate": "Bearer"},
            )

        asset_root = f"{settings.tenant_assets_root}/{org_id}"

        ctx = TenantContext(
            org_id=org_id,
            user_id=user_id,
            org_config=org.config or {},
            user_preferences=user.preferences or {},
            permissions=frozenset(payload.get("permissions", [])),
            asset_root=asset_root,
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
