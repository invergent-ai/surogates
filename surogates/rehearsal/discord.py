"""A rehearsal world's Discord, held open by the runtime's real Gateway client.

Mounted at ``/d``: the REST API the adapter calls for a rehearsal bot token
(``/d/api/v10/…``), and the Gateway its ``GET /gateway/bot`` points to
(``/d/gateway``), where the channels service keeps a websocket per running
rehearsal bot and the person's messages arrive as ``MESSAGE_CREATE``.  A
message written before the bot has connected waits for its READY.
"""

from __future__ import annotations

import secrets
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response

from surogates.rehearsal import store

router = APIRouter(prefix="/d")

#: Discord's own interval; the client beats on it and reconnects on a missed ACK.
HEARTBEAT_MS = 41250
#: Discord's close code for a token it does not know: the client stops for good.
AUTHENTICATION_FAILED = 4004


def dm_of(address: str) -> str:
    """A member's DM with the bot: user ``999…`` talks in channel ``998…``."""
    return "998" + address[3:]


def new_snowflake() -> str:
    # 20 digits from 999, sorted by time: never a real snowflake.
    return f"999{int(time.time() * 1000):013d}{secrets.randbelow(10000):04d}"


def bot_user(run) -> dict[str, Any]:
    return {"id": run.phone_number_id, "username": "agent", "global_name": None,
            "discriminator": "0", "bot": True}


def author(person) -> dict[str, Any]:
    return {"id": person.address, "username": person.name.lower(), "global_name": person.name,
            "discriminator": "0"}


class Gateways:
    """The open Gateway connection of each running rehearsal bot."""

    def __init__(self) -> None:
        self._sockets: dict[uuid.UUID, WebSocket] = {}
        self._seq: dict[uuid.UUID, int] = {}
        self._waiting: dict[uuid.UUID, list[dict[str, Any]]] = {}

    async def _send(self, run_id: uuid.UUID, event: str, data: dict[str, Any]) -> None:
        self._seq[run_id] = self._seq.get(run_id, 0) + 1
        await self._sockets[run_id].send_json(
            {"op": 0, "s": self._seq[run_id], "t": event, "d": data})

    async def dispatch(self, run_id: uuid.UUID, message: dict[str, Any]) -> bool:
        """``MESSAGE_CREATE`` *message*; ``False`` when it waits for the bot to connect."""
        if run_id not in self._sockets:
            self._waiting.setdefault(run_id, []).append(message)
            return False
        await self._send(run_id, "MESSAGE_CREATE", message)
        return True

    async def ready(self, run_id: uuid.UUID, ws: WebSocket, data: dict[str, Any]) -> list[str]:
        """Hold *ws* as the run's link, say READY and hand over what waited; their ids."""
        self._sockets[run_id], self._seq[run_id] = ws, 0
        await self._send(run_id, "READY", data)
        waiting = self._waiting.pop(run_id, [])
        for message in waiting:
            await self._send(run_id, "MESSAGE_CREATE", message)
        return [m["id"] for m in waiting]

    def drop(self, run_id: uuid.UUID, ws: WebSocket) -> None:
        if self._sockets.get(run_id) is ws:
            del self._sockets[run_id]

    async def close(self, run_id: uuid.UUID) -> None:
        self._waiting.pop(run_id, None)
        ws = self._sockets.pop(run_id, None)
        if ws is not None:
            await ws.close(AUTHENTICATION_FAILED)


GATEWAYS = Gateways()


def gateway_url(settings) -> str:
    base = settings.rehearsal.url.rstrip("/").replace("https://", "wss://").replace("http://", "ws://")
    return f"{base}/d/gateway"


async def _running(sf, token: str):
    run = await store.run_by_token(sf, token) if token else None
    if run is None or run.channel != "discord" or run.status != "running":
        return None
    return run


@router.websocket("/gateway/")
@router.websocket("/gateway")
async def gateway(ws: WebSocket):
    sf = ws.app.state.session_factory
    await ws.accept()
    await ws.send_json({"op": 10, "d": {"heartbeat_interval": HEARTBEAT_MS}})
    run = None
    try:
        while True:
            frame = await ws.receive_json()
            op, d = frame.get("op"), frame.get("d") or {}
            if op == 1:
                await ws.send_json({"op": 11})
            elif op in (2, 6):
                run = await _running(sf, str(d.get("token") or ""))
                if run is None:
                    await ws.close(AUTHENTICATION_FAILED)
                    return
                delivered = await GATEWAYS.ready(run.id, ws, {
                    "v": 10, "user": bot_user(run), "guilds": [],
                    "session_id": secrets.token_hex(16),
                    "resume_gateway_url": gateway_url(ws.app.state.settings),
                    "application": {"id": run.phone_number_id, "flags": 0},
                })
                for message_id in delivered:
                    await store.set_status(sf, run.id, message_id, "sent")
    except WebSocketDisconnect:
        pass
    finally:
        if run is not None:
            GATEWAYS.drop(run.id, ws)


def _error(status: int, code: int, message: str) -> JSONResponse:
    return JSONResponse({"code": code, "message": message}, status_code=status)


async def _person(sf, run, channel_id: str):
    for person in await store.people(sf, run.id):
        if dm_of(person.address) == channel_id:
            return person
    return None


@router.api_route("/api/v10/{path:path}", methods=["GET", "POST", "PATCH", "DELETE"])
async def rest(path: str, request: Request):
    sf = request.app.state.session_factory
    run = await _running(sf, request.headers.get("authorization", "").removeprefix("Bot ").strip())
    if run is None:
        return _error(401, 0, "401: Unauthorized")
    parts, method = path.strip("/").split("/"), request.method

    if parts == ["gateway", "bot"]:
        return {"url": gateway_url(request.app.state.settings), "shards": 1,
                "session_start_limit": {"total": 1000, "remaining": 1000, "reset_after": 0,
                                        "max_concurrency": 1}}
    if parts == ["users", "@me"]:
        return bot_user(run)
    if parts == ["users", "@me", "channels"] and method == "POST":
        recipient = str((await request.json()).get("recipient_id") or "")
        person = await store.person_by_address(sf, run.id, recipient)
        if person is None:
            return _error(400, 50007, "Cannot send messages to this user")
        return {"id": dm_of(person.address), "type": 1, "recipients": [author(person)]}

    if len(parts) < 2 or parts[0] != "channels":
        return _error(404, 0, "404: Not Found")
    person = await _person(sf, run, parts[1])
    if person is None:
        return _error(404, 10003, "Unknown Channel")
    rest_of = parts[2:]

    if rest_of == ["typing"] and method == "POST":
        await store.set_person_typing(sf, person.id, True)
        return Response(status_code=204)
    if rest_of == ["messages"] and method == "POST":
        if "json" not in request.headers.get("content-type", ""):
            return _error(400, 50035, "Invalid Form Body")
        body = await request.json()
        message_id = new_snowflake()
        message = {
            "id": message_id, "type": 0, "channel_id": parts[1], "author": bot_user(run),
            "content": body.get("content") or "", "embeds": body.get("embeds") or [],
            "components": body.get("components") or [], "attachments": [], "mentions": [],
            "timestamp": datetime.now(timezone.utc).isoformat(), "edited_timestamp": None,
        }
        if body.get("message_reference"):
            message["message_reference"] = body["message_reference"]
        await store.log_message(sf, run_id=run.id, person_id=person.id, direction="out",
                                kind="message", body=message, wamid=message_id, status="sent")
        # A message clears Discord's typing indicator.
        await store.set_person_typing(sf, person.id, False)
        return message
    if len(rest_of) == 2 and rest_of[0] == "messages":
        sent = await store.message(sf, run.id, rest_of[1])
        if sent is None or sent.direction != "out":
            return _error(404, 10008, "Unknown Message")
        if method == "DELETE":
            await store.delete_message(sf, run.id, rest_of[1])
            return Response(status_code=204)
        if method == "PATCH":
            body = await request.json()
            edited = {**sent.body, **{k: body[k] for k in ("content", "embeds", "components")
                                      if k in body},
                      "edited_timestamp": datetime.now(timezone.utc).isoformat()}
            await store.replace_body(sf, run.id, rest_of[1], edited)
            return edited
    return _error(404, 0, "404: Not Found")
