"""Authentication routes -- login, token refresh, and user info."""

from __future__ import annotations

import logging
import uuid
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import select

from surogates.audit import (
    AuditStore,
    AuditType,
    auth_failed_event,
    auth_login_event,
    client_ip,
)
from surogates.db.models import ChannelIdentity, User
from surogates.runtime import AgentRuntimeContext, agent_runtime_context_dep
from surogates.tenant.auth.database import DatabaseAuthProvider
from surogates.tenant.auth.firebase import (
    FirebaseTokenError,
    firebase_auth_provider_name,
    verify_firebase_id_token,
)
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


class FirebaseWebConfig(BaseModel):
    """Public Firebase web config returned to the agent web app."""

    api_key: str
    auth_domain: str
    project_id: str
    app_id: str | None = None
    messaging_sender_id: str | None = None
    measurement_id: str | None = None
    enabled_providers: list[str]


class AuthConfigResponse(BaseModel):
    """Runtime auth shape exposed by ``GET /v1/auth/config``."""

    self_registration_enabled: bool
    firebase: FirebaseWebConfig | None = None


class FirebaseExchangeRequest(BaseModel):
    id_token: str


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/auth/config", response_model=AuthConfigResponse)
async def auth_config(request: Request) -> AuthConfigResponse:
    """Public runtime auth config consumed by the agent web app.

    Returns the Firebase web config only when (a) self-registration is
    enabled for this agent AND (b) the project shipped a usable Firebase
    config. Otherwise advertises self-registration as disabled and omits
    Firebase entirely so the web app falls back to local login.
    """
    auth = request.app.state.settings.auth
    if not (auth.self_registration_enabled and auth.firebase_configured):
        return AuthConfigResponse(self_registration_enabled=False, firebase=None)
    return AuthConfigResponse(
        self_registration_enabled=True,
        firebase=FirebaseWebConfig(
            api_key=auth.firebase_api_key,
            auth_domain=auth.firebase_auth_domain,
            project_id=auth.firebase_project_id,
            app_id=auth.firebase_app_id or None,
            messaging_sender_id=auth.firebase_messaging_sender_id or None,
            measurement_id=auth.firebase_measurement_id or None,
            enabled_providers=list(auth.providers),
        ),
    )


def _email_from_firebase_claims(claims: dict) -> tuple[str | None, bool]:
    """Return ``(email, verified)`` extracted from Firebase token claims."""
    email = claims.get("email")
    if not isinstance(email, str) or not email.strip():
        return None, False
    return email.strip().lower(), claims.get("email_verified") is True


def _display_name_from_firebase_claims(
    claims: dict, email: str | None,
) -> str:
    name = claims.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()[:256]
    if email:
        return email.split("@", 1)[0]
    return "Agent User"


@router.post("/auth/firebase/exchange", response_model=TokenResponse)
async def firebase_exchange(
    body: FirebaseExchangeRequest,
    request: Request,
    agent_runtime: AgentRuntimeContext = Depends(agent_runtime_context_dep),
) -> TokenResponse:
    """Exchange a verified Firebase ID token for Surogates tokens.

    Behaviour depends on the runtime auth settings:

    * If Firebase is not configured at all, returns 404 — the endpoint
      simply isn't usable.
    * If self-registration is enabled, an unknown ``(auth_provider,
      external_id)`` is auto-provisioned as a new user. A matching email
      on an existing row is upgraded to the namespaced Firebase
      ``auth_provider`` only when the Firebase email is verified.
    * If self-registration is disabled, the request succeeds only when a
      pre-linked Firebase user already exists. No new rows are created;
      no manual ``database`` user is silently linked.
    """
    settings = request.app.state.settings
    auth = settings.auth
    audit_store: AuditStore | None = getattr(
        request.app.state, "audit_store", None,
    )
    source_ip = client_ip(request)

    if not auth.firebase_configured:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Firebase auth is not configured.",
        )
    if not agent_runtime.org_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="org_id is required (tenant has no org configured).",
        )
    org_id = UUID(agent_runtime.org_id)

    try:
        claims = await verify_firebase_id_token(
            body.id_token, auth.firebase_project_id,
        )
    except FirebaseTokenError as exc:
        if audit_store is not None:
            await audit_store.emit(
                org_id=org_id,
                type=AuditType.AUTH_FAILED,
                data=auth_failed_event(
                    "firebase", str(exc), source_ip=source_ip,
                ),
            )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc),
        ) from exc

    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Firebase token subject is missing.",
        )
    email, email_verified = _email_from_firebase_claims(claims)
    provider = firebase_auth_provider_name(auth.firebase_project_id)

    session_factory = request.app.state.session_factory
    async with session_factory() as session:
        user = await session.scalar(
            select(User).where(
                User.org_id == org_id,
                User.auth_provider == provider,
                User.external_id == subject,
            )
        )
        if (
            user is None
            and auth.self_registration_enabled
            and email
            and email_verified
        ):
            # Email-based link upgrade: only happens during self-
            # registration, never as a silent backdoor when registration
            # is disabled. We only upgrade rows that have no external_id
            # yet — never a manual ``database`` row with a password.
            candidate = await session.scalar(
                select(User).where(
                    User.org_id == org_id, User.email == email,
                )
            )
            if (
                candidate is not None
                and candidate.external_id is None
                and candidate.auth_provider != "database"
            ):
                candidate.auth_provider = provider
                candidate.external_id = subject
                await session.commit()
                await session.refresh(candidate)
                user = candidate

        if user is None:
            if not auth.self_registration_enabled:
                if audit_store is not None:
                    await audit_store.emit(
                        org_id=org_id,
                        type=AuditType.AUTH_FAILED,
                        data=auth_failed_event(
                            "firebase",
                            "self-registration disabled",
                            source_ip=source_ip,
                        ),
                    )
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=(
                        "Self-registration is not enabled for this agent."
                    ),
                )
            if email is None:
                # Firebase tokens MUST carry an email so we have something
                # to put on the User row. Verification is enforced
                # separately on the link-to-existing-user path above —
                # for a fresh account we don't gate on email_verified
                # because Firebase's createUserWithEmailAndPassword always
                # mints an unverified-email token (verification only flips
                # the claim after the user clicks the verification email
                # AND refreshes the ID token).
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Firebase token must include an email.",
                )
            user = User(
                id=uuid.uuid4(),
                org_id=org_id,
                email=email,
                display_name=_display_name_from_firebase_claims(claims, email),
                auth_provider=provider,
                external_id=subject,
            )
            session.add(user)
            await session.commit()
            await session.refresh(user)

    permissions: set[str] = {"sessions:read", "sessions:write", "tools:read"}
    access_token = create_access_token(
        org_id=org_id, user_id=user.id, permissions=permissions,
    )
    refresh_token = create_refresh_token(
        org_id=org_id, user_id=user.id,
    )
    if audit_store is not None:
        await audit_store.emit(
            org_id=org_id,
            user_id=user.id,
            type=AuditType.AUTH_LOGIN,
            data=auth_login_event("firebase", source_ip=source_ip),
        )
    return TokenResponse(
        access_token=access_token, refresh_token=refresh_token,
    )


@router.post("/auth/login", response_model=TokenResponse)
async def login(
    body: LoginRequest,
    request: Request,
    agent_runtime: AgentRuntimeContext = Depends(agent_runtime_context_dep),
) -> TokenResponse:
    """Authenticate a user and issue access + refresh tokens.

    Every attempt — success or failure — is written to ``audit_log``
    via :class:`AuditStore` so compliance tooling can query login
    activity without scanning application logs.
    """
    session_factory = request.app.state.session_factory
    audit_store: AuditStore | None = getattr(
        request.app.state, "audit_store", None,
    )
    source_ip = client_ip(request)

    # Use the request body's org_id if provided, otherwise the tenant
    # resolved per-request via agent_runtime_context_dep (helm mode
    # sources from settings; shared mode from the runtime config).
    org_id = body.org_id
    if org_id is None:
        if not agent_runtime.org_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="org_id is required (tenant has no org configured).",
            )
        org_id = UUID(agent_runtime.org_id)

    provider = DatabaseAuthProvider(session_factory, org_id)
    result = await provider.authenticate(
        {"email": body.email, "password": body.password}
    )

    if not result.authenticated or result.user_id is None:
        if audit_store is not None:
            await audit_store.emit(
                org_id=org_id,
                type=AuditType.AUTH_FAILED,
                data=auth_failed_event(
                    "password",
                    result.error or "invalid credentials",
                    source_ip=source_ip,
                ),
            )
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

    if audit_store is not None:
        await audit_store.emit(
            org_id=org_id,
            user_id=user_id,
            type=AuditType.AUTH_LOGIN,
            data=auth_login_event("password", source_ip=source_ip),
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


# ---------------------------------------------------------------------------
# Channel Pairing
# ---------------------------------------------------------------------------


class UserUpdateRequest(BaseModel):
    """Payload for updating the current user's profile."""

    display_name: str | None = None
    email: str | None = None


@router.patch("/auth/me", response_model=UserResponse)
async def update_me(
    body: UserUpdateRequest,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> UserResponse:
    """Update profile fields for the currently authenticated user."""
    if body.display_name is None and body.email is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="No fields to update.",
        )

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

        if body.display_name is not None:
            stripped = body.display_name.strip()
            if not stripped:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="Display name cannot be empty.",
                )
            user.display_name = stripped
        if body.email is not None:
            user.email = body.email

        await session.commit()
        await session.refresh(user)

    return UserResponse(
        id=user.id,
        org_id=user.org_id,
        email=user.email,
        display_name=user.display_name,
        auth_provider=user.auth_provider,
        created_at=user.created_at,
    )


# ---------------------------------------------------------------------------
# Connected Channels
# ---------------------------------------------------------------------------


class ChannelIdentityResponse(BaseModel):
    """Serialised channel identity returned by the API."""

    id: UUID
    platform: str
    platform_user_id: str
    platform_meta: dict


@router.get("/auth/me/channels", response_model=list[ChannelIdentityResponse])
async def list_my_channels(
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> list[ChannelIdentityResponse]:
    """List channel identities linked to the currently authenticated user."""
    session_factory = request.app.state.session_factory

    async with session_factory() as session:
        result = await session.execute(
            select(ChannelIdentity).where(
                ChannelIdentity.user_id == tenant.user_id,
            )
        )
        rows = result.scalars().all()

    return [
        ChannelIdentityResponse(
            id=row.id,
            platform=row.platform,
            platform_user_id=row.platform_user_id,
            platform_meta=row.platform_meta,
        )
        for row in rows
    ]


@router.delete("/auth/me/channels/{identity_id}", status_code=204)
async def unlink_channel(
    identity_id: UUID,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> None:
    """Remove a channel identity from the currently authenticated user."""
    session_factory = request.app.state.session_factory

    async with session_factory() as session:
        result = await session.execute(
            select(ChannelIdentity).where(
                ChannelIdentity.id == identity_id,
                ChannelIdentity.user_id == tenant.user_id,
            )
        )
        identity = result.scalar_one_or_none()

        if identity is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Channel identity not found.",
            )

        await session.delete(identity)
        await session.commit()


# ---------------------------------------------------------------------------
# Channel Pairing
# ---------------------------------------------------------------------------


class PairingInfoResponse(BaseModel):
    """Info about a pending pairing code (returned before login)."""

    platform: str
    platform_user_id: str
    valid: bool


class LinkChannelRequest(BaseModel):
    code: str


class LinkChannelResponse(BaseModel):
    success: bool
    platform: str
    platform_user_id: str


@router.get("/auth/pairing-info")
async def pairing_info(
    code: str,
    request: Request,
) -> PairingInfoResponse:
    """Look up a pairing code — does NOT require authentication.

    Used by the web UI to show which platform account will be linked
    before the user logs in.
    """
    pairing_store = request.app.state.pairing_store
    if pairing_store is None:
        return PairingInfoResponse(platform="", platform_user_id="", valid=False)

    entry = await pairing_store.get(code)
    if entry is None:
        return PairingInfoResponse(platform="", platform_user_id="", valid=False)

    # Mask the platform user ID — the unauthenticated endpoint should
    # not leak the full Slack/Teams UID to anyone with the code.
    raw_id = entry.get("platform_user_id", "")
    masked_id = f"{raw_id[:2]}***{raw_id[-3:]}" if len(raw_id) > 5 else "***"

    return PairingInfoResponse(
        platform=entry.get("platform", ""),
        platform_user_id=masked_id,
        valid=True,
    )


@router.post("/auth/link-channel", response_model=LinkChannelResponse)
async def link_channel(
    body: LinkChannelRequest,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> LinkChannelResponse:
    """Resolve a pairing code and bind the platform identity to the logged-in user.

    Requires authentication — the logged-in user's account is linked to
    the platform identity encoded in the pairing code.
    """
    pairing_store = request.app.state.pairing_store
    if pairing_store is None:
        raise HTTPException(status_code=503, detail="Pairing service not available.")

    entry = await pairing_store.resolve(body.code)
    if entry is None:
        raise HTTPException(status_code=400, detail="Invalid or expired pairing code.")

    session_factory = request.app.state.session_factory

    async with session_factory() as db:
        # Check if this platform identity is already linked.
        platform = entry.get("platform", "")
        platform_user_id = entry.get("platform_user_id", "")
        platform_meta = entry.get("platform_meta", {})

        existing = await db.execute(
            select(ChannelIdentity)
            .where(ChannelIdentity.platform == platform)
            .where(ChannelIdentity.platform_user_id == platform_user_id)
        )
        if existing.scalar_one_or_none():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"This {platform} account is already linked to a user.",
            )

        identity = ChannelIdentity(
            id=uuid.uuid4(),
            user_id=tenant.user_id,
            platform=platform,
            platform_user_id=platform_user_id,
            platform_meta=platform_meta,
        )
        db.add(identity)
        await db.commit()

    logger.info(
        "Linked %s:%s to user %s via pairing code %s",
        platform, platform_user_id, tenant.user_id, body.code,
    )

    return LinkChannelResponse(
        success=True,
        platform=platform,
        platform_user_id=platform_user_id,
    )
