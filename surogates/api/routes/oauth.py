"""Surogate Desktop's sign-in: OAuth 2.0 for a native app (RFC 8252).

The desktop opens the system browser on the web client's ``/oauth/authorize``
page with its PKCE challenge (RFC 7636, S256 only) and a loopback
``redirect_uri``.  The user signs in there as they always do, then allows
the desktop; the page posts that decision here and sends the browser to the
loopback with a one-time code.  The desktop exchanges the code and its
verifier at ``/token`` for an access token, which carries the browser
sign-in's ``auth_time`` and its family (``sid``), and a refresh token, which
rotates on every use.  A sign-in lasts 30 days from the browser sign-in, and
ends with the computer it added or restored (``surogates.devices.store``).
``/revoke`` ends the sign-in (RFC 7009).  Only the user's own sign-in in a
browser allows the desktop: a token issued to the desktop cannot.

The desktop's window loads the web client, which keeps a session of its own:
``/web-code`` gives the desktop a one-time code for it, and the page
exchanges that at ``/web-session``.  So the user signs in once.

Mounted under ``/v1/auth/``, which the auth middleware leaves to each route.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
from typing import Annotated, Literal
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select

from surogates.api.routes._shared import require_recent_sign_in
from surogates.api.routes.auth import USER_PERMISSIONS, TokenResponse
from surogates.db.models import User
from surogates.runtime import AgentRuntimeContext, agent_runtime_context_dep
from surogates.tenant.auth.jwt import create_access_token, create_refresh_token
from surogates.tenant.auth.middleware import get_current_tenant
from surogates.tenant.auth.oauth import OAuthTokens, RefreshGrant
from surogates.tenant.context import TenantContext

router = APIRouter(prefix="/auth/oauth")

AgentRuntime = Annotated[AgentRuntimeContext, Depends(agent_runtime_context_dep)]
Tenant = Annotated[TenantContext, Depends(get_current_tenant)]

#: The public clients this server signs in, by client_id: the name the consent page shows.
DESKTOP_CLIENT = "surogate-desktop"
CLIENTS = {DESKTOP_CLIENT: "Surogate Desktop"}

#: A loopback redirect on any port the desktop listened on (RFC 8252, section 7.3), on one fixed path.
_LOOPBACK = re.compile(r"^http://127\.0\.0\.1:(?P<port>[0-9]{4,5})/callback$")
_STATE = re.compile(r"^[A-Za-z0-9._~-]{16,256}$")
_CHALLENGE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_VERIFIER = re.compile(r"^[A-Za-z0-9._~-]{43,128}$")
CODE_TTL_S = 60
ACCESS_MINUTES = 30


def code_key(code: str) -> str:
    """The Redis key of an authorization code: its digest, so a dump of Redis holds no code."""
    return "surogates:oauth:code:" + hashlib.sha256(code.encode()).hexdigest()


def _web_code_key(code: str) -> str:
    return "surogates:oauth:web:" + hashlib.sha256(code.encode()).hexdigest()


def _s256(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def _loopback(redirect_uri: str) -> bool:
    found = _LOOPBACK.fullmatch(redirect_uri)
    return found is not None and 1024 <= int(found["port"]) <= 65535


def _error(error: str) -> JSONResponse:
    return JSONResponse({"error": error}, status_code=status.HTTP_400_BAD_REQUEST, headers={"Cache-Control": "no-store"})


def _user_of(tenant: TenantContext, ctx: AgentRuntimeContext) -> UUID:
    if tenant.user_id is None or str(tenant.org_id) != ctx.org_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Sign in to this agent as one of its users.")
    return tenant.user_id


async def _user_exists(request: Request, org_id: UUID, user_id: UUID) -> bool:
    async with request.app.state.session_factory() as db:
        found = await db.scalar(select(User.id).where(User.id == user_id, User.org_id == org_id))
    return found is not None


class AuthorizeRequest(BaseModel):
    response_type: Literal["code"]
    client_id: str
    redirect_uri: str
    state: str
    code_challenge: str
    code_challenge_method: Literal["S256"]
    decision: Literal["allow", "deny"]


class AuthorizeResponse(BaseModel):
    redirect_to: str


@router.post("/authorize", response_model=AuthorizeResponse)
async def authorize(body: AuthorizeRequest, request: Request, ctx: AgentRuntime, tenant: Tenant) -> AuthorizeResponse:
    """The signed-in user's answer on the consent page: where the browser goes next."""
    # A request the desktop would not make goes nowhere: the browser is not sent to an address it names.
    if body.client_id not in CLIENTS or not _loopback(body.redirect_uri):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This sign-in request did not come from Surogate Desktop.")
    if not _STATE.fullmatch(body.state) or not _CHALLENGE.fullmatch(body.code_challenge):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This sign-in request is malformed.")
    user_id = _user_of(tenant, ctx)
    # Only the user's own sign-in in a browser allows the desktop: never a token of the desktop's
    # sign-in, whether the desktop holds it or its window does.
    if tenant.client_id is not None or tenant.oauth_family_id is not None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Allow Surogate Desktop from your browser's own sign-in.")
    if body.decision == "deny":
        return AuthorizeResponse(redirect_to=f"{body.redirect_uri}?{urlencode({'error': 'access_denied', 'state': body.state})}")
    require_recent_sign_in(tenant)
    code = secrets.token_urlsafe(32)
    record = {
        "client_id": body.client_id, "redirect_uri": body.redirect_uri, "challenge": body.code_challenge,
        "org_id": str(tenant.org_id), "user_id": str(user_id), "auth_time": tenant.auth_time, "agent_id": ctx.agent_id,
    }
    await request.app.state.redis.set(code_key(code), json.dumps(record), ex=CODE_TTL_S, nx=True)
    return AuthorizeResponse(redirect_to=f"{body.redirect_uri}?{urlencode({'code': code, 'state': body.state})}")


def _issued(grant: RefreshGrant, client_id: str) -> JSONResponse:
    access_token = create_access_token(
        grant.org_id, grant.user_id, USER_PERMISSIONS, ACCESS_MINUTES,
        auth_time=grant.auth_time, client_id=client_id, family_id=grant.family_id,
    )
    return JSONResponse(
        {
            "access_token": access_token, "token_type": "Bearer", "expires_in": ACCESS_MINUTES * 60,
            "refresh_token": grant.refresh_token, "auth_time": grant.auth_time,
        },
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


@router.post("/token")
async def token(
    request: Request,
    ctx: AgentRuntime,
    grant_type: Annotated[str, Form()],
    client_id: Annotated[str, Form()],
    code: Annotated[str | None, Form()] = None,
    redirect_uri: Annotated[str | None, Form()] = None,
    code_verifier: Annotated[str | None, Form()] = None,
    refresh_token: Annotated[str | None, Form()] = None,
) -> JSONResponse:
    """The token endpoint (RFC 6749, section 3.2), for a code and its verifier, or a refresh token."""
    if client_id not in CLIENTS:
        return _error("invalid_client")
    tokens = OAuthTokens(request.app.state.session_factory)
    if grant_type == "authorization_code":
        # Any attempt spends the code, so a wrong verifier cannot be followed by a right one.
        raw = await request.app.state.redis.getdel(code_key(code)) if code else None
        if raw is None:
            return _error("invalid_grant")
        record = json.loads(raw)
        matches = (
            record["client_id"] == client_id
            and record["redirect_uri"] == redirect_uri
            and record["agent_id"] == ctx.agent_id
            and code_verifier is not None
            and _VERIFIER.fullmatch(code_verifier) is not None
            and hmac.compare_digest(_s256(code_verifier), record["challenge"])
        )
        org_id, user_id = UUID(record["org_id"]), UUID(record["user_id"])
        if not matches or not await _user_exists(request, org_id, user_id):
            return _error("invalid_grant")
        grant = await tokens.issue(
            org_id=org_id, user_id=user_id, agent_id=ctx.agent_id, client_id=client_id, auth_time=record["auth_time"],
        )
        return _issued(grant, client_id)
    if grant_type == "refresh_token":
        grant = await tokens.rotate(refresh_token, client_id=client_id, agent_id=ctx.agent_id) if refresh_token else None
        # Spent, revoked, past its 30 days, or its computer revoked: the user signs in again.
        if grant is None or not await _user_exists(request, grant.org_id, grant.user_id):
            return _error("invalid_grant")
        return _issued(grant, client_id)
    return _error("unsupported_grant_type")


@router.post("/revoke")
async def revoke(
    request: Request, token: Annotated[str, Form()], client_id: Annotated[str, Form()],
) -> Response:
    """End the sign-in a refresh token belongs to (RFC 7009): signing out. Unknown tokens are no error."""
    await OAuthTokens(request.app.state.session_factory).revoke(token, client_id=client_id)
    return Response(status_code=status.HTTP_200_OK)


class WebCode(BaseModel):
    code: str


@router.post("/web-code", response_model=WebCode)
async def web_code(request: Request, ctx: AgentRuntime, tenant: Tenant) -> WebCode:
    """A one-time code for the desktop's window: its web client exchanges it for a session of its own."""
    user_id = _user_of(tenant, ctx)
    family_id = tenant.oauth_family_id
    if tenant.client_id != DESKTOP_CLIENT or tenant.auth_time is None or family_id is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only Surogate Desktop's own sign-in gives its window a session.")
    # An access token outlives its sign-in by minutes: it mints nothing that would outlive it more.
    if not await OAuthTokens(request.app.state.session_factory).live(family_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This sign-in has ended. Sign in again from Surogate Desktop.")
    code = secrets.token_urlsafe(32)
    record = {
        "org_id": str(tenant.org_id), "user_id": str(user_id), "auth_time": tenant.auth_time, "agent_id": ctx.agent_id,
        "family_id": str(family_id),
    }
    await request.app.state.redis.set(_web_code_key(code), json.dumps(record), ex=CODE_TTL_S, nx=True)
    return WebCode(code=code)


@router.post("/web-session", response_model=TokenResponse)
async def web_session(body: WebCode, request: Request, ctx: AgentRuntime) -> TokenResponse:
    """The web client's session in the desktop's window, for a code from ``/web-code``. Used once."""
    raw = await request.app.state.redis.getdel(_web_code_key(body.code))
    record = json.loads(raw) if raw is not None else None
    if record is None or record["agent_id"] != ctx.agent_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This sign-in code is not valid. Sign in again from Surogate.")
    org_id, user_id, family_id = UUID(record["org_id"]), UUID(record["user_id"]), UUID(record["family_id"])
    if not await _user_exists(request, org_id, user_id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This sign-in code is not valid. Sign in again from Surogate.")
    # The desktop's sign-in (sid), but not its client: the window's session ends with that sign-in.
    signed_in = {"auth_time": record["auth_time"], "family_id": family_id}
    return TokenResponse(
        access_token=create_access_token(org_id, user_id, USER_PERMISSIONS, **signed_in),
        refresh_token=create_refresh_token(org_id, user_id, **signed_in),
    )
