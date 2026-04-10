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
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse, Response

from surogates.db.models import Org, User
from surogates.tenant.auth.jwt import InvalidTokenError, decode_token
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

# Paths that never require authentication.
_PUBLIC_PATH_PREFIXES: tuple[str, ...] = (
    "/health",
    "/v1/auth/",
    "/v1/transparency",
    "/docs",
    "/redoc",
    "/openapi.json",
)

_bearer_scheme = HTTPBearer(auto_error=False)


# ------------------------------------------------------------------
# FastAPI dependency
# ------------------------------------------------------------------


async def get_current_tenant(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> TenantContext:
    """FastAPI dependency that enforces JWT auth and returns ``TenantContext``.

    Accepts JWT from either the ``Authorization: Bearer`` header or a
    ``?token=`` query parameter (for SSE/WebSocket clients that cannot
    set headers).

    Attach to routes via ``Depends(get_current_tenant)``.
    """
    # Try header first, fall back to query param.
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

    try:
        payload = decode_token(raw_token)
    except InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {exc}",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    if payload.get("type") != "access":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Only access tokens are accepted for API requests.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    session_factory: async_sessionmaker = request.app.state.session_factory
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

    tenant_assets_root: str = request.app.state.settings.tenant_assets_root
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


# ------------------------------------------------------------------
# ASGI middleware
# ------------------------------------------------------------------


class _AuthMiddleware(BaseHTTPMiddleware):
    """Starlette middleware that validates JWT and sets TenantContext."""

    def __init__(self, app, session_factory: async_sessionmaker, settings: Settings):  # type: ignore[override]
        super().__init__(app)
        self._session_factory = session_factory
        self._settings = settings

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        path = request.url.path

        # Skip authentication for public paths.
        if any(path.startswith(prefix) for prefix in _PUBLIC_PATH_PREFIXES):
            return await call_next(request)

        # Extract token from Authorization header or ?token= query param.
        # SSE (EventSource) and WebSocket clients cannot set headers, so they
        # pass the JWT as a query parameter instead.
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
        try:
            payload = decode_token(token)
        except InvalidTokenError as exc:
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={"detail": f"Invalid token: {exc}"},
                headers={"WWW-Authenticate": "Bearer"},
            )

        if payload.get("type") != "access":
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={
                    "detail": "Only access tokens are accepted for API requests."
                },
                headers={"WWW-Authenticate": "Bearer"},
            )

        org_id = UUID(payload["org_id"])
        user_id = UUID(payload["user_id"])

        async with self._session_factory() as session:
            org, user = await _load_org_and_user(session, org_id, user_id)

        if org is None or user is None:
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={"detail": "Organisation or user not found."},
                headers={"WWW-Authenticate": "Bearer"},
            )

        asset_root = f"{self._settings.tenant_assets_root}/{org_id}"

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

        if any(path.startswith(prefix) for prefix in _PUBLIC_PATH_PREFIXES):
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

        try:
            payload = decode_token(token)
        except InvalidTokenError as exc:
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={"detail": f"Invalid token: {exc}"},
                headers={"WWW-Authenticate": "Bearer"},
            )

        if payload.get("type") != "access":
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={
                    "detail": "Only access tokens are accepted for API requests."
                },
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


async def _load_org_and_user(
    session: AsyncSession, org_id: UUID, user_id: UUID
) -> tuple[Org | None, User | None]:
    """Fetch the ``Org`` and ``User`` rows needed to build a context."""
    org_result = await session.execute(select(Org).where(Org.id == org_id))
    org: Org | None = org_result.scalar_one_or_none()

    user_result = await session.execute(
        select(User).where(User.id == user_id, User.org_id == org_id)
    )
    user: User | None = user_result.scalar_one_or_none()

    return org, user
