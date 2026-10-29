"""A rehearsal world's Microsoft Teams, called by the runtime's real Teams adapter.

Mounted at ``/m``, it is the three Microsoft services the adapter talks to for
a rehearsal App ID: Entra's token endpoint (``/m/login``), the Bot Framework
signing keys (``/m/keys``) and the Bot Connector (``/m/v3/conversations``,
the ``serviceUrl`` of every activity the world sends).  The world signs its
activities with its own key, which the adapter trusts for rehearsal App IDs
only.
"""

from __future__ import annotations

import functools
import secrets
import time
from datetime import datetime, timezone
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse
from jose import jwk, jwt

from surogates.channels.platforms.teams import BOT_FRAMEWORK_ISSUER
from surogates.rehearsal import store

router = APIRouter(prefix="/m")

KID = "rehearsal"
_THREAD_MARK = ";messageid="


@functools.cache
def _private_pem() -> bytes:
    # One key per process: the adapter reads the keys on every inbound
    # activity, so a restart only invalidates tokens in flight.
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())


def service_url(settings) -> str:
    return f"{settings.rehearsal.url.rstrip('/')}/m"


def sign(app_id: str, service: str) -> str:
    """A Bot Framework token for one inbound activity to *app_id*."""
    now = int(time.time())
    return jwt.encode(
        {"iss": BOT_FRAMEWORK_ISSUER, "aud": app_id, "serviceurl": service,
         "iat": now, "nbf": now, "exp": now + 300},
        _private_pem().decode(), algorithm="RS256", headers={"kid": KID},
    )


def conversation_of(address: str) -> str:
    """A member's personal chat with the bot: ``29:999…`` talks in ``a:999…``."""
    return "a:" + address.removeprefix("29:")


def tenant_of(run) -> str:
    return run.phone_number_id


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


@router.get("/keys")
async def keys() -> dict[str, Any]:
    public = jwk.construct(_private_pem().decode(), "RS256").public_key().to_dict()
    return {"keys": [{**public, "kid": KID, "use": "sig"}]}


@router.post("/login/{tenant}/oauth2/v2.0/token")
async def token(tenant: str, request: Request, client_id: str = Form(""),
                client_secret: str = Form("")):
    run = await store.run_by_number(request.app.state.session_factory, client_id)
    if (run is None or run.channel != "teams" or run.status != "running"
            or tenant != tenant_of(run) or not secrets.compare_digest(client_secret, run.app_secret)):
        return JSONResponse({"error": "invalid_client",
                             "error_description": "AADSTS7000215: Invalid client secret provided."},
                            status_code=401)
    return {"token_type": "Bearer", "expires_in": 3600, "ext_expires_in": 3600,
            "access_token": run.access_token}


async def _caller(request: Request, conversation: str):
    """The run and person a connector call is for, or ``None``."""
    sf = request.app.state.session_factory
    bearer = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    run = await store.run_by_token(sf, bearer) if bearer else None
    if run is None or run.channel != "teams" or run.status != "running":
        return None, None
    base = conversation.partition(_THREAD_MARK)[0]
    for person in await store.people(sf, run.id):
        if conversation_of(person.address) == base:
            return run, person
    return run, None


@router.post("/v3/conversations/{conversation}/activities")
async def post_activity(conversation: str, request: Request):
    run, person = await _caller(request, conversation)
    if run is None:
        return _error(401, "Unauthorized", "Authorization has been denied for this request.")
    if person is None:
        return _error(404, "ConversationNotFound", "Conversation not found.")
    sf = request.app.state.session_factory
    activity: dict[str, Any] = await request.json()
    if activity.get("type") == "typing":
        await store.set_person_typing(sf, person.id, True)
        return {}
    activity_id = f"1{int(time.time() * 1000)}{secrets.randbelow(1000):03d}"
    body = {**activity, "id": activity_id, "from": {"id": f"28:{run.phone_number_id}"},
            "timestamp": datetime.now(timezone.utc).isoformat()}
    await store.log_message(sf, run_id=run.id, person_id=person.id, direction="out",
                            kind="message", body=body, wamid=activity_id, status="sent")
    await store.set_person_typing(sf, person.id, False)
    return {"id": activity_id}


@router.put("/v3/conversations/{conversation}/activities/{activity_id}")
async def update_activity(conversation: str, activity_id: str, request: Request):
    run, person = await _caller(request, conversation)
    if run is None:
        return _error(401, "Unauthorized", "Authorization has been denied for this request.")
    sf = request.app.state.session_factory
    sent = await store.message(sf, run.id, activity_id)
    if person is None or sent is None or sent.direction != "out":
        return _error(404, "ActivityNotFound", "Activity not found.")
    activity: dict[str, Any] = await request.json()
    edited = {**sent.body, **activity, "id": activity_id,
              "edited": datetime.now(timezone.utc).isoformat()}
    await store.replace_body(sf, run.id, activity_id, edited)
    return {"id": activity_id}
