"""The device link: one authenticated WebSocket per connected desktop device.

Protocol version 1, JSON text frames of at most MAX_FRAME_CHARS (2 MiB); the app must
accept frames that large.

  app -> server   {"type": "hello", "protocols": [1], "open": [ids]}
                                                        first frame, within HELLO_TIMEOUT_S; "open"
                                                        lists the operations the app holds
                                                        unfinished, at most MAX_OPEN_REPORTED
  server -> app   {"type": "welcome", "protocol": 1, "device_id", "org_id",
                   "agent_id", "user_id", "name", "heartbeat_s": 15}
  app -> server   {"type": "ping"}                      every heartbeat_s
  server -> app   {"type": "pong"}
  app -> server   {"type": "revoke"}                    the app removes this device
  server -> app   {"type": "op", "id", "session_id", "calling_session_id",
                   "invocation_id", "ordinal", "kind", "args", "digest"}
                                                         an operation to run
  app -> server   {"type": "op_result", "id", "digest", "outcome"}
                                                         outcome is {"ok": v} or {"error": {...}}
  server -> app   {"type": "op_ack", "id"}               the outcome is recorded durably
  server -> app   {"type": "cancel", "id"}               the session stopped this operation: end it,
                                                        record that, and never run it

Every handshake is accepted and a refusal is a close code, so the app can tell
a refused token from a proxy's HTTP error:

  4400  protocol error, including an unsupported protocol, an oversized frame
        or a malformed open list in hello
  4401  token unknown, including one replaced by reauthorization, or the address
        names another agent or none: the app stops reconnecting and suspends local work
  4403  the revocation case: revoked (also when a revoked device reconnects),
        credentials rotated, or the device's user removed. The app stops
        reconnecting, suspends local work and shows that local access was revoked
  4408  no frame for IDLE_TIMEOUT_S: the app reconnects
  4409  another connection of this device took over: this one ends
  1011  server trouble, as is any other close: the app reconnects with backoff

Operations (kinds, arguments, outcomes and size limits:
surogates.devices.workspace) go only to the connection holding the device's
presence, under its current credentials.  Each is sent once per connection:
after welcome, when a worker announces it, or after a ping (at most once a
second).  A task of its own sends them, so a long batch never delays a pong, a
reply's acknowledgement or a revocation.  A new connection is sent every
operation still open, up to 100 at a time, so the app can see one again; the
rest follow at the next reconcile or announcement.
The app keeps a journal by operation id: it runs each operation once and
answers a repeat with the recorded outcome.  A reply for an operation this
device was not given, or with another digest, is a protocol error (4400); a
reply under rotated-out credentials closes with 4403.
On connect, before any operation, the server sends a cancel for each operation
the app reported open that was cancelled.  Later, a cancel is sent live when it
happens; if that one was lost, the next reconcile sends it once more.  So a
cancel may arrive more than once (live, resent, and on reconnect), and the app
treats it as idempotent.  The server does not acknowledge a cancel.  A cancel
may arrive before the op it names, so the app records unknown ids too.  A
cancelled bind dismisses the folder prompt.  A result the app sends for a
cancelled operation is acknowledged as a duplicate and changes nothing.

The app also drops the connection, and reconnects with backoff, when no welcome
arrives within 10 s of connecting, or no frame from the server within
2 x heartbeat_s of a ping.  Any frame counts as liveness, not only a pong:
behind a buffering proxy a pong still queues behind the operation frames sent
before it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Awaitable
from typing import Any, TypeVar
from uuid import UUID

from fastapi import WebSocket, WebSocketDisconnect
from redis.asyncio.client import PubSub

from surogates.devices.operations import DeviceOperations
from surogates.devices.presence import (
    HEARTBEAT_INTERVAL_S,
    PRESENCE_TTL_S,
    DevicePresence,
    new_holder,
)
from surogates.devices.store import DeviceRecord, DeviceStore
from surogates.devices.workspace import is_well_formed
from surogates.runtime.resolver import resolve_agent_id_soft

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

PROTOCOL_VERSION = 1
HELLO_TIMEOUT_S = 10.0
# The most operations a hello may report as unfinished.
MAX_OPEN_REPORTED = 1000
# The longest one Redis or database call may take before the link closes with
# 1011: a stalled dependency must not leave the socket open with no pong.
DEPENDENCY_TIMEOUT_S = 5.0
# A device that sends nothing for as long as its presence lasts is gone.
IDLE_TIMEOUT_S = float(PRESENCE_TTL_S)
# How often a connected device's row is read back (and ``last_seen_at``
# written): the fallback for a lost revocation or rotation message.
LAST_SEEN_INTERVAL_S = 60.0
# Pings faster than this are answered without touching Redis.
MIN_REFRESH_INTERVAL_S = 1.0
# One operation's 1 MiB of file data, base64-encoded, plus its envelope (the
# operation contract keeps every args object and outcome under 1.5 MiB).
MAX_FRAME_CHARS = 2 * 1024 * 1024
# A client that stops reading must not stall the connection's other work.
SEND_TIMEOUT_S = 10.0

CLOSE_PROTOCOL = 4400
CLOSE_UNAUTHENTICATED = 4401
CLOSE_REVOKED = 4403
CLOSE_IDLE = 4408
CLOSE_SUPERSEDED = 4409


async def _bounded(call: Awaitable[_T]) -> _T:
    """Await one Redis or database call for at most DEPENDENCY_TIMEOUT_S.

    Its TimeoutError is not the receive timeout: keep it out of any
    ``except TimeoutError`` that maps to CLOSE_IDLE, so the link ends in 1011.
    """
    async with asyncio.timeout(DEPENDENCY_TIMEOUT_S):
        return await call


class _Close(Exception):
    """End the connection with this close code."""

    def __init__(self, code: int, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


class _Link:
    """One connection's sending side.

    Frames go out one at a time, each within SEND_TIMEOUT_S.  Each open
    operation is delivered once per connection, and ``_delivered`` holds the
    ones sent but not yet answered; a repeat across connections is harmless,
    because the app's journal answers it without running it again.
    """

    def __init__(
        self,
        websocket: WebSocket,
        *,
        device: DeviceRecord,
        holder: str,
        presence: DevicePresence,
        operations: DeviceOperations,
    ) -> None:
        self._websocket = websocket
        self._device = device
        self._holder = holder
        self._presence = presence
        self._operations = operations
        self._send_lock = asyncio.Lock()
        self._delivered: set[UUID] = set()
        self._delivery_wanted = asyncio.Event()

    async def send(self, frame: dict[str, Any]) -> None:
        async with self._send_lock:
            async with asyncio.timeout(SEND_TIMEOUT_S):
                await self._websocket.send_json(frame)

    def deliver_soon(self) -> None:
        """Ask the delivery task for a delivery; never waits for one."""
        self._delivery_wanted.set()

    async def deliveries(self) -> None:
        """Deliver each time one is asked for, until the connection ends.

        Asks that arrive during a delivery are answered by the next one.
        """
        while True:
            await self._delivery_wanted.wait()
            self._delivery_wanted.clear()
            await self.deliver()

    async def deliver(self) -> None:
        """Send the device its open operations not yet sent on this connection.

        First sends a cancel again for each operation it was sent and has not
        answered that the server has closed since (cancelled or revoked), in
        case the live one was lost.
        Only the connection holding the device's presence delivers, so a
        superseded one that has not closed yet runs nothing.
        """
        if not await _bounded(self._presence.holds(self._device.id, self._holder)):
            return
        # A cancel announced while the app was connected may have been lost:
        # each reconcile tells it again about what it was sent and has not
        # answered.  Dropping them also keeps the delivered set bounded.
        if self._delivered:
            for operation_id in await _bounded(self._operations.closed_among(self._device.id, self._delivered)):
                self._delivered.discard(operation_id)
                await self.send({"type": "cancel", "id": str(operation_id)})
        for operation in await _bounded(self._operations.pending(
            self._device.id, self._device.credential_generation, exclude=self._delivered,
        )):
            # Another delivery may have sent it while this one's query ran.
            # Checked and marked with no await between, so only one sends it.
            if operation.id in self._delivered:
                continue
            self._delivered.add(operation.id)
            await self.send(operation.frame())

    def forget(self, operation_id: UUID) -> None:
        """Stop tracking an operation as sent and unanswered."""
        self._delivered.discard(operation_id)

    async def record(self, frame: dict[str, Any]) -> None:
        """Record an ``op_result`` durably, then acknowledge it."""
        try:
            operation_id = UUID(str(frame["id"]))
        except (KeyError, ValueError):
            raise _Close(CLOSE_PROTOCOL, "malformed op_result") from None
        digest = frame.get("digest")
        outcome = frame.get("outcome")
        if (
            not isinstance(digest, str)
            or not isinstance(outcome, dict)
            or len({"ok", "error"} & outcome.keys()) != 1
            or ("error" in outcome and not isinstance(outcome["error"], dict))
        ):
            raise _Close(CLOSE_PROTOCOL, "malformed op_result")
        if not is_well_formed(outcome):
            # Recorded, not refused: the app sends its journaled reply again on
            # every reconnect, so a refusal would loop.
            outcome = {"error": {"type": "other", "message": "The computer's result was not valid Unicode"}}
        status = await _bounded(self._operations.complete(
            self._device.id, self._device.credential_generation, operation_id, digest, outcome,
        ))
        if status == "stale":
            raise _Close(CLOSE_REVOKED, "credentials rotated")
        if status == "rejected":
            raise _Close(CLOSE_PROTOCOL, "result for an operation this device was not given")
        # Answered operations never come back from pending(), so only the
        # unanswered ones need excluding; this keeps the set, and the query
        # that carries it, bounded on a long-lived connection.
        self._delivered.discard(operation_id)
        await self.send({"type": "op_ack", "id": str(operation_id)})


async def serve_device_link(
    websocket: WebSocket,
    *,
    store: DeviceStore,
    presence: DevicePresence,
    operations: DeviceOperations,
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
    if device.revoked_at is not None:
        await websocket.close(code=CLOSE_REVOKED, reason="revoked")
        return

    holder = new_holder()
    pubsub: PubSub | None = None
    claimed = False
    try:
        reported = await _hello(websocket)
        # Listen before claiming, so every message sent after the claim is seen.
        pubsub = await _bounded(presence.subscribe(device.id))
        # Set before the claim: release is compare-and-delete, so it is safe
        # even when the claim fails between its SET and its PUBLISH.
        claimed = True
        await _bounded(presence.claim(device.id, holder))
        # A revocation or rotation published before the subscription was missed.
        _check_current(await _bounded(store.touch(device.id)), device)
        link = _Link(
            websocket, device=device, holder=holder, presence=presence, operations=operations,
        )
        await link.send({
            "type": "welcome",
            "protocol": PROTOCOL_VERSION,
            "device_id": str(device.id),
            "org_id": str(device.org_id),
            "agent_id": device.agent_id,
            "user_id": str(device.user_id),
            "name": device.name,
            "heartbeat_s": HEARTBEAT_INTERVAL_S,
        })
        # Cancellations come before any new work, so the app stops what the
        # server closed while it was away: a session stopped it, or a
        # revocation did.
        for operation_id in await _bounded(operations.closed_among(device.id, reported)):
            await link.send({"type": "cancel", "id": str(operation_id)})
        link.deliver_soon()
        await _serve(link, pubsub, device=device, holder=holder, store=store, presence=presence)
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
                await _bounded(presence.release(device.id, holder))
        if pubsub is not None:
            with contextlib.suppress(Exception):
                await _bounded(pubsub.aclose())


async def _authenticate(websocket: WebSocket, store: DeviceStore) -> DeviceRecord | None:
    scheme, _, token = (websocket.headers.get("authorization") or "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    device = await _bounded(store.find_by_token(token.strip()))
    if device is None:
        return None
    # The address must name the device's own agent, or a refusal would disclose
    # that another agent's device is revoked.
    if await _bounded(resolve_agent_id_soft(websocket)) != device.agent_id:  # type: ignore[arg-type]
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


async def _hello(websocket: WebSocket) -> list[UUID]:
    """Read the app's hello; returns the operations it reports unfinished."""
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
    reported = frame.get("open", [])
    if not isinstance(reported, list) or len(reported) > MAX_OPEN_REPORTED:
        raise _Close(CLOSE_PROTOCOL, "malformed open list")
    try:
        return [UUID(str(value)) for value in reported]
    except ValueError:
        raise _Close(CLOSE_PROTOCOL, "malformed open list") from None


def _check_current(current: DeviceRecord | None, device: DeviceRecord) -> None:
    """Close unless the device still exists under the credentials it connected with."""
    if (
        current is None
        or current.revoked_at is not None
        or current.credential_generation != device.credential_generation
    ):
        raise _Close(CLOSE_REVOKED, "revoked")


async def _serve(
    link: _Link,
    pubsub: PubSub,
    *,
    device: DeviceRecord,
    holder: str,
    store: DeviceStore,
    presence: DevicePresence,
) -> None:
    """Run the heartbeat, the control listener and the deliveries until one ends the connection.

    Neither of the first two waits for a delivery: a long batch of frames
    must not hold up a ping, a reply or a revocation.
    """
    tasks = {
        asyncio.create_task(
            _heartbeats(link, device=device, holder=holder, store=store, presence=presence),
        ),
        asyncio.create_task(_control(link, pubsub, device=device, holder=holder, presence=presence)),
        asyncio.create_task(link.deliveries()),
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
    link: _Link,
    *,
    device: DeviceRecord,
    holder: str,
    store: DeviceStore,
    presence: DevicePresence,
) -> None:
    last_check = last_refresh = time.monotonic()
    while True:
        try:
            frame = await asyncio.wait_for(_receive_frame(link._websocket), IDLE_TIMEOUT_S)
        except TimeoutError:
            raise _Close(CLOSE_IDLE, "heartbeat timeout") from None
        kind = frame.get("type")
        if kind == "revoke":
            await _bounded(store.revoke_by_id(device.id, device.credential_generation))
            raise _Close(CLOSE_REVOKED, "revoked")
        if kind == "op_result":
            await link.record(frame)
            continue
        if kind != "ping":
            raise _Close(CLOSE_PROTOCOL, "unexpected frame")
        now = time.monotonic()
        reconcile = now - last_refresh >= MIN_REFRESH_INTERVAL_S
        if reconcile:
            last_refresh = now
            if not await _bounded(presence.refresh(device.id, holder)):
                raise _Close(CLOSE_SUPERSEDED, "superseded")
        if now - last_check >= LAST_SEEN_INTERVAL_S:
            last_check = now
            _check_current(await _bounded(store.touch(device.id)), device)
        await link.send({"type": "pong"})
        # At most once a second, so a lost announcement strands nothing.
        if reconcile:
            link.deliver_soon()


async def _control(
    link: _Link,
    pubsub: PubSub,
    *,
    device: DeviceRecord,
    holder: str,
    presence: DevicePresence,
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
        # A worker announced an operation.
        if kind == "op":
            link.deliver_soon()
            continue
        # A session stopped an operation of this device.
        if kind == "cancel":
            try:
                operation_id = UUID(value)
            except ValueError:
                continue
            link.forget(operation_id)
            await link.send({"type": "cancel", "id": value})
            continue
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
