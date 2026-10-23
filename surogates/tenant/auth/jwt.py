"""JWT token issuance and validation.

The platform issues its own HS256 JWTs after the pluggable auth provider
has verified the user's credentials.  Tokens carry the ``org_id``,
``user_id``, granted ``permissions``, and a ``type`` discriminator
(``"access"`` vs ``"refresh"``).

A set-password token is of another kind: it names one ``purpose`` and
carries no ``type`` and no ``permissions``, so :func:`decode_token` never
accepts it, and it is read only by :func:`decode_set_password_token`.
"""

from __future__ import annotations

import os
import time
from typing import Any
from uuid import UUID

from jose import JWTError, jwt

__all__ = [
    "SET_PASSWORD_PURPOSE",
    "create_access_token",
    "create_channel_session_token",
    "create_refresh_token",
    "create_sandbox_token",
    "create_service_account_session_token",
    "create_set_password_token",
    "decode_set_password_token",
    "decode_token",
    "signing_secret",
    "InvalidTokenError",
]

_ALGORITHM = "HS256"


def _get_secret() -> str:
    """Return the signing secret, raising early if it is not configured."""
    secret = os.environ.get("SUROGATES_JWT_SECRET")
    if not secret:
        raise RuntimeError(
            "SUROGATES_JWT_SECRET environment variable is not set. "
            "JWT operations require a secret key."
        )
    return secret


def signing_secret() -> str:
    """The runtime's signing secret, ``SUROGATES_JWT_SECRET``: every token here is signed with it."""
    return _get_secret()


# ------------------------------------------------------------------
# Token creation
# ------------------------------------------------------------------


def create_access_token(
    org_id: UUID,
    user_id: UUID,
    permissions: set[str] | frozenset[str],
    expires_minutes: int = 30,
    *,
    auth_time: int | None = None,
    client_id: str | None = None,
    family_id: UUID | None = None,
) -> str:
    """Create a short-lived access token.

    *auth_time* is when the user last signed in (epoch seconds), carried
    through every refresh, so a route can ask for a recent sign-in.
    *client_id* names the OAuth client the token was issued to, and
    *family_id* (the ``sid`` claim) the OAuth sign-in it was issued under.
    """
    now = int(time.time())
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "org_id": str(org_id),
        "user_id": str(user_id),
        "permissions": sorted(permissions),
        "type": "access",
        "iat": now,
        "exp": now + expires_minutes * 60,
    }
    if auth_time is not None:
        payload["auth_time"] = auth_time
    if client_id is not None:
        payload["client_id"] = client_id
    if family_id is not None:
        payload["sid"] = str(family_id)
    return jwt.encode(payload, _get_secret(), algorithm=_ALGORITHM)


#: Default lifetime for a ``service_account_session`` JWT.  The worker
#: mints one at the start of a session and reuses it until the harness
#: releases the session, so the lifetime must exceed the longest
#: session we expect to run — synthetic-data pipelines routinely run
#: multi-hour workflows, and a mid-flight expiry would silently 401
#: every subsequent skills/memory call.  Revocation is bounded by the
#: service-account auth cache's TTL (see
#: ``surogates.tenant.auth.service_account``): the process that
#: performs the revoke evicts its cache entry immediately; peer
#: processes converge on the revocation within the cache TTL.
_SERVICE_ACCOUNT_SESSION_TOKEN_MINUTES: int = 365 * 24 * 60


def create_service_account_session_token(
    org_id: UUID,
    service_account_id: UUID,
    session_id: UUID,
    expires_minutes: int = _SERVICE_ACCOUNT_SESSION_TOKEN_MINUTES,
) -> str:
    """Mint a long-lived JWT for worker → API calls on an SA-owned session.

    The worker uses this token so :class:`HarnessAPIClient` can reach
    ``/v1/skills`` and ``/v1/memory`` on behalf of a service-account
    session (which has no user identity and therefore cannot mint a
    normal access token).  The token is distinct from a bare ``surg_sk_``
    key: it carries a session-scope claim (``session_id``) so a leak
    cannot be reused to submit new prompts or act on other sessions —
    :meth:`TenantContext.covers_session` enforces it on every
    session-scoped route.

    Default lifetime is one year so long-running pipeline sessions
    don't hit mid-flight expiry.  A compromised session JWT cannot
    outlive its service account, but revocation convergence is
    bounded by the SA auth cache TTL (immediate in the revoking
    process; peer processes within the TTL window).
    """
    now = int(time.time())
    payload: dict[str, Any] = {
        "sub": str(service_account_id),
        "org_id": str(org_id),
        "service_account_id": str(service_account_id),
        "session_id": str(session_id),
        "permissions": [],
        "type": "service_account_session",
        "iat": now,
        "exp": now + expires_minutes * 60,
    }
    return jwt.encode(payload, _get_secret(), algorithm=_ALGORITHM)


#: Default lifetime for a ``channel_session`` JWT.  Mirrors the
#: ``service_account_session`` lifetime so a long-running anonymous
#: visitor session never hits mid-flight token expiry.  Revocation is
#: bounded by the session-row-existence check the middleware performs
#: (``_build_channel_session_context``), not by TTL — deleting the
#: ``sessions`` row immediately invalidates the token on the next call
#: across every API replica.
_CHANNEL_SESSION_TOKEN_MINUTES: int = 365 * 24 * 60


def create_channel_session_token(
    org_id: UUID,
    agent_id: str,
    session_id: UUID,
    channel: str,
    expires_minutes: int = _CHANNEL_SESSION_TOKEN_MINUTES,
) -> str:
    """Mint a worker→API JWT for an anonymous-channel session.

    Anonymous-channel sessions (today: the public-website widget) have
    no human user and no service account; the deployment itself is the
    authority.  The worker signs this token with
    ``SUROGATES_JWT_SECRET`` so :class:`HarnessAPIClient` can reach
    ``/v1/skills`` and other api-client-gated routes that previously
    degraded silently because no JWT could be minted.

    The token carries ``org_id``, ``agent_id``, ``session_id``, and
    ``channel`` so the middleware can verify the session row still
    exists with matching org / agent / channel before producing a
    ``TenantContext``.  Four independent invariants — any mismatch is
    401 — make the session row the authority and the JWT a pointer
    into it.  Revocation is therefore bounded by that existence check:
    deleting the ``sessions`` row immediately invalidates the token
    across every replica on the next call.

    The token's effective scope is narrow even within its 1-year
    lifetime: routes that require a user or service-account principal
    refuse :class:`PrincipalKind.CHANNEL` via
    ``surogates.api.routes._shared.require_not_channel_principal``.
    Specifically, ``/v1/memory``, mutating ``/v1/skills``, and
    ``/v1/agents`` are off-limits — this is the hard boundary that
    keeps a leaked website JWT from inheriting the ``user_id=None →
    shared/*`` semantics that :class:`TenantStorage` applies for
    service-account contexts.
    """
    now = int(time.time())
    payload: dict[str, Any] = {
        "sub": str(session_id),
        "type": "channel_session",
        "org_id": str(org_id),
        "agent_id": agent_id,
        "session_id": str(session_id),
        "channel": channel,
        "permissions": [],
        "iat": now,
        "exp": now + expires_minutes * 60,
    }
    return jwt.encode(payload, _get_secret(), algorithm=_ALGORITHM)


def create_sandbox_token(
    org_id: UUID,
    user_id: UUID,
    session_id: UUID,
    expires_minutes: int = 60,
    *,
    agent_id: str | None = None,
    is_service_account: bool = False,
) -> str:
    """Create a short-lived token for sandbox-to-MCP-proxy authentication.

    Carries the session context (org, user, session) so the MCP proxy
    can scope MCP server access and credential resolution.

    When ``agent_id`` is supplied it is embedded as a signed claim so the
    MCP proxy can bind the per-request ``?agent_id=`` to the token — a
    caller cannot then request a different agent's MCP servers than the
    one its token was minted for.  Omitted (``None``) keeps the token
    backward-compatible: the proxy falls back to the query param alone.

    ``is_service_account`` flags sessions whose ``user_id`` claim is
    actually a ``service_accounts.id`` (because ``sessions.user_id`` was
    NULL). The proxy uses this to skip foreign keys to ``users.id``
    (e.g. when writing to ``audit_log``).
    """
    now = int(time.time())
    payload: dict[str, Any] = {
        "sub": str(session_id),
        "org_id": str(org_id),
        "user_id": str(user_id),
        "session_id": str(session_id),
        "permissions": [],
        "type": "sandbox",
        "iat": now,
        "exp": now + expires_minutes * 60,
    }
    if agent_id is not None:
        payload["agent_id"] = agent_id
    if is_service_account:
        payload["is_service_account"] = True
    return jwt.encode(payload, _get_secret(), algorithm=_ALGORITHM)


def create_refresh_token(
    org_id: UUID,
    user_id: UUID,
    expires_days: int = 7,
    *,
    auth_time: int | None = None,
    family_id: UUID | None = None,
) -> str:
    """Create a long-lived refresh token (carries no permissions).

    *auth_time* is when the user signed in: the access tokens it refreshes carry it.
    *family_id* (``sid``) is the OAuth sign-in it belongs to: it refreshes only while that sign-in lasts.
    """
    now = int(time.time())
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "org_id": str(org_id),
        "user_id": str(user_id),
        "permissions": [],
        "type": "refresh",
        "iat": now,
        "exp": now + expires_days * 86400,
    }
    if auth_time is not None:
        payload["auth_time"] = auth_time
    if family_id is not None:
        payload["sid"] = str(family_id)
    return jwt.encode(payload, _get_secret(), algorithm=_ALGORITHM)


#: The ``purpose`` of a token that lets one user set their password.
SET_PASSWORD_PURPOSE = "set-password"


def create_set_password_token(
    *,
    org_id: UUID,
    user_id: UUID,
    digest: str,
    issued_at: int,
    expires_at: int,
    secret: str,
) -> str:
    """A token that lets *user_id* set their password through *org_id*, and does nothing else.

    *digest* stands for the user's password as it was when the token was
    minted; the reader compares it with the password's digest now.  The
    token has a ``purpose`` claim and no ``type`` or ``permissions``
    claim: :func:`decode_token` refuses it, so it is never an access, a
    refresh or any other token.  *issued_at* and *expires_at* are epoch
    seconds.
    """
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "org_id": str(org_id),
        "purpose": SET_PASSWORD_PURPOSE,
        "pwd": digest,
        "iat": issued_at,
        "exp": expires_at,
    }
    return jwt.encode(payload, secret, algorithm=_ALGORITHM)


# ------------------------------------------------------------------
# Token validation
# ------------------------------------------------------------------


class InvalidTokenError(Exception):
    """Raised when a token cannot be decoded or has failed validation."""


def decode_token(token: str) -> dict[str, Any]:
    """Decode and validate a JWT, returning its payload.

    The returned dict contains at least: ``sub``, ``org_id``, ``user_id``,
    ``permissions``, ``exp``, ``iat``, ``type``.

    Raises ``InvalidTokenError`` on any validation failure (bad signature,
    expired, missing claims, etc.).
    """
    try:
        payload: dict[str, Any] = jwt.decode(
            token,
            _get_secret(),
            algorithms=[_ALGORITHM],
            options={"require_exp": True, "require_iat": True, "require_sub": True},
        )
    except JWTError as exc:
        raise InvalidTokenError(str(exc)) from exc

    # Base claims every token carries.  ``user_id`` is mandatory on
    # human-identity tokens; service-account session tokens omit it
    # (they carry ``service_account_id`` + ``session_id`` instead).
    required_claims = ("org_id", "permissions", "type")
    missing = [c for c in required_claims if c not in payload]
    if missing:
        raise InvalidTokenError(f"Token is missing required claims: {missing}")

    token_type = payload["type"]
    if token_type not in (
        "access",
        "refresh",
        "sandbox",
        "service_account_session",
        "channel_session",
    ):
        raise InvalidTokenError(f"Unknown token type: {token_type!r}")

    if token_type == "service_account_session":
        for claim in ("service_account_id", "session_id"):
            if claim not in payload:
                raise InvalidTokenError(
                    f"service_account_session token is missing claim: {claim!r}"
                )
    elif token_type == "channel_session":
        for claim in ("agent_id", "session_id", "channel"):
            if claim not in payload:
                raise InvalidTokenError(
                    f"channel_session token is missing claim: {claim!r}"
                )
    else:
        if "user_id" not in payload:
            raise InvalidTokenError(
                f"{token_type} token is missing required claim: 'user_id'"
            )

    return payload


def decode_set_password_token(token: str, *, secret: str, now: int) -> dict[str, Any]:
    """The claims of a set-password token signed with *secret* that has not lapsed by *now* (epoch seconds).

    Raises ``InvalidTokenError`` for any other token, an access or a
    refresh token among them.  The error's text never quotes the token.
    The expiry is checked against *now*, not the clock.
    """
    try:
        payload: dict[str, Any] = jwt.decode(
            token,
            secret,
            algorithms=[_ALGORITHM],
            # ``require_exp`` would check the expiry against the clock: it is checked below, against *now*.
            options={"verify_exp": False, "require_iat": True, "require_sub": True},
        )
    except JWTError as exc:
        raise InvalidTokenError(str(exc)) from exc
    if payload.get("purpose") != SET_PASSWORD_PURPOSE or "type" in payload or "permissions" in payload:
        raise InvalidTokenError("Not a set-password token.")
    if not all(isinstance(payload.get(claim), str) for claim in ("sub", "org_id", "pwd")):
        raise InvalidTokenError("Set-password token is missing a claim.")
    expires_at = payload.get("exp")
    if not isinstance(expires_at, int) or isinstance(expires_at, bool) or expires_at <= now:
        raise InvalidTokenError("Set-password token has lapsed.")
    return payload
