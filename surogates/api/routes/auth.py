"""Authentication routes -- login, token refresh, and user info."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
import re

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from surogates.audit import (
    AuditStore,
    AuditType,
    auth_failed_event,
    auth_login_event,
    client_ip,
)
from surogates.db.agent_users import SOURCE_LOGIN, ensure_agent_user
from surogates.db.models import ChannelIdentity, Org, User
from surogates.runtime import AgentRuntimeContext, FirebaseConfig, agent_runtime_context_dep
from surogates.runtime.firebase_cache import FirebaseConfigUnavailable, project_firebase
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
    signing_secret,
)
from surogates.tenant.auth.middleware import get_current_tenant
from surogates.tenant.auth.oauth import OAuthTokens
from surogates.tenant.context import TenantContext
from surogates.tenant.invitations import InvalidSetPasswordToken, read_set_password_token
from surogates.tenant.memberships import belongs_to, email_holder, user_orgs
from surogates.tenant.models import UserResponse

logger = logging.getLogger(__name__)

router = APIRouter()

#: What a signed-in user's access token grants, however they signed in.
USER_PERMISSIONS = frozenset({"sessions:read", "sessions:write", "tools:read"})

#: bcrypt hashes no more of a password than this many bytes, and refuses a longer one.
_PASSWORD_MAX_BYTES = 72


def password_refusal(password: str) -> str | None:
    """Why the runtime's rules refuse *password* as a new password; None when they take it."""
    if len(password) < 8:
        return "New password must be at least 8 characters."
    try:
        size = len(password.encode("utf-8"))
    except UnicodeEncodeError:
        # A lone surrogate, which JSON can carry and UTF-8 cannot.
        return "New password must be valid text."
    if size > _PASSWORD_MAX_BYTES:
        return f"New password must be at most {_PASSWORD_MAX_BYTES} bytes."
    return None


#: A Firebase sign-up refused: another user of the organisation has the email.
EMAIL_TAKEN = (
    "That email already belongs to an account in this organisation. "
    "Sign in with its existing method."
)


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


USERNAME_PATTERN = r"^[a-z0-9][a-z0-9._-]{1,30}[a-z0-9]$"


class AuthConfigResponse(BaseModel):
    """Runtime auth shape exposed by ``GET /v1/auth/config``."""

    agent_id: str
    self_registration_enabled: bool
    firebase: FirebaseWebConfig | None = None
    # Enabled built-in slash commands for this agent, as canonical
    # hyphenated ids (e.g. "loop", "deep-research").  The web SPA hides
    # disabled commands from the composer menu + navbar.  "clear" has no
    # flag and is always present.
    slash_commands: list[str] = Field(default_factory=list)
    # "Multi session" capability.  When False each user keeps one session
    # per channel; the SPA hides "New chat" and pins the conversation.
    multi_session: bool = True
    # "Live browser support" capability.  When False the SPA hides the
    # Browser Profiles settings tab and the composer's browser-profile
    # picker (the agent's browser_* tools are already removed server-side).
    # Defaults True so an older backend keeps browser affordances visible.
    browser_enabled: bool = True
    # Chats can work on a folder of the user's computer through Surogate
    # Desktop.  An older server omits it, and the desktop then offers cloud
    # chats only.
    desktop_sessions: bool = True
    # Projects: coordinator chats under /v1/workstreams.  An older server
    # omits it, and the desktop then shows no Projects.
    workstreams: bool = True
    # Active messaging channels an end-user can link their identity to
    # (subset of slack/teams/telegram).  Empty means the agent has no
    # such channel, so the SPA hides "Connected Channels" entirely.
    linkable_channels: list[str] = Field(default_factory=list)
    # Username handle rule (source of truth: USERNAME_RE below). The
    # sign-up form pre-validates with this so the client can never
    # drift from the server.
    username_pattern: str = USERNAME_PATTERN
    # Monetization projection: on paid agents the login page routes
    # NEW users to the buy page (account creation happens inside the
    # purchase flow, so they arrive entitled); free agents keep local
    # self-registration. Public data — the buy page itself is public.
    commerce_mode: str = "free"
    commerce_buy_url: str | None = None
    # Where the agent's users see their usage and billing: the console the
    # operator names (SUROGATES_API_CONSOLE_URL).  None: the agent names
    # none, and Surogate Desktop shows no Usage or Plans and billing.
    console_url: str | None = None


class FirebaseExchangeRequest(BaseModel):
    id_token: str
    # Optional sign-up profile, applied atomically when this exchange
    # auto-provisions a NEW user (the sign-up form collected them).
    # Existing users are never overwritten here — profile edits go
    # through PATCH /auth/me. Invalid or taken usernames are skipped
    # rather than failing the sign-in.
    display_name: str | None = None
    username: str | None = None
    phone: str | None = None


class OrgAuthConfig(BaseModel):
    """How to sign in to an organisation: ``GET /v1/auth/orgs/{org_id}/config``.

    ``firebase`` is None when the organisation has no Firebase project:
    its users sign in with a password.
    """

    org_id: UUID
    org_name: str
    firebase: FirebaseWebConfig | None = None


class OrgLoginRequest(BaseModel):
    email: str
    password: str


class OrgFirebaseExchangeRequest(BaseModel):
    id_token: str


# A missing field defaults to empty, which is refused as any other link
# or password is: FastAPI's 422 for a missing field would quote the whole
# body back, the token with it.


class SetPasswordCheckRequest(BaseModel):
    token: str = ""


class SetPasswordCheckResponse(BaseModel):
    """Whose password a set-password link sets."""

    email: str
    display_name: str


class SetPasswordRequest(BaseModel):
    token: str = ""
    password: str = ""


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/auth/config", response_model=AuthConfigResponse)
async def auth_config(
    request: Request,
    agent_runtime: AgentRuntimeContext = Depends(agent_runtime_context_dep),
) -> AuthConfigResponse:
    """Public runtime auth config consumed by the agent web app.

    Resolves Firebase config per-project via :class:`FirebaseConfigCache`
    (which fronts ops's ``GET /api/projects/{id}/firebase-config``).
    Returns ``self_registration_enabled=False`` when no Firebase
    config is configured for this project so the SPA falls back to
    local login.
    """
    slash_commands = sorted(agent_runtime.slash_commands.commands)
    from surogates.api.routes._commerce_turn import runtime_commerce_payload

    commerce = await runtime_commerce_payload(
        request, str(agent_runtime.agent_id),
    )
    # Capability + monetization fields shared by every branch below; only
    # self_registration_enabled and firebase differ per resolution outcome.
    base = dict(
        agent_id=agent_runtime.agent_id,
        slash_commands=slash_commands,
        multi_session=agent_runtime.multi_session,
        browser_enabled=agent_runtime.browser_enabled,
        linkable_channels=list(agent_runtime.linkable_channels),
        commerce_mode=str(commerce.get("commerce_mode") or "free"),
        commerce_buy_url=commerce.get("commerce_buy_url"),
        console_url=request.app.state.settings.api.console_url or None,
    )
    cache = getattr(request.app.state, "firebase_config_cache", None)
    project_id = getattr(agent_runtime, "project_id", None)
    if cache is None or not project_id:
        return AuthConfigResponse(
            self_registration_enabled=False, firebase=None, **base,
        )
    try:
        fb = await cache.get(project_id)
    except LookupError:
        return AuthConfigResponse(
            self_registration_enabled=False, firebase=None, **base,
        )
    return AuthConfigResponse(
        self_registration_enabled=True, firebase=_web_config(fb), **base,
    )


def _web_config(fb: FirebaseConfig) -> FirebaseWebConfig:
    """The public part of a project's Firebase config, for a sign-in page."""
    return FirebaseWebConfig(
        api_key=fb.api_key,
        auth_domain=fb.auth_domain,
        project_id=fb.firebase_project_id,
        app_id=fb.app_id or None,
        messaging_sender_id=fb.messaging_sender_id or None,
        measurement_id=fb.measurement_id or None,
        enabled_providers=list(fb.enabled_providers),
    )


def _email_from_firebase_claims(claims: dict) -> tuple[str | None, bool]:
    """Return ``(email, verified)`` extracted from Firebase token claims."""
    email = claims.get("email")
    if not isinstance(email, str) or not email.strip():
        return None, False
    return email.strip().lower(), claims.get("email_verified") is True


def _sign_in_provider_from_claims(claims: dict) -> str | None:
    """The Firebase sign-in method from token claims.

    Firebase nests it as ``firebase.sign_in_provider`` (e.g. "password",
    "google.com", "github.com").  Returns ``None`` when absent so callers
    leave the stored value untouched rather than clobbering it.
    """
    firebase = claims.get("firebase")
    if isinstance(firebase, dict):
        method = firebase.get("sign_in_provider")
        if isinstance(method, str) and method:
            return method
    return None


def _firebase_auth_time(claims: dict) -> int | None:
    """When the user signed in to Firebase, from the verified ID token.

    A fresh ID token of an old Firebase session is no new sign-in. A
    token that names no time is not recent.
    """
    auth_time = claims.get("auth_time")
    return auth_time if isinstance(auth_time, int) else None


def _issue_tokens(org_id: UUID, user_id: UUID, auth_time: int | None) -> TokenResponse:
    """A user's access and refresh tokens for *org_id*, signed in at *auth_time*."""
    return TokenResponse(
        access_token=create_access_token(
            org_id=org_id, user_id=user_id, permissions=USER_PERMISSIONS, auth_time=auth_time,
        ),
        refresh_token=create_refresh_token(
            org_id=org_id, user_id=user_id, auth_time=auth_time,
        ),
    )


async def _firebase_user(
    session: AsyncSession,
    org_id: UUID,
    provider: str,
    subject: str,
    email: str | None,
    verified: bool,
    *,
    link_by_email: bool,
) -> User | None:
    """The user of *org_id* who signs in with this Firebase identity, or None.

    First by identity, ``(auth_provider, external_id)``, among the
    organisation's users, home and member. When a home user and a member
    match, the home user is the one taken.

    Failing that, and only when *link_by_email* allows it, a home user of
    the organisation with the verified email is linked to the identity,
    when they have no external id yet and are not a ``database`` user: an
    invited account's first sign-in. The link is never a silent backdoor
    where sign-up is off. A manual ``database`` row with a password is
    never linked. Nor is a member whose home is another organisation:
    whoever runs this organisation's Firebase project could otherwise
    mint a verified token for the member's email and take over their
    account. A member signs in with their own identity, and their
    membership admits them.
    """
    user = await session.scalar(
        select(User)
        .where(belongs_to(org_id), User.auth_provider == provider, User.external_id == subject)
        .order_by((User.org_id == org_id).desc())
        .limit(1)
    )
    if user is not None or not link_by_email or not email or not verified:
        return user
    candidate = await session.scalar(
        select(User).where(User.org_id == org_id, User.email == email)
    )
    if (
        candidate is None
        or candidate.external_id is not None
        or candidate.auth_provider == "database"
    ):
        return None
    candidate.auth_provider = provider
    candidate.external_id = subject
    await session.commit()
    await session.refresh(candidate)
    return candidate


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

    The agent's organisation's users sign in here, its home users and its
    members. A new account is never created with an email another user of
    the organisation has: that answers 409.

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
    audit_store: AuditStore | None = getattr(
        request.app.state, "audit_store", None,
    )
    source_ip = client_ip(request)

    if not agent_runtime.org_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="org_id is required (tenant has no org configured).",
        )
    org_id = UUID(agent_runtime.org_id)

    # Resolve Firebase per-project via the cache.  Self-registration
    # is gated by the per-project Firebase row existing at all —
    # projects that haven't opted in have no row and the LookupError
    # below bails out with 404.
    cache = getattr(request.app.state, "firebase_config_cache", None)
    project_id = getattr(agent_runtime, "project_id", None)
    fb = None
    if cache is not None and project_id:
        try:
            fb = await cache.get(project_id)
        except LookupError:
            fb = None
    if fb is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Firebase auth is not configured.",
        )
    fb_project_id = fb.firebase_project_id
    self_reg_enabled = True

    try:
        claims = await verify_firebase_id_token(
            body.id_token, fb_project_id,
        )
    except FirebaseTokenError as exc:
        if audit_store is not None:
            await audit_store.emit(
                org_id=org_id,
                agent_id=agent_runtime.agent_id,
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
    provider = firebase_auth_provider_name(fb_project_id)
    sign_in_method = _sign_in_provider_from_claims(claims)

    session_factory = request.app.state.session_factory
    async with session_factory() as session:
        # The organisation's users, home and member alike, by Firebase
        # identity, then a home user by a verified email: only during
        # self-registration, never as a silent backdoor when it is off.
        user = await _firebase_user(
            session, org_id, provider, subject, email, email_verified,
            link_by_email=self_reg_enabled,
        )

        if user is None:
            if not self_reg_enabled:
                if audit_store is not None:
                    await audit_store.emit(
                        org_id=org_id,
                        agent_id=agent_runtime.agent_id,
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
            # The email is the login key among the organisation's users,
            # home and member alike; the unique index below sees only its
            # home users. Refuse rather than shadow an account that has it.
            taken = await session.scalar(
                select(User.id)
                .where(belongs_to(org_id), func.lower(User.email) == email)
                .limit(1)
            )
            if taken is not None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT, detail=EMAIL_TAKEN,
                )
            requested_username = (
                body.username.strip().lower() if body.username else None
            )
            if requested_username is not None and (
                not USERNAME_RE.fullmatch(requested_username)
                or await session.scalar(
                    select(User.id).where(
                        User.org_id == org_id,
                        func.lower(User.username) == requested_username,
                    )
                )
                is not None
            ):
                # Sign-in must not fail over the handle: skip it and
                # let the user pick another via PATCH /auth/me.
                requested_username = None
            user = User(
                id=uuid.uuid4(),
                org_id=org_id,
                email=email,
                display_name=(
                    (body.display_name or "").strip()
                    or _display_name_from_firebase_claims(claims, email)
                ),
                auth_provider=provider,
                external_id=subject,
                username=requested_username,
                phone=(body.phone or "").strip() or None,
                sign_in_provider=sign_in_method,
            )
            session.add(user)
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                if "uq_users_org_lower_email" in str(getattr(exc, "orig", exc)):
                    # The email is the org-scoped login key and already
                    # belongs to another account — most often a manual
                    # ``database`` row, which we deliberately never link
                    # to Firebase. Refuse rather than shadow it.
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=EMAIL_TAKEN,
                    ) from exc
                # The username unique index caught a concurrent claim;
                # retry once without it — account creation wins.
                user.username = None
                session.add(user)
                await session.commit()
            await session.refresh(user)

        # Record how they signed in (only when the token carried it, so a
        # malformed claim never clobbers a known value). Persisted even on
        # the no-agent path below via its own commit.
        if sign_in_method is not None and user.sign_in_provider != sign_in_method:
            user.sign_in_provider = sign_in_method
            await session.commit()

        # Successful auth through THIS agent's web channel is
        # assignment intent — enroll the user on the serving agent.
        if agent_runtime.agent_id:
            await ensure_agent_user(
                session,
                org_id=org_id,
                agent_id=agent_runtime.agent_id,
                user_id=user.id,
                source=SOURCE_LOGIN,
            )
            await session.commit()

    tokens = _issue_tokens(org_id, user.id, _firebase_auth_time(claims))
    if audit_store is not None:
        await audit_store.emit(
            org_id=org_id,
            agent_id=agent_runtime.agent_id,
            user_id=user.id,
            type=AuditType.AUTH_LOGIN,
            data=auth_login_event("firebase", source_ip=source_ip),
        )
    return tokens


@router.post("/auth/login", response_model=TokenResponse)
async def login(
    body: LoginRequest,
    request: Request,
    agent_runtime: AgentRuntimeContext = Depends(agent_runtime_context_dep),
) -> TokenResponse:
    """Authenticate a user and issue access + refresh tokens.

    Any user of the organisation signs in, a home user or a member, with
    the password of their account.

    Every attempt — success or failure — is written to ``audit_log``
    via :class:`AuditStore` so compliance tooling can query login
    activity without scanning application logs.
    """
    session_factory = request.app.state.session_factory
    audit_store: AuditStore | None = getattr(
        request.app.state, "audit_store", None,
    )
    source_ip = client_ip(request)

    # Use the request body's org_id if provided, otherwise the
    # tenant resolved per-request via agent_runtime_context_dep.
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
                agent_id=agent_runtime.agent_id,
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

    # Successful auth through THIS agent's web channel is assignment
    # intent — enroll the user on the serving agent. Only when the
    # authenticated org IS the serving agent's org: the body may carry
    # a different org_id, and a binding pairing org X with org Y's
    # agent would corrupt both tenants' rosters.
    if agent_runtime.agent_id and str(org_id) == str(agent_runtime.org_id):
        async with session_factory() as enroll_session:
            await ensure_agent_user(
                enroll_session,
                org_id=org_id,
                agent_id=agent_runtime.agent_id,
                user_id=user_id,
                source=SOURCE_LOGIN,
            )
            await enroll_session.commit()

    tokens = _issue_tokens(org_id, user_id, int(time.time()))

    if audit_store is not None:
        await audit_store.emit(
            org_id=org_id,
            agent_id=agent_runtime.agent_id,
            user_id=user_id,
            type=AuditType.AUTH_LOGIN,
            data=auth_login_event("password", source_ip=source_ip),
        )

    return tokens


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

    # Surogate Desktop's window holds a session of the desktop's sign-in (sid): it ends with it.
    sid = payload.get("sid")
    family_id = UUID(sid) if isinstance(sid, str) else None
    if family_id is not None and not await OAuthTokens(request.app.state.session_factory).live(family_id):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="This sign-in has ended. Sign in again from Surogate Desktop.",
        )

    # Only while the user still belongs to the token's organisation: a
    # member taken out of it mints no new access token.
    async with request.app.state.session_factory() as session:
        belongs = await session.scalar(
            select(User.id).where(User.id == user_id, belongs_to(org_id))
        )
    if belongs is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found.",
        )

    # The sign-in's time, not the refresh's: a refresh is not a sign-in.
    signed_in = payload.get("auth_time")
    access_token = create_access_token(
        org_id=org_id,
        user_id=user_id,
        permissions=USER_PERMISSIONS,
        auth_time=signed_in if isinstance(signed_in, int) else None,
        family_id=family_id,
    )

    return AccessTokenResponse(access_token=access_token)


# ---------------------------------------------------------------------------
# Sign-in by organisation
# ---------------------------------------------------------------------------
#
# For a client that serves several organisations and no agent. The
# organisation in the path decides: these routes resolve no agent, ignore
# a Host subdomain and ``?agent_id=``, and enrol the user on no agent.
# Their tokens name the organisation, and are accepted while the user
# belongs to it, as a home user or as a member.

#: The Firebase exchange of an organisation found no account for the identity.
NO_ACCOUNT_IN_ORG = "You have no account in this organisation. Ask to be invited."


async def _org_or_404(session: AsyncSession, org_id: UUID) -> Org:
    org = await session.get(Org, org_id)
    if org is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organisation not found.",
        )
    return org


async def _org_firebase(request: Request, org_id: UUID) -> FirebaseConfig | None:
    """The Firebase project of *org_id*, or None when it has none.

    An organisation's id is its ops project's id, the key the Firebase
    config cache is read by. A platform that fails to answer is not "no
    Firebase": a password form shown for a Firebase organisation would be
    wrong, so that answers 503.
    """
    cache = getattr(request.app.state, "firebase_config_cache", None)
    try:
        return await project_firebase(cache, str(org_id))
    except FirebaseConfigUnavailable as unavailable:
        failure = unavailable.__cause__
        logger.warning(
            "Firebase config of organisation %s is unavailable", org_id, exc_info=failure,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Firebase auth is unavailable. Try again shortly.",
        ) from failure


@router.get("/auth/orgs/{org_id}/config", response_model=OrgAuthConfig)
async def org_auth_config(org_id: UUID, request: Request) -> OrgAuthConfig:
    """The organisation's name, and its Firebase web config when it has one."""
    async with request.app.state.session_factory() as session:
        org = await _org_or_404(session, org_id)
    fb = await _org_firebase(request, org_id)
    return OrgAuthConfig(
        org_id=org.id,
        org_name=org.name,
        firebase=_web_config(fb) if fb is not None else None,
    )


@router.post("/auth/orgs/{org_id}/login", response_model=TokenResponse)
async def org_login(
    org_id: UUID, body: OrgLoginRequest, request: Request,
) -> TokenResponse:
    """Sign in to an organisation with a password.

    Any of the organisation's users signs in, home or member, with the
    password of their account. Every failure answers the same 401, so the
    answer never tells whether an email has an account; the audit log
    keeps the reason.
    """
    session_factory = request.app.state.session_factory
    audit_store: AuditStore | None = getattr(
        request.app.state, "audit_store", None,
    )
    source_ip = client_ip(request)
    async with session_factory() as session:
        await _org_or_404(session, org_id)

    result = await DatabaseAuthProvider(session_factory, org_id).authenticate(
        {"email": body.email, "password": body.password}
    )
    if not result.authenticated or result.user_id is None:
        if audit_store is not None:
            await audit_store.emit(
                org_id=org_id,
                agent_id=None,
                type=AuditType.AUTH_FAILED,
                data=auth_failed_event(
                    "password",
                    result.error or "invalid credentials",
                    source_ip=source_ip,
                ),
            )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials.",
        )

    user_id = UUID(result.user_id)
    tokens = _issue_tokens(org_id, user_id, int(time.time()))
    if audit_store is not None:
        await audit_store.emit(
            org_id=org_id,
            agent_id=None,
            user_id=user_id,
            type=AuditType.AUTH_LOGIN,
            data=auth_login_event("password", source_ip=source_ip),
        )
    return tokens


@router.post("/auth/orgs/{org_id}/firebase/exchange", response_model=TokenResponse)
async def org_firebase_exchange(
    org_id: UUID, body: OrgFirebaseExchangeRequest, request: Request,
) -> TokenResponse:
    """Exchange an ID token of the organisation's Firebase project for tokens.

    It never creates a user. The organisation's user, home or member, is
    found by Firebase identity. Failing that, an invited account whose
    home is the organisation, with the verified email, is linked to it.
    Anyone else gets 403.

    One login spans organisations for local password accounts, and for
    users of one Firebase project the organisations share. A member whose
    account belongs to another Firebase project has a different identity
    there, so their sign-in here is refused with 403.
    """
    session_factory = request.app.state.session_factory
    audit_store: AuditStore | None = getattr(
        request.app.state, "audit_store", None,
    )
    source_ip = client_ip(request)
    async with session_factory() as session:
        await _org_or_404(session, org_id)
    fb = await _org_firebase(request, org_id)
    if fb is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Firebase auth is not configured.",
        )

    try:
        claims = await verify_firebase_id_token(
            body.id_token, fb.firebase_project_id,
        )
    except FirebaseTokenError as exc:
        if audit_store is not None:
            await audit_store.emit(
                org_id=org_id,
                agent_id=None,
                type=AuditType.AUTH_FAILED,
                data=auth_failed_event("firebase", str(exc), source_ip=source_ip),
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
    provider = firebase_auth_provider_name(fb.firebase_project_id)
    sign_in_method = _sign_in_provider_from_claims(claims)

    async with session_factory() as session:
        user = await _firebase_user(
            session, org_id, provider, subject, email, email_verified,
            link_by_email=True,
        )
        if user is None:
            if audit_store is not None:
                await audit_store.emit(
                    org_id=org_id,
                    agent_id=None,
                    type=AuditType.AUTH_FAILED,
                    data=auth_failed_event(
                        "firebase",
                        "no account in this organisation",
                        source_ip=source_ip,
                    ),
                )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail=NO_ACCOUNT_IN_ORG,
            )
        # Record how they signed in, only when the token carried it.
        if sign_in_method is not None and user.sign_in_provider != sign_in_method:
            user.sign_in_provider = sign_in_method
            await session.commit()
        user_id = user.id

    tokens = _issue_tokens(org_id, user_id, _firebase_auth_time(claims))
    if audit_store is not None:
        await audit_store.emit(
            org_id=org_id,
            agent_id=None,
            user_id=user_id,
            type=AuditType.AUTH_LOGIN,
            data=auth_login_event("firebase", source_ip=source_ip),
        )
    return tokens


#: Every set-password link refused answers this, whatever the reason: the answer never tells why.
SET_PASSWORD_LINK_REFUSED = "This link is not valid or has expired."


async def _set_password_user(session: AsyncSession, token: str, org_id: UUID, *, lock: bool) -> User:
    """The user a set-password link names, locked for the session's transaction when *lock*: 400 for any link refused."""
    try:
        return await read_set_password_token(
            session, token, org_id, signing_secret(), datetime.now(UTC), lock=lock,
        )
    except InvalidSetPasswordToken:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=SET_PASSWORD_LINK_REFUSED,
        ) from None


@router.post("/auth/orgs/{org_id}/set-password/check", response_model=SetPasswordCheckResponse)
async def check_set_password_link(
    org_id: UUID, body: SetPasswordCheckRequest, request: Request,
) -> SetPasswordCheckResponse:
    """Whose password a set-password link sets: their email and display name.  It writes nothing.

    The link is the token Keel's invitation of a new password user gives:
    see :mod:`surogates.tenant.invitations`.
    """
    async with request.app.state.session_factory() as session:
        await _org_or_404(session, org_id)
        user = await _set_password_user(session, body.token, org_id, lock=False)
        return SetPasswordCheckResponse(email=user.email, display_name=user.display_name)


@router.post("/auth/orgs/{org_id}/set-password", response_model=TokenResponse)
async def set_password_from_link(
    org_id: UUID, body: SetPasswordRequest, request: Request,
) -> TokenResponse:
    """Set the password a set-password link names, and sign in to the organisation with it.

    The link works once: setting the password changes the digest it
    carries.  It is checked, then the password is hashed, then the link is
    checked again with the user's row locked, in the transaction that
    writes the hash: of two uses at once, the second waits for the first,
    finds the password set and is refused.  The tokens are those of the
    organisation's password login.
    """
    session_factory = request.app.state.session_factory
    audit_store: AuditStore | None = getattr(
        request.app.state, "audit_store", None,
    )
    async with session_factory() as session:
        await _org_or_404(session, org_id)
        await _set_password_user(session, body.token, org_id, lock=False)
    refusal = password_refusal(body.password)
    if refusal is not None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=refusal,
        )
    password_hash = await asyncio.to_thread(DatabaseAuthProvider.hash_password, body.password)
    async with session_factory() as session:
        user = await _set_password_user(session, body.token, org_id, lock=True)
        user.password_hash = password_hash
        user_id = user.id
        await session.commit()

    tokens = _issue_tokens(org_id, user_id, int(time.time()))
    if audit_store is not None:
        await audit_store.emit(
            org_id=org_id,
            agent_id=None,
            user_id=user_id,
            type=AuditType.AUTH_LOGIN,
            data=auth_login_event("password", source_ip=client_ip(request)),
        )
    return tokens


class MeOrganisation(BaseModel):
    """An organisation the signed-in user belongs to."""

    id: UUID
    name: str


class MeResponse(UserResponse):
    """The signed-in user, with their organisation's name: Surogate Desktop's Settings shows it.

    ``org_id`` and ``org_name`` name the organisation the token was signed
    in through. ``organisations`` lists every organisation the user
    belongs to: their home one first, then the others by name.
    """

    org_name: str
    organisations: list[MeOrganisation]


async def _me(session: AsyncSession, user: User, org_id: UUID) -> MeResponse:
    """*user* as signed in through *org_id*; 404 once they no longer belong to it."""
    organisations = await user_orgs(session, user.id)
    org_name = next((o.name for o in organisations if o.id == org_id), None)
    if org_name is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found.",
        )
    return MeResponse(
        id=user.id,
        org_id=org_id,
        email=user.email,
        display_name=user.display_name,
        username=user.username,
        phone=user.phone,
        auth_provider=user.auth_provider,
        sign_in_provider=user.sign_in_provider,
        created_at=user.created_at,
        org_name=org_name,
        organisations=[MeOrganisation(id=o.id, name=o.name) for o in organisations],
    )


@router.get("/auth/me", response_model=MeResponse)
async def me(
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> MeResponse:
    """Return profile information for the currently authenticated user."""
    session_factory = request.app.state.session_factory
    async with session_factory() as session:
        # By id alone: the dependency has checked that the user belongs
        # to the token's organisation, home or member.
        user = await session.scalar(select(User).where(User.id == tenant.user_id))
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found.",
            )
        return await _me(session, user, tenant.org_id)


# ---------------------------------------------------------------------------
# Channel Pairing
# ---------------------------------------------------------------------------


# Lowercase handle: 3-32 chars of [a-z0-9._-], starting and ending
# alphanumeric. Validated after lowercasing, so any case is accepted
# on the wire. Served to clients via /auth/config's username_pattern —
# one source of truth, no frontend mirror to drift.
USERNAME_RE = re.compile(USERNAME_PATTERN)


class UserUpdateRequest(BaseModel):
    """Payload for updating the current user's profile."""

    display_name: str | None = None
    email: str | None = None
    username: str | None = None
    phone: str | None = None


@router.patch("/auth/me", response_model=MeResponse)
async def update_me(
    body: UserUpdateRequest,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> MeResponse:
    """Update profile fields for the currently authenticated user."""
    if (
        body.display_name is None
        and body.email is None
        and body.username is None
        and body.phone is None
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="No fields to update.",
        )

    username = body.username.strip().lower() if body.username is not None else None
    if username is not None and not USERNAME_RE.fullmatch(username):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "Username must be 3-32 characters (letters, digits, "
                "dots, dashes, underscores) and start/end with a letter "
                "or digit."
            ),
        )

    session_factory = request.app.state.session_factory

    async with session_factory() as session:
        # By id alone: the dependency has checked that the user belongs
        # to the token's organisation, home or member.
        result = await session.execute(
            select(User).where(User.id == tenant.user_id)
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
            # The email is the password login's key in every organisation
            # the user belongs to, not only the home one the unique index
            # covers: another user of any of them keeps it.
            org_ids = [org.id for org in await user_orgs(session, user.id)]
            if await email_holder(session, org_ids, body.email, other_than=user.id) is not None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="That email is already in use.",
                )
            user.email = body.email
        if username is not None:
            # In the user's home organisation, where the unique index is.
            taken = await session.scalar(
                select(User.id).where(
                    User.org_id == user.org_id,
                    func.lower(User.username) == username,
                    User.id != user.id,
                )
            )
            if taken is not None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="That username is already taken.",
                )
            user.username = username
        if body.phone is not None:
            # Optional field: an empty string clears it.
            user.phone = body.phone.strip() or None

        try:
            await session.commit()
        except IntegrityError as exc:
            # Two org-scoped unique indexes can fire here: the
            # username handle and the login-key e-mail. Name the right
            # one — a mislabeled conflict sends the user fixing the
            # wrong field.
            constraint = str(getattr(exc, "orig", exc))
            if "uq_users_org_lower_email" in constraint:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="That email is already in use.",
                )
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="That username is already taken.",
            )
        await session.refresh(user)
        return await _me(session, user, tenant.org_id)


# ---------------------------------------------------------------------------
# Password
# ---------------------------------------------------------------------------


class ChangePasswordRequest(BaseModel):
    """Payload for changing a local (database) account's password."""

    current_password: str
    new_password: str


@router.post("/auth/me/password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(
    body: ChangePasswordRequest,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> None:
    """Change the password of a local (``database``) account.

    Only accounts with a stored bcrypt hash can change their password
    here.  Firebase-backed users manage credentials through their
    identity provider — the web app offers them a reset-email flow
    instead — so this route refuses them with 409.
    """
    refusal = password_refusal(body.new_password)
    if refusal is not None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=refusal,
        )

    session_factory = request.app.state.session_factory
    async with session_factory() as session:
        # By id alone: the dependency has checked that the user belongs
        # to the token's organisation, home or member.
        result = await session.execute(
            select(User).where(User.id == tenant.user_id)
        )
        user = result.scalar_one_or_none()
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found.",
            )
        if user.auth_provider != "database" or not user.password_hash:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "Password changes are managed by your sign-in provider."
                ),
            )
        if not DatabaseAuthProvider.verify_password(
            body.current_password, user.password_hash,
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Current password is incorrect.",
            )
        user.password_hash = DatabaseAuthProvider.hash_password(
            body.new_password,
        )
        await session.commit()


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
    # Treat a code with no org metadata as invalid: org-bound codes always carry
    # an org_id, so a missing one is a malformed / pre-migration entry that must
    # not be presented as linkable.
    if entry is None or not entry.get("org_id"):
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

    # Peek (non-destructive) first: org-bound codes must not be consumed while
    # logged into the wrong org (which would both mis-bind and burn a still-valid
    # code).  Validate org BEFORE the single-use resolve, so a wrong-org attempt
    # leaves the code alive for the correct org.
    entry = await pairing_store.get(body.code)
    if entry is None:
        raise HTTPException(status_code=400, detail="Invalid or expired pairing code.")

    if str(entry.get("org_id", "")) != str(tenant.org_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Pairing code is not valid for this organization.",
        )

    # Org matches — now consume the code (single-use) and bind.
    entry = await pairing_store.resolve(body.code)
    if entry is None:
        raise HTTPException(status_code=400, detail="Invalid or expired pairing code.")

    session_factory = request.app.state.session_factory

    async with session_factory() as db:
        platform = entry.get("platform", "")
        platform_user_id = entry.get("platform_user_id", "")
        platform_meta = entry.get("platform_meta", {})

        row = (
            await db.execute(
                select(ChannelIdentity, User)
                .join(User, ChannelIdentity.user_id == User.id)
                .where(ChannelIdentity.org_id == tenant.org_id)
                .where(ChannelIdentity.platform == platform)
                .where(ChannelIdentity.platform_user_id == platform_user_id)
            )
        ).first()

        if row is None:
            db.add(ChannelIdentity(
                id=uuid.uuid4(),
                org_id=tenant.org_id,
                user_id=tenant.user_id,
                platform=platform,
                platform_user_id=platform_user_id,
                platform_meta=platform_meta,
            ))
            await db.commit()
        else:
            identity, existing_user = row
            if identity.user_id == tenant.user_id:
                pass  # already linked to this user — idempotent
            elif existing_user.auth_provider == platform:
                # A Mate shadow identity (auth_provider == platform): take it
                # over by re-pointing it to the authenticated real account.
                identity.user_id = tenant.user_id
                await db.commit()
            else:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"This {platform} account is already linked to a user.",
                )

    logger.info(
        "Linked %s:%s to user %s via pairing code %s",
        platform, platform_user_id, tenant.user_id, body.code,
    )

    # Evict the sender's negative-cached "unknown" entry on the channels pod so
    # they are recognized on their next message instead of being re-prompted
    # until the identity cache's TTL expires.  The identifier is the channels
    # identity-cache key verbatim (``<platform>\x00<user>\x00<org>``).  Best
    # effort: a publish failure must not fail the bind — the entry still ages
    # out on its own.
    redis = getattr(request.app.state, "redis", None)
    if redis is not None:
        invalidation = (
            f"channel_identity_changed:{platform}\x00{platform_user_id}\x00{tenant.org_id}"
        )
        try:
            await redis.publish(invalidation, b"")
        except Exception:  # noqa: BLE001 — best-effort cross-process invalidation
            logger.warning(
                "Failed to publish channel_identity_changed for %s:%s",
                platform, platform_user_id, exc_info=True,
            )

    return LinkChannelResponse(
        success=True,
        platform=platform,
        platform_user_id=platform_user_id,
    )
