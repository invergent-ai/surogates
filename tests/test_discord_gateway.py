"""Discord Gateway client against an in-process fake socket, and the
supervisor's leader lock."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
import respx
from websockets.asyncio.server import serve

from surogates.channels import gateway as gw
from surogates.channels.gateway import (
    DIRECT_MESSAGES,
    GUILD_MESSAGES,
    GUILDS,
    MESSAGE_CONTENT,
    DiscordGatewayClient,
    DiscordGatewaySupervisor,
)
from surogates.channels.platforms.discord import DISCORD_API_BASE, DiscordPlatform

TOKEN = "fake-token"
BOT = "900000000000000001"


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(gw, "_BACKOFF_INITIAL_S", 0.01)
    monkeypatch.setattr(gw, "_INVALID_SESSION_WAIT_S", (0.0, 0.01))


class FakeGateway:
    """HELLO, heartbeat ACK, IDENTIFY → READY, RESUME → RESUMED.

    ``script`` maps a connection number to a coroutine run after that
    connection's IDENTIFY/RESUME, for the test's own frames.
    """

    def __init__(self, *, interval_ms: int = 50, ack: bool = True) -> None:
        self.interval_ms = interval_ms
        self.ack = ack
        self.frames: list[tuple[int, dict]] = []
        self.connections = 0
        self.seq = 0
        self.url = ""
        self.script: dict[int, Any] = {}
        self.close_on_identify: int | None = None

    def ops(self, op: int) -> list[tuple[int, dict]]:
        return [(n, f) for n, f in self.frames if f["op"] == op]

    async def send_dispatch(self, ws, t: str, d: dict) -> None:
        self.seq += 1
        await ws.send(json.dumps({"op": 0, "t": t, "s": self.seq, "d": d}))

    async def handler(self, ws) -> None:
        self.connections += 1
        n = self.connections
        await ws.send(json.dumps({"op": 10, "d": {"heartbeat_interval": self.interval_ms}}))
        async for raw in ws:
            frame = json.loads(raw)
            self.frames.append((n, frame))
            op = frame["op"]
            if op == 1 and self.ack:
                await ws.send(json.dumps({"op": 11}))
            elif op == 2:
                if self.close_on_identify:
                    await ws.close(self.close_on_identify, "nope")
                    return
                await self.send_dispatch(ws, "READY", {
                    "session_id": f"sess{n}", "resume_gateway_url": self.url,
                    "user": {"id": BOT, "username": "surogate", "bot": True},
                })
            elif op == 6:
                await self.send_dispatch(ws, "RESUMED", {})
            if op in (2, 6) and n in self.script:
                await self.script[n](self, ws)


async def _wait(cond, timeout: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not cond():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition never became true")
        await asyncio.sleep(0.01)


@pytest.fixture
async def fake():
    g = FakeGateway()
    async with serve(g.handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        g.url = f"ws://127.0.0.1:{port}"
        with respx.mock(assert_all_called=False) as router:
            router.get(f"{DISCORD_API_BASE}/gateway/bot").mock(
                return_value=httpx.Response(200, json={"url": g.url}),
            )
            yield g


async def _running(client: DiscordGatewayClient):
    return asyncio.create_task(client.run())


async def _stop(task: asyncio.Task) -> None:
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def _client(events: list | None = None, intents: int = 4609) -> DiscordGatewayClient:
    async def _on(event, data, client):
        if events is not None:
            events.append((event, data, client.bot_user_id))

    return DiscordGatewayClient(token=TOKEN, intents=intents, on_dispatch=_on)


async def test_identifies_heartbeats_and_dispatches(fake):
    async def _message(g, ws):
        await g.send_dispatch(ws, "MESSAGE_CREATE", {"id": "1", "content": "hi"})

    fake.script[1] = _message
    events: list = []
    client = _client(events)
    task = await _running(client)
    await _wait(lambda: events and len(fake.ops(1)) >= 2)
    await _stop(task)

    (_, identify), = fake.ops(2)
    assert identify["d"]["token"] == TOKEN and identify["d"]["intents"] == 4609
    assert events == [("MESSAGE_CREATE", {"id": "1", "content": "hi"}, BOT)]
    assert client.session_id == "sess1" and client.seq == 2
    # Heartbeats carry the last sequence number seen.
    assert fake.ops(1)[-1][1]["d"] == 2


async def test_a_missed_ack_reconnects_and_resumes(fake):
    fake.ack = False
    client = _client()
    task = await _running(client)
    await _wait(lambda: fake.ops(6))
    await _stop(task)

    assert len(fake.ops(2)) == 1  # identified once, resumed after
    (n, resume), *_ = fake.ops(6)
    assert n == 2 and resume["d"] == {"token": TOKEN, "session_id": "sess1", "seq": 1}


async def test_reconnect_request_resumes_on_a_new_connection(fake):
    async def _reconnect(g, ws):
        await ws.send(json.dumps({"op": 7, "d": None}))

    fake.script[1] = _reconnect
    task = await _running(_client())
    await _wait(lambda: fake.ops(6))
    await _stop(task)
    assert len(fake.ops(2)) == 1 and fake.ops(6)[0][0] == 2


@pytest.mark.parametrize("resumable", [True, False])
async def test_invalid_session(fake, resumable):
    async def _invalid(g, ws):
        await ws.send(json.dumps({"op": 9, "d": resumable}))

    fake.script[1] = _invalid
    task = await _running(_client())
    await _wait(lambda: fake.connections >= 2 and any(n == 2 for n, f in fake.frames if f["op"] in (2, 6)))
    await _stop(task)
    second = [f["op"] for n, f in fake.frames if n == 2 and f["op"] in (2, 6)]
    assert second == ([6] if resumable else [2])


async def test_a_fatal_close_stops_the_client(fake):
    fake.close_on_identify = 4004
    client = _client()
    await asyncio.wait_for(client.run(), 3)
    assert client.fatal_code == 4004 and fake.connections == 1


async def test_an_unauthorized_token_is_fatal_before_connecting():
    with respx.mock() as router:
        router.get(f"{DISCORD_API_BASE}/gateway/bot").mock(return_value=httpx.Response(401))
        client = _client()
        await asyncio.wait_for(client.run(), 3)
    assert client.fatal_code == 4004


# ---------------------------------------------------------------------------
# Supervisor
# ---------------------------------------------------------------------------


class _Redis:
    """The four calls RedisLeaderLock makes."""

    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}

    async def set(self, key, value, *, nx=False, xx=False, ex=None):
        if (nx and key in self.data) or (xx and key not in self.data):
            return None
        self.data[key] = value
        return True

    async def get(self, key):
        return self.data.get(key)

    async def delete(self, key):
        self.data.pop(key, None)


class _Vault:
    async def resolve_ref(self, ref, *, org_id):
        return "vault-token" if ref == "vault://discord_bot_token_app1" else None


class _Ops:
    def __init__(self, routings):
        self.routings = routings

    async def list_channel_routings(self, kind):
        assert kind == "discord"
        return self.routings


class _FakeClient:
    instances: list["_FakeClient"] = []

    def __init__(self, *, token, intents, on_dispatch):
        self.token, self.intents, self.on_dispatch = token, intents, on_dispatch
        self.bot_user_id = BOT
        self.fatal_code = None
        self.cancelled = False
        self.stop = asyncio.Event()
        _FakeClient.instances.append(self)

    async def run(self):
        try:
            await self.stop.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


def _supervisor(routings, redis=None, ingested=None):
    _FakeClient.instances = []

    async def _ingest(platform, ident, body):
        ingested.append((platform.kind, ident, body))

    return DiscordGatewaySupervisor(
        platform=DiscordPlatform(), platform_client=_Ops(routings), vault=_Vault(),
        redis=redis or _Redis(), ingest=_ingest, client_factory=_FakeClient, holder_id="me",
    )


ROUTING = {"channel_identifier": "app1", "org_id": "o", "agent_id": "a", "config": {}}


async def test_the_lock_winner_starts_the_bot_and_feeds_ingest():
    ingested: list = []
    sup = _supervisor([ROUTING], ingested=ingested)
    await sup.reconcile()

    (client,) = _FakeClient.instances
    assert sup.held == {"app1"} and client.token == "vault-token"
    assert client.intents == GUILDS | GUILD_MESSAGES | DIRECT_MESSAGES
    await client.on_dispatch("MESSAGE_CREATE", {"id": "5"}, client)
    await client.on_dispatch("TYPING_START", {}, client)
    assert ingested == [("discord", "app1", {"t": "MESSAGE_CREATE", "d": {"id": "5"}, "bot_user_id": BOT})]
    await sup.shutdown()


async def test_a_lock_held_elsewhere_starts_nothing():
    redis = _Redis()
    redis.data["discord-gateway:app1"] = b"other-replica"
    sup = _supervisor([ROUTING], redis=redis)
    await sup.reconcile()
    assert sup.held == set() and _FakeClient.instances == []


async def test_a_removed_routing_stops_the_bot_and_frees_the_lock():
    redis = _Redis()
    sup = _supervisor([ROUTING], redis=redis)
    await sup.reconcile()
    await asyncio.sleep(0)  # let the connection task start
    (client,) = _FakeClient.instances
    sup._platform_client.routings = []
    await sup.reconcile()
    assert client.cancelled and sup.held == set() and redis.data == {}


async def test_a_change_restarts_with_the_new_intents():
    sup = _supervisor([ROUTING])
    await sup.reconcile()
    await asyncio.sleep(0)  # let the connection task start
    sup._platform_client.routings = [{**ROUTING, "config": {"message_content": True}}]
    await sup.reconcile(changed="app1")
    first, second = _FakeClient.instances
    assert first.cancelled and second.intents & MESSAGE_CONTENT
    await sup.shutdown()


async def test_a_fatal_bot_is_left_alone_until_its_routing_changes():
    sup = _supervisor([ROUTING])
    await sup.reconcile()
    (client,) = _FakeClient.instances
    client.fatal_code = 4004
    client.stop.set()
    await asyncio.sleep(0)
    await sup.reconcile()
    await sup.reconcile()
    assert len(_FakeClient.instances) == 1 and sup.held == set()
    await sup.reconcile(changed="app1")
    assert len(_FakeClient.instances) == 2
    await sup.shutdown()
