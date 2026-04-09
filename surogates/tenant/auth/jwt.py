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

    # Validate that all mandatory custom claims are present.
    required_claims = ("org_id", "user_id", "permissions", "type")
    missing = [c for c in required_claims if c not in payload]
    if missing:
        raise InvalidTokenError(f"Token is missing required claims: {missing}")

    if payload["type"] not in ("access", "refresh"):
        raise InvalidTokenError(
            f"Unknown token type: {payload['type']!r}"
        )

    return payload
