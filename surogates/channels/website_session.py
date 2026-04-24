"""Session-cookie JWT and CSRF helpers for the website channel.

A website session is represented by a single signed JWT stored in an
``HttpOnly``/``Secure``/``SameSite=None`` cookie.  The JWT claims bind
the session to its originating website agent, the origin it was
bootstrapped from, and a per-session CSRF token.  Double-submit CSRF:
state-changing requests must present the same token in the
``X-CSRF-Token`` header as the cookie JWT carries, so an attacker who
cannot read the cookie also cannot forge a matching header from a
different origin.

The JWT is signed with the platform's existing HS256 secret via
:mod:`surogates.tenant.auth.jwt` so operators only manage one signing
key.  The ``type`` claim (``website_session``) keeps these tokens
cleanly separated from access/refresh/sandbox/service-account session
tokens at the decode boundary.
"""

from __future__ import annotations

import hmac
import logging
import secrets
import time
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from jose import JWTError, jwt

from surogates.tenant.auth.jwt import InvalidTokenError, _get_secret  # reuse secret

logger = logging.getLogger(__name__)

__all__ = [
    "COOKIE_NAME",
    "CSRF_HEADER_NAME",
    "DEFAULT_SESSION_TTL_SECONDS",
    "WebsiteSessionClaims",
    "create_website_session_token",
    "decode_website_session_token",
    "generate_csrf_token",
    "verify_csrf_token",
]


_ALGORITHM = "HS256"

COOKIE_NAME = "surg_ws"
CSRF_HEADER_NAME = "X-CSRF-Token"
# One hour is long enough for a typical support conversation without
# inflating the damage window if a cookie is somehow exfiltrated.  The
# client can silently re-bootstrap once the cookie expires, since the
# publishable key stays valid.
DEFAULT_SESSION_TTL_SECONDS = 60 * 60
# Raw bytes behind the CSRF token.  256 bits of entropy keeps the
# double-submit compare resistant to guessing even with aggressive
# parallelism.
_CSRF_BYTES = 32


def generate_csrf_token() -> str:
    """Return a fresh, high-entropy CSRF token as a URL-safe string."""
    return secrets.token_urlsafe(_CSRF_BYTES)


def verify_csrf_token(cookie_token: str | None, header_token: str | None) -> bool:
    """Constant-time compare the cookie CSRF token with the header token.

    Returns False when either side is missing -- a CSRF attacker cannot
    read the cookie across origins, so a request that omits the header
    is definitionally forged.  ``hmac.compare_digest`` avoids timing
    side channels on the comparison itself.
    """
    if not cookie_token or not header_token:
        return False
    return hmac.compare_digest(cookie_token, header_token)


@dataclass(frozen=True)
class WebsiteSessionClaims:
    """Decoded claim set of a website-session JWT.

    Every route on ``/v1/website/*`` that accepts the cookie works from
    this frozen view; the surrounding request does not need to touch
    the JWT library itself.
    """

    session_id: UUID
    org_id: UUID
    agent_id: UUID
    origin: str
    csrf_token: str
    issued_at: int
    expires_at: int


def create_website_session_token(
    *,
    session_id: UUID,
    org_id: UUID,
    agent_id: UUID,
    origin: str,
    csrf_token: str,
    expires_seconds: int = DEFAULT_SESSION_TTL_SECONDS,
) -> str:
    """Sign a JWT binding a website session to its agent + origin + CSRF.

    ``origin`` is baked into the claims on issue and re-checked against
    the request's ``Origin`` header on every subsequent call.  A cookie
    stolen from one website embed and replayed against another embed
    in the same org therefore still fails: the claims would name the
    original origin, not the attacker's.
    """
    now = int(time.time())
    payload: dict[str, Any] = {
        "sub": str(session_id),
        "type": "website_session",
        "session_id": str(session_id),
        "org_id": str(org_id),
        "agent_id": str(agent_id),
        "origin": origin,
        "csrf": csrf_token,
        "iat": now,
        "exp": now + expires_seconds,
    }
    return jwt.encode(payload, _get_secret(), algorithm=_ALGORITHM)


def decode_website_session_token(token: str) -> WebsiteSessionClaims:
    """Decode and validate a website-session JWT.

    Raises :class:`InvalidTokenError` on any failure -- signature,
    expiry, missing claims, or wrong token type.  The explicit type
    check prevents an access/refresh JWT from being presented as a
    website-session cookie.
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

    if payload.get("type") != "website_session":
        raise InvalidTokenError(
            f"Expected website_session token, got {payload.get('type')!r}"
        )

    required = ("session_id", "org_id", "agent_id", "origin", "csrf")
    missing = [c for c in required if c not in payload]
    if missing:
        raise InvalidTokenError(
            f"Website-session token missing required claims: {missing}"
        )

    try:
        return WebsiteSessionClaims(
            session_id=UUID(payload["session_id"]),
            org_id=UUID(payload["org_id"]),
            agent_id=UUID(payload["agent_id"]),
            origin=str(payload["origin"]),
            csrf_token=str(payload["csrf"]),
            issued_at=int(payload["iat"]),
            expires_at=int(payload["exp"]),
        )
    except (ValueError, TypeError) as exc:
        raise InvalidTokenError(f"Website-session token has malformed claims: {exc}") from exc
