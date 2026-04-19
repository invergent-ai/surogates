"""JWT token issuance and validation.

The platform issues its own HS256 JWTs after the pluggable auth provider
has verified the user's credentials.  Tokens carry the ``org_id``,
``user_id``, granted ``permissions``, and a ``type`` discriminator
(``"access"`` vs ``"refresh"``).
"""

from __future__ import annotations

import os
import time
from typing import Any
from uuid import UUID

from jose import JWTError, jwt

__all__ = [
    "create_access_token",
    "create_refresh_token",
    "create_sandbox_token",
    "create_service_account_session_token",
    "decode_token",
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


# ------------------------------------------------------------------
# Token creation
# ------------------------------------------------------------------


def create_access_token(
    org_id: UUID,
    user_id: UUID,
    permissions: set[str],
    expires_minutes: int = 30,
) -> str:
    """Create a short-lived access token."""
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


def create_sandbox_token(
    org_id: UUID,
    user_id: UUID,
    session_id: UUID,
    expires_minutes: int = 60,
) -> str:
    """Create a short-lived token for sandbox-to-MCP-proxy authentication.

    Carries the session context (org, user, session) so the MCP proxy
    can scope MCP server access and credential resolution.
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
    return jwt.encode(payload, _get_secret(), algorithm=_ALGORITHM)


def create_refresh_token(
    org_id: UUID,
    user_id: UUID,
    expires_days: int = 7,
) -> str:
    """Create a long-lived refresh token (carries no permissions)."""
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
    return jwt.encode(payload, _get_secret(), algorithm=_ALGORITHM)


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
        "access", "refresh", "sandbox", "service_account_session",
    ):
        raise InvalidTokenError(f"Unknown token type: {token_type!r}")

    if token_type == "service_account_session":
        for claim in ("service_account_id", "session_id"):
            if claim not in payload:
                raise InvalidTokenError(
                    f"service_account_session token is missing claim: {claim!r}"
                )
    else:
        if "user_id" not in payload:
            raise InvalidTokenError(
                f"{token_type} token is missing required claim: 'user_id'"
            )

    return payload
