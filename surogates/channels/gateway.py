"""Discord Gateway: the runtime's first socket channel.

Discord delivers a bot's messages only over a websocket the bot keeps open
(the Gateway), never as a webhook.  So the channels process holds that
connection itself, one per customer bot:

* :class:`DiscordGatewayClient` is one bot's connection: ``GET /gateway/bot``
  for the URL, HELLO → heartbeats (a missed ACK means a dead link, so it
  reconnects), IDENTIFY with the routing's intents, READY's ``session_id`` /
  ``resume_gateway_url`` / bot user kept so every later reconnect RESUMEs
  instead of identifying again.  Discord allows 1 000 identifies a day per
  bot, so a reconnect storm must not spend them: RESUME whenever the session
  lives, back off exponentially between failed attempts, and stop for good on
  the close codes that say retrying cannot help (bad token, bad intents).
  Each ``MESSAGE_CREATE`` goes to the dispatcher's shared ``ingest()``, the
  same code a webhook body runs after verify.

* :class:`DiscordGatewaySupervisor` decides which bots this replica holds.
  On boot, every few seconds, and on each
  ``channel_routing_changed:discord:<app id>`` it lists the active routings
  (ops ``GET /api/channels/by-kind/discord``) and, per bot, takes the Redis
  leader lock ``discord-gateway:<app id>``: two replicas holding one bot's
  Gateway would answer every message twice.  The lock is renewed each pass;
  a replica that dies lets it expire and another takes the bot over.

No discord.py: a thin client over ``websockets`` is all the runtime needs,
since everything outbound is plain REST (:mod:`.platforms.discord`).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import socket
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from surogates.channels.credentials import resolve_channel_credentials
from surogates.channels.platforms import discord as discord_platform
from surogates.runtime.leader_lock import RedisLeaderLock
from surogates.runtime.pubsub import listen_forever

__all__ = ["DiscordGatewayClient", "DiscordGatewaySupervisor", "intents_for"]

logger = logging.getLogger(__name__)

# Intents (https://discord.com/developers/docs/events/gateway#gateway-intents).
GUILDS = 1 << 0
GUILD_MESSAGES = 1 << 9
DIRECT_MESSAGES = 1 << 12
#: Privileged: the customer must switch it on in the Developer Portal, or
#: IDENTIFY is refused with close code 4014.  Without it Discord blanks the
#: content of server messages that do not mention the bot.
MESSAGE_CONTENT = 1 << 15

#: Close codes after which reconnecting cannot succeed: authentication failed
#: (4004), invalid shard / sharding required (4010, 4011), invalid API version
#: (4012), invalid or disallowed intents (4013, 4014).
FATAL_CLOSE_CODES = frozenset({4004, 4010, 4011, 4012, 4013, 4014})
#: Close codes that end the session (invalid seq, session timed out): the next
#: connection must IDENTIFY again.
_SESSION_LOST_CODES = frozenset({4007, 4009})
#: Our own close code when we drop a link ourselves.  Anything but 1000/1001,
#: which would make Discord invalidate the session we mean to resume.
_RECONNECT_CLOSE = 4000

_BACKOFF_INITIAL_S = 1.0
_BACKOFF_MAX_S = 60.0
#: Discord asks for a random 1–5 s wait after INVALID SESSION.
_INVALID_SESSION_WAIT_S = (1.0, 5.0)
#: READY for a bot in many servers is large; GUILD_CREATE larger still.
_MAX_FRAME = 16 * 1024 * 1024


def intents_for(config: dict | None) -> int:
    """The intents a routing's bot identifies with."""
    intents = GUILDS | GUILD_MESSAGES | DIRECT_MESSAGES
    if (config or {}).get("message_content"):
        intents |= MESSAGE_CONTENT
    return intents


class _Fatal(Exception):
    def __init__(self, code: int) -> None:
        super().__init__(code)
        self.code = code


Dispatch = Callable[[str, dict, "DiscordGatewayClient"], Awaitable[None]]


class DiscordGatewayClient:
    """One bot's Gateway connection, held until cancelled or a fatal close.

    *on_dispatch* receives ``(event_name, data, client)`` for every dispatch
    but READY and RESUMED; it runs as its own task so a slow pipeline never
    holds up heartbeats or the next event.
    """

    def __init__(
        self,
        *,
        token: str,
        intents: int,
        on_dispatch: Dispatch,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._token = token
        self._intents = intents
        self._on_dispatch = on_dispatch
        self._http = http
        self.session_id: str | None = None
        self.resume_url: str | None = None
        self.seq: int | None = None
        self.bot_user_id: str = ""
        #: The close code that stopped this client for good, if one did.
        self.fatal_code: int | None = None
        self._acked = True
        self._healthy = False
        self._tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Connect, and reconnect, until cancelled or a fatal close code."""
        delay = _BACKOFF_INITIAL_S
        while True:
            self._healthy = False
            try:
                await self._session()
            except _Fatal as exc:
                self.fatal_code = exc.code
                logger.error(
                    "[discord] Gateway refused this bot for good (close code %s); "
                    "fix the token or intents in the Developer Portal and reconnect the channel",
                    exc.code,
                )
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — any failure is a reconnect
                logger.warning("[discord] Gateway connection failed: %r", exc)
            if self._healthy:
                # The link worked and dropped (or Discord asked us to move):
                # come straight back, with a little jitter.
                delay = _BACKOFF_INITIAL_S
                await asyncio.sleep(random.random() * _BACKOFF_INITIAL_S)
            else:
                await asyncio.sleep(delay * (0.5 + random.random() / 2))
                delay = min(delay * 2, _BACKOFF_MAX_S)

    async def _gateway_url(self) -> str:
        http = self._http or httpx.AsyncClient(timeout=15.0)
        try:
            resp = await http.get(
                f"{discord_platform.DISCORD_API_BASE}/gateway/bot",
                headers={"Authorization": f"Bot {self._token}"},
            )
        finally:
            if self._http is None:
                await http.aclose()
        if resp.status_code == 401:
            raise _Fatal(4004)
        resp.raise_for_status()
        return resp.json()["url"]

    async def _session(self) -> None:
        """One connection, from HELLO to its close."""
        resuming = bool(self.session_id and self.resume_url)
        base = self.resume_url if resuming else await self._gateway_url()
        async with connect(
            f"{base.rstrip('/')}/?v=10&encoding=json", max_size=_MAX_FRAME, open_timeout=30,
        ) as ws:
            hello = json.loads(await asyncio.wait_for(ws.recv(), 30))
            if hello.get("op") != 10:
                raise RuntimeError(f"expected HELLO, got op {hello.get('op')}")
            interval = float(hello["d"]["heartbeat_interval"]) / 1000.0
            self._acked = True
            heartbeat = asyncio.create_task(self._heartbeat(ws, interval))
            try:
                if resuming:
                    await self._send(ws, 6, {
                        "token": self._token, "session_id": self.session_id, "seq": self.seq,
                    })
                else:
                    await self._send(ws, 2, {
                        "token": self._token,
                        "intents": self._intents,
                        "properties": {"os": "linux", "browser": "surogates", "device": "surogates"},
                    })
                try:
                    async for raw in ws:
                        await self._handle(ws, json.loads(raw))
                except ConnectionClosed:
                    pass
            finally:
                heartbeat.cancel()
            code = ws.close_code
        if code in FATAL_CLOSE_CODES:
            raise _Fatal(code)
        if code in _SESSION_LOST_CODES:
            self._forget_session()
        logger.info("[discord] Gateway closed (code %s); reconnecting", code)

    def _forget_session(self) -> None:
        self.session_id = self.resume_url = None
        self.seq = None

    # ------------------------------------------------------------------
    # Frames
    # ------------------------------------------------------------------

    @staticmethod
    async def _send(ws: Any, op: int, d: Any) -> None:
        await ws.send(json.dumps({"op": op, "d": d}))

    async def _heartbeat(self, ws: Any, interval: float) -> None:
        """Beat every *interval*, the first after a random fraction of it."""
        try:
            await asyncio.sleep(interval * random.random())
            while True:
                if not self._acked:
                    logger.warning("[discord] heartbeat not acknowledged; reconnecting")
                    await ws.close(_RECONNECT_CLOSE, "heartbeat ACK missed")
                    return
                self._acked = False
                await self._send(ws, 1, self.seq)
                await asyncio.sleep(interval)
        except (asyncio.CancelledError, ConnectionClosed):
            return

    async def _handle(self, ws: Any, payload: dict) -> None:
        op = payload.get("op")
        if payload.get("s") is not None:
            self.seq = payload["s"]
        if op == 0:
            event, data = payload.get("t") or "", payload.get("d") or {}
            if event == "READY":
                self.session_id = data.get("session_id")
                self.resume_url = data.get("resume_gateway_url")
                self.bot_user_id = str((data.get("user") or {}).get("id") or "")
                self._healthy = True
                logger.info("[discord] Gateway ready as %s", self.bot_user_id)
            elif event == "RESUMED":
                self._healthy = True
            else:
                task = asyncio.create_task(self._dispatch(event, data))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
        elif op == 1:
            await self._send(ws, 1, self.seq)
        elif op == 11:
            self._acked = True
        elif op == 7:
            await ws.close(_RECONNECT_CLOSE, "reconnect requested")
        elif op == 9:
            if not payload.get("d"):
                self._forget_session()
            await asyncio.sleep(random.uniform(*_INVALID_SESSION_WAIT_S))
            await ws.close(_RECONNECT_CLOSE, "invalid session")

    async def _dispatch(self, event: str, data: dict) -> None:
        try:
            await self._on_dispatch(event, data, self)
        except Exception:
            logger.exception("[discord] handling %s failed", event)


# ---------------------------------------------------------------------------
# Supervisor
# ---------------------------------------------------------------------------


@dataclass
class _Held:
    client: Any
    task: asyncio.Task
    lock: RedisLeaderLock


class DiscordGatewaySupervisor:
    """Holds a Gateway connection for each active Discord routing this
    replica wins the leader lock for.

    *ingest* is ``ChannelWebhookDispatcher.ingest_from_socket``.
    """

    LOCK_TTL_S = 30

    def __init__(
        self,
        *,
        platform: Any,
        platform_client: Any,
        vault: Any,
        redis: Any,
        ingest: Callable[[Any, str, Any], Awaitable[Any]],
        client_factory: Callable[..., Any] = DiscordGatewayClient,
        holder_id: str | None = None,
    ) -> None:
        self._platform = platform
        self._platform_client = platform_client
        self._vault = vault
        self._redis = redis
        self._ingest = ingest
        self._client_factory = client_factory
        self._holder = holder_id or f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self._held: dict[str, _Held] = {}
        #: Bots stopped on a fatal close or a missing token; left alone until
        #: their routing changes, so a bad token costs one identify, not a
        #: thousand.
        self._halted: set[str] = set()
        self._pass = asyncio.Lock()

    @property
    def held(self) -> set[str]:
        return set(self._held)

    async def reconcile(self, changed: str | None = None) -> None:
        """Bring the held connections in line with ops' active routings.

        *changed* is the identifier a ``channel_routing_changed`` named: its
        connection restarts, so a new token or intents take effect.
        """
        async with self._pass:
            try:
                routings = await self._platform_client.list_channel_routings("discord")
            except Exception:
                # Ops unreachable says nothing about the bots: keep what we
                # hold (and its locks) rather than dropping every connection.
                logger.warning("[discord] listing routings failed; keeping connections", exc_info=True)
                await self._renew_all()
                return
            wanted = {r["channel_identifier"]: r for r in routings if r.get("channel_identifier")}
            if changed is not None:
                self._halted.discard(changed)

            for ident in list(self._held):
                held = self._held[ident]
                if ident not in wanted or ident == changed:
                    await self._stop(ident)
                elif held.task.done():
                    if getattr(held.client, "fatal_code", None):
                        self._halted.add(ident)
                        logger.error(
                            "[discord] routing %s marked errored: Gateway close code %s",
                            ident, held.client.fatal_code,
                        )
                    await self._stop(ident)
                elif not await self._renew(held):
                    logger.warning("[discord] lost the Gateway lock for %s; stopping", ident)
                    await self._stop(ident, release=False)

            for ident, routing in wanted.items():
                if ident not in self._held and ident not in self._halted:
                    try:
                        await self._start(ident, routing)
                    except Exception:
                        logger.exception("[discord] starting the Gateway for %s failed", ident)

    async def run(self) -> None:
        """Reconcile now, every ``LOCK_TTL_S / 3`` and on every routing change."""

        async def _tick() -> None:
            while True:
                await self.reconcile()
                await asyncio.sleep(self.LOCK_TTL_S / 3)

        async def _on_message(msg: dict) -> None:
            channel = msg.get("channel") or ""
            if isinstance(channel, bytes):
                channel = channel.decode()
            ident = channel.removeprefix("channel_routing_changed:discord:")
            await self.reconcile(changed=ident or None)

        try:
            await asyncio.gather(
                _tick(),
                listen_forever(
                    self._redis, ["channel_routing_changed:discord:*"], _on_message,
                    name="[discord] Gateway supervisor",
                ),
            )
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        """Close every connection and release its lock."""
        for ident in list(self._held):
            await self._stop(ident)

    # ------------------------------------------------------------------

    async def _start(self, ident: str, routing: dict) -> None:
        lock = RedisLeaderLock(
            self._redis, key=f"discord-gateway:{ident}",
            ttl_seconds=self.LOCK_TTL_S, holder_id=self._holder,
        )
        if not await lock.acquire():
            return  # another replica holds this bot
        creds = await resolve_channel_credentials(
            vault=self._vault, kind="discord", identifier=ident,
            org_id=routing.get("org_id", ""),
            refs=self._platform.descriptor.vault_refs(ident),
        )
        token = (creds or {}).get("bot_token") or ""
        if not token:
            logger.error("[discord] routing %s marked errored: no bot token in the vault", ident)
            self._halted.add(ident)
            await lock.release()
            return

        async def _on_dispatch(event: str, data: dict, client: Any) -> None:
            if event == "MESSAGE_CREATE":
                await self._ingest(self._platform, ident, {
                    "t": event, "d": data, "bot_user_id": client.bot_user_id,
                })

        client = self._client_factory(
            token=token, intents=intents_for(routing.get("config")), on_dispatch=_on_dispatch,
        )
        task = asyncio.create_task(client.run(), name=f"discord-gateway-{ident}")
        self._held[ident] = _Held(client=client, task=task, lock=lock)
        logger.info("[discord] holding the Gateway for %s", ident)

    async def _stop(self, ident: str, *, release: bool = True) -> None:
        held = self._held.pop(ident, None)
        if held is None:
            return
        held.task.cancel()
        await asyncio.gather(held.task, return_exceptions=True)
        if release:
            try:
                await held.lock.release()
            except Exception:
                logger.warning("[discord] releasing the Gateway lock for %s failed", ident, exc_info=True)

    async def _renew(self, held: _Held) -> bool:
        try:
            return await held.lock.heartbeat()
        except Exception:
            # Redis down: we cannot prove we lost it, and nobody else can
            # take it either, so keep the connection.
            logger.warning("[discord] renewing a Gateway lock failed", exc_info=True)
            return True

    async def _renew_all(self) -> None:
        for ident, held in list(self._held.items()):
            if not await self._renew(held):
                await self._stop(ident, release=False)
