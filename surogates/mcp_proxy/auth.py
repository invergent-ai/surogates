"""Sandbox authentication for the MCP proxy.

Validates the sandbox JWT that the worker mints when provisioning a
sandbox pod.  Extracts org_id, user_id, and session_id from the token
claims so the proxy can scope MCP server access and credential
resolution.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from uuid import UUID

from fastapi import HTTPException, Request, status

from surogates.tenant.auth.jwt import InvalidTokenError, decode_token

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ProxyAuthContext:
    """Identity extracted from a validated sandbox token."""

    org_id: UUID
    user_id: UUID
    session_id: UUID


async def get_proxy_auth(request: Request) -> ProxyAuthContext:
    """FastAPI dependency that validates the sandbox JWT.

    Expects ``Authorization: Bearer <token>`` with ``type: "sandbox"``.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Authorization header.",
        )

    token = auth_header[7:]
    try:
        payload = decode_token(token)
    except InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {exc}",
        ) from exc

    if payload.get("type") != "sandbox":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Expected a sandbox token.",
        )

    session_id_str = payload.get("session_id")
    if not session_id_str:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token missing session_id claim.",
        )

    return ProxyAuthContext(
        org_id=UUID(payload["org_id"]),
        user_id=UUID(payload["user_id"]),
        session_id=UUID(session_id_str),
    )
