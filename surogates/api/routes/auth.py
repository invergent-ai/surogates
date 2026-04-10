"""Authentication routes -- login, token refresh, and user info."""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from surogates.tenant.auth.database import DatabaseAuthProvider
from surogates.tenant.auth.jwt import (
    InvalidTokenError,
    create_access_token,
    create_refresh_token,
    decode_token,
)
from surogates.tenant.auth.middleware import get_current_tenant
from surogates.tenant.context import TenantContext
from surogates.tenant.models import UserResponse

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class LoginRequest(BaseModel):
    email: str
    password: str
    org_id: UUID | None = None  # optional — defaults to the server's configured org


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class RefreshRequest(BaseModel):
    refresh_token: str


class AccessTokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/auth/login", response_model=TokenResponse)
async def login(body: LoginRequest, request: Request) -> TokenResponse:
    """Authenticate a user and issue access + refresh tokens."""
    session_factory = request.app.state.session_factory

    # Use the request's org_id if provided, otherwise the server's configured org.
    org_id = body.org_id
    if org_id is None:
        settings = request.app.state.settings
        if not settings.org_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="org_id is required (server has no default org configured).",
            )
        org_id = UUID(settings.org_id)

    provider = DatabaseAuthProvider(session_factory, org_id)
    result = await provider.authenticate(
        {"email": body.email, "password": body.password}
    )

    if not result.authenticated or result.user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=result.error or "Invalid credentials.",
        )

    user_id = UUID(result.user_id)
    # Default permissions for authenticated users.
    permissions: set[str] = {"sessions:read", "sessions:write", "tools:read"}

    access_token = create_access_token(
        org_id=org_id,
        user_id=user_id,
        permissions=permissions,
    )
    refresh_token = create_refresh_token(
        org_id=org_id,
        user_id=user_id,
    )

    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
    )


@router.post("/auth/refresh", response_model=AccessTokenResponse)
async def refresh(body: RefreshRequest, request: Request) -> AccessTokenResponse:
    """Exchange a valid refresh token for a new access token."""
    try:
        payload = decode_token(body.refresh_token)
    except InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid refresh token: {exc}",
        ) from exc

    if payload.get("type") != "refresh":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Expected a refresh token.",
        )

    org_id = UUID(payload["org_id"])
    user_id = UUID(payload["user_id"])

    # Re-issue an access token with default permissions.
    permissions: set[str] = {"sessions:read", "sessions:write", "tools:read"}

    access_token = create_access_token(
        org_id=org_id,
        user_id=user_id,
        permissions=permissions,
    )

    return AccessTokenResponse(access_token=access_token)


@router.get("/auth/me", response_model=UserResponse)
async def me(
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> UserResponse:
    """Return profile information for the currently authenticated user."""
    from sqlalchemy import select

    from surogates.db.models import User

    session_factory = request.app.state.session_factory
    async with session_factory() as session:
        result = await session.execute(
            select(User).where(
                User.id == tenant.user_id,
                User.org_id == tenant.org_id,
            )
        )
        user = result.scalar_one_or_none()

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found.",
        )

    return UserResponse(
        id=user.id,
        org_id=user.org_id,
        email=user.email,
        display_name=user.display_name,
        auth_provider=user.auth_provider,
        created_at=user.created_at,
    )
