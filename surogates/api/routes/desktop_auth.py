"""Spike only: desktop sign-in handoff (loopback redirect + S256 PKCE)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from surogates.api.routes.auth import TokenResponse
from surogates.tenant.auth.jwt import create_access_token, create_refresh_token
from surogates.tenant.auth.middleware import get_current_tenant
from surogates.tenant.context import TenantContext

router = APIRouter()

_CODE_TTL_SECONDS = 60
_CHALLENGE_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_STATE_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")


class CodeRequest(BaseModel):
    state: str
    code_challenge: str
    port: int


class CodeResponse(BaseModel):
    code: str
    redirect_uri: str


class TokenRequest(BaseModel):
    code: str
    code_verifier: str
    state: str
    redirect_uri: str


def _origin(request: Request) -> str:
    return f"{request.url.scheme}://{request.headers.get('host', '')}"


def _key(code: str) -> str:
    return "surogates:desktop_code:" + hashlib.sha256(code.encode()).hexdigest()


def _bad() -> HTTPException:
    return HTTPException(status_code=400, detail="invalid_grant")


@router.post("/auth/desktop/code", response_model=CodeResponse)
async def desktop_code(
    body: CodeRequest, request: Request, tenant: TenantContext = Depends(get_current_tenant),
) -> CodeResponse:
    if not 1024 <= body.port <= 65535 or not _CHALLENGE_RE.match(body.code_challenge) or not _STATE_RE.match(body.state):
        raise HTTPException(status_code=400, detail="invalid_request")
    redirect_uri = f"http://127.0.0.1:{body.port}/callback"
    code = secrets.token_urlsafe(32)
    record = {
        "challenge": body.code_challenge, "state": body.state, "origin": _origin(request),
        "redirect_uri": redirect_uri, "org_id": str(tenant.org_id), "user_id": str(tenant.user_id),
    }
    await request.app.state.redis.set(_key(code), json.dumps(record), ex=_CODE_TTL_SECONDS, nx=True)
    return CodeResponse(code=code, redirect_uri=redirect_uri)


@router.post("/auth/desktop/token", response_model=TokenResponse)
async def desktop_token(body: TokenRequest, request: Request) -> TokenResponse:
    raw = await request.app.state.redis.getdel(_key(body.code))  # any attempt consumes the code
    if raw is None:
        raise _bad()
    record = json.loads(raw)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(body.code_verifier.encode()).digest()).rstrip(b"=").decode()
    checks = [
        hmac.compare_digest(challenge, record["challenge"]),
        hmac.compare_digest(body.state, record["state"]),
        body.redirect_uri == record["redirect_uri"],
        _origin(request) == record["origin"],
    ]
    if not all(checks):
        raise _bad()
    org_id, user_id = UUID(record["org_id"]), UUID(record["user_id"])
    return TokenResponse(
        access_token=create_access_token(org_id=org_id, user_id=user_id, permissions={"sessions:read", "sessions:write", "tools:read"}),
        refresh_token=create_refresh_token(org_id=org_id, user_id=user_id),
    )
