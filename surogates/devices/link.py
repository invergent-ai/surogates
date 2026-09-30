"""The device link: one authenticated WebSocket per connected desktop device.

Protocol version 1, JSON text frames of at most MAX_FRAME_CHARS.

  app -> server   {"type": "hello", "protocols": [1]}   first frame, within HELLO_TIMEOUT_S
  server -> app   {"type": "welcome", "protocol": 1, "device_id", "org_id",
                   "agent_id", "user_id", "name", "heartbeat_s": 15}
  app -> server   {"type": "ping"}                      every heartbeat_s
  server -> app   {"type": "pong"}
  app -> server   {"type": "revoke"}                    the app removes this device

Every handshake is accepted and a refusal is a close code, so the app can tell
a refused token from a proxy's HTTP error:

  4400  protocol error, including an unsupported protocol or an oversized frame
  4401  token unknown or revoked, for another agent, or no agent here: the app stops
  4403  revoked, credentials rotated, or the device's user removed: the app stops
  4408  no frame for IDLE_TIMEOUT_S: the app reconnects
  4409  another connection of this device took over: this one ends
  1011  server trouble, as is any other close: the app reconnects with backoff
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect
from redis.asyncio.client import PubSub

from surogates.devices.presence import (
    HEARTBEAT_INTERVAL_S,
    PRESENCE_TTL_S,
    DevicePresence,
    new_holder,
)
from surogates.devices.store import DeviceRecord, DeviceStore
from surogates.runtime.resolver import resolve_agent_id_soft

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = 1
HELLO_TIMEOUT_S = 10.0
# A device that sends nothing for as long as its presence lasts is gone.
IDLE_TIMEOUT_S = float(PRESENCE_TTL_S)
# How often a connected device's row is read back (and ``last_seen_at``
# written): the fallback for a lost revocation or rotation message.
LAST_SEEN_INTERVAL_S = 60.0
# Pings faster than this are answered without touching Redis.
MIN_REFRESH_INTERVAL_S = 1.0
MAX_FRAME_CHARS = 64 * 1024

CLOSE_PROTOCOL = 4400
CLOSE_UNAUTHENTICATED = 4401
CLOSE_REVOKED = 4403
CLOSE_IDLE = 4408
CLOSE_SUPERSEDED = 4409


class _Close(Exception):
    """End the connection with this close code."""

    def __init__(self, code: int, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


async def serve_device_link(
    websocket: WebSocket, *, store: DeviceStore, presence: DevicePresence,
) -> None:
    await websocket.accept()
    try:
        device = await _authenticate(websocket, store)
    except Exception:
        logger.exception("device link authentication failed")
        await websocket.close(code=1011, reason="server error")
        return
    if device is None:
        await websocket.close(code=CLOSE_UNAUTHENTICATED, reason="unauthenticated")
        return

    holder = new_holder()
    pubsub: PubSub | None = None
    claimed = False
    try:
        await _hello(websocket)
        # Listen before claiming, so every message sent after the claim is seen.
        pubsub = await presence.subscribe(device.id)
        # Set before the claim: release is compare-and-delete, so it is safe
        # even when the claim fails between its SET and its PUBLISH.
        claimed = True
        await presence.claim(device.id, holder)
        # A revocation or rotation published before the subscription was missed.
        _check_current(await store.touch(device.id), device)
        await websocket.send_json({
            "type": "welcome",
            "protocol": PROTOCOL_VERSION,
            "device_id": str(device.id),
            "org_id": str(device.org_id),
            "agent_id": device.agent_id,
            "user_id": str(device.user_id),
            "name": device.name,
            "heartbeat_s": HEARTBEAT_INTERVAL_S,
        })
        await _serve(websocket, pubsub, device=device, holder=holder, store=store, presence=presence)
    except _Close as close:
        with contextlib.suppress(Exception):
            await websocket.close(code=close.code, reason=close.reason)
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("device link %s failed", device.id)
        with contextlib.suppress(Exception):
            await websocket.close(code=1011, reason="server error")
    finally:
        if claimed:
            with contextlib.suppress(Exception):
                await presence.release(device.id, holder)
        if pubsub is not None:
            with contextlib.suppress(Exception):
                await pubsub.aclose()


async def _authenticate(websocket: WebSocket, store: DeviceStore) -> DeviceRecord | None:
    scheme, _, token = (websocket.headers.get("authorization") or "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    device = await store.get_by_token(token.strip())
    if device is None:
        return None
    # The address must name the device's own agent.
    if await resolve_agent_id_soft(websocket) != device.agent_id:  # type: ignore[arg-type]
        return None
    return device


async def _receive_frame(websocket: WebSocket) -> dict[str, Any]:
    message = await websocket.receive()
    if message["type"] == "websocket.disconnect":
        raise WebSocketDisconnect(message.get("code", 1000))
    text = message.get("text")
    if text is None:
        raise _Close(CLOSE_PROTOCOL, "text frames only")
    if len(text) > MAX_FRAME_CHARS:
        raise _Close(CLOSE_PROTOCOL, "frame too large")
    try:
        frame = json.loads(text)
    except ValueError:
        raise _Close(CLOSE_PROTOCOL, "invalid JSON") from None
    if not isinstance(frame, dict):
        raise _Close(CLOSE_PROTOCOL, "a frame is a JSON object")
    return frame


async def _hello(websocket: WebSocket) -> None:
    try:
        frame = await asyncio.wait_for(_receive_frame(websocket), HELLO_TIMEOUT_S)
    except TimeoutError:
        raise _Close(CLOSE_PROTOCOL, "no hello") from None
    if frame.get("type") != "hello":
        raise _Close(CLOSE_PROTOCOL, "expected hello")
    protocols = frame.get("protocols")
    if not isinstance(protocols, list) or PROTOCOL_VERSION not in protocols:
        await websocket.send_json({
            "type": "error", "code": "unsupported_protocol", "supported": [PROTOCOL_VERSION],
        })
        raise _Close(CLOSE_PROTOCOL, "unsupported protocol")


def _check_current(current: DeviceRecord | None, device: DeviceRecord) -> None:
    """Close unless the device still exists under the credentials it connected with."""
    if (
        current is None
        or current.revoked_at is not None
        or current.credential_generation != device.credential_generation
    ):
        raise _Close(CLOSE_REVOKED, "revoked")


async def _serve(
    websocket: WebSocket,
    pubsub: PubSub,
    *,
    device: DeviceRecord,
    holder: str,
    store: DeviceStore,
    presence: DevicePresence,
) -> None:
    """Run the heartbeat and the control listener until either ends the connection."""
    tasks = {
        asyncio.create_task(
            _heartbeats(websocket, device=device, holder=holder, store=store, presence=presence),
        ),
        asyncio.create_task(_control(pubsub, device=device, holder=holder, presence=presence)),
    }
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    for task in done:
        task.result()


async def _heartbeats(
    websocket: WebSocket,
    *,
    device: DeviceRecord,
    holder: str,
    store: DeviceStore,
    presence: DevicePresence,
) -> None:
    last_check = last_refresh = time.monotonic()
    while True:
        try:
            frame = await asyncio.wait_for(_receive_frame(websocket), IDLE_TIMEOUT_S)
        except TimeoutError:
            raise _Close(CLOSE_IDLE, "heartbeat timeout") from None
        kind = frame.get("type")
        if kind == "revoke":
            await store.revoke_by_id(device.id)
            raise _Close(CLOSE_REVOKED, "revoked")
        if kind != "ping":
            raise _Close(CLOSE_PROTOCOL, "unexpected frame")
        now = time.monotonic()
        if now - last_refresh >= MIN_REFRESH_INTERVAL_S:
            last_refresh = now
            if not await presence.refresh(device.id, holder):
                raise _Close(CLOSE_SUPERSEDED, "superseded")
        if now - last_check >= LAST_SEEN_INTERVAL_S:
            last_check = now
            _check_current(await store.touch(device.id), device)
        await websocket.send_json({"type": "pong"})


async def _control(
    pubsub: PubSub, *, device: DeviceRecord, holder: str, presence: DevicePresence,
) -> None:
    # ponytail: one pub/sub connection per device socket; share one subscriber
    # per pod when connected devices number in the thousands.
    while True:
        message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
        if message is None:
            continue
        data = message["data"]
        text = data.decode() if isinstance(data, bytes) else str(data)
        kind, _, value = text.partition(":")
        generation = int(value) if value.isdigit() else None
        # A revocation of an older generation predates this connection's token.
        if kind == "revoked" and generation is not None and generation >= device.credential_generation:
            raise _Close(CLOSE_REVOKED, "revoked")
        if kind == "rotated" and generation is not None and generation > device.credential_generation:
            raise _Close(CLOSE_REVOKED, "credentials rotated")
        # Two connections opened together each see the other's claim; only the
        # one that no longer holds the device steps down.
        if kind == "superseded" and value != holder and not await presence.holds(device.id, holder):
            raise _Close(CLOSE_SUPERSEDED, "superseded")
