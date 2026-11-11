"""A Discord simulation: the runtime's real Gateway client holds the world's
Gateway, the person's messages arrive on it as MESSAGE_CREATE, and what the
adapter posts, edits and types lands in the run."""

from __future__ import annotations

import asyncio
import socket
import uuid
from types import SimpleNamespace

import httpx
import pytest
import uvicorn
from fastapi import FastAPI

from surogates.channels.gateway import DiscordGatewayClient, DiscordGatewaySupervisor
from surogates.channels.platforms.discord import DiscordPlatform, _api_base, open_dm, parse
from surogates.simulation import api, discord, store
from surogates.simulation.ids import new_discord_bot_token, new_discord_id
from tests.test_simulation_api import TOKEN
from tests.test_simulation_models import rsf  # noqa: F401
from tests.test_simulation_store import FakeVault, line, make_org, provision_into


def test_only_a_simulation_token_reaches_the_world(monkeypatch):
    monkeypatch.setenv("SUROGATES_SIMULATION_URL", "http://simulation:8004")
    assert _api_base(new_discord_bot_token(new_discord_id())) == "http://simulation:8004/d/api/v10"
    # A real token starts with the base64 of the bot's id.
    assert _api_base("MTk5OTk5OTk5OTk5OTk5OTk5.Gx.abc").startswith("https://discord.com/")
    assert _api_base("999123.abc").startswith("https://discord.com/")


@pytest.mark.asyncio
async def test_the_supervisor_holds_running_simulation_bots():
    started: list[str] = []

    class Client:
        def __init__(self, **kw):
            started.append(kw["token"])

        async def run(self):
            await asyncio.sleep(3600)

    class Lock:
        def __init__(self, *a, **k):
            pass

        async def acquire(self):
            return True

        async def release(self):
            pass

    class Vault:
        async def resolve_ref(self, ref, *, org_id):
            return "tok-" + ref.rsplit("_", 1)[-1]

    class Ops:
        async def list_channel_routings(self, kind):
            return [{"channel_identifier": "real", "org_id": "o"}]

    async def simulations():
        return [{"channel_identifier": "999simulation", "org_id": "o", "config": {}}]

    import surogates.channels.gateway as gw
    original, gw.RedisLeaderLock = gw.RedisLeaderLock, Lock
    try:
        supervisor = DiscordGatewaySupervisor(
            platform=DiscordPlatform(), platform_client=Ops(), vault=Vault(), redis=None,
            ingest=None, client_factory=Client, simulations=simulations,
        )
        await supervisor.reconcile()
        assert supervisor.held == {"real", "999simulation"}
        assert sorted(started) == ["tok-999simulation", "tok-real"]
        await supervisor.shutdown()
    finally:
        gw.RedisLeaderLock = original


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _until(check, timeout=10.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not await check():
        assert loop.time() < deadline, "timed out"
        await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_discord_run(rsf, monkeypatch):  # noqa: F811
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    monkeypatch.setenv("SUROGATES_SIMULATION_URL", base)

    class Settings:
        class simulation:
            url = base
            internal_token = TOKEN
            channels_url = "http://channels:8003"

    org_id = await make_org(rsf)
    app = FastAPI()
    app.include_router(api.router)
    app.include_router(discord.router)
    app.state.session_factory, app.state.settings = rsf, Settings
    app.state.vault, app.state.provision = FakeVault(), provision_into(rsf)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None))
    serving = asyncio.create_task(server.serve())
    client = httpx.AsyncClient(base_url=base, headers={"Authorization": f"Bearer {TOKEN}"})
    gateway_task = None
    try:
        await _until(lambda: asyncio.sleep(0, server.started))
        run = (await client.post("/internal/runs", json={
            "org_id": str(org_id), "agent_id": "a1", "created_by": "u1",
            "batch_id": str(uuid.uuid4()), "people": [line("Ana")],
            "channel": "discord",
        })).json()
        org, person, app_id = {"org_id": str(org_id)}, run["people"][0], run["phone_number_id"]
        row = await store.get_run(rsf, uuid.UUID(run["id"]))
        assert await store.running_routings(rsf, "discord") == [
            {"channel_identifier": app_id, "org_id": str(org_id), "config": {}}]
        write = f"/internal/runs/{run['id']}/people/{person['id']}/messages"

        # Written before the bot connects: it waits for READY.
        assert (await client.post(write, params=org, json={"text": "Salut"})).json() == {
            "delivered": False}
        seen: list[dict] = []

        async def on_dispatch(event, data, gw_client):
            seen.append({"t": event, "d": data, "bot_user_id": gw_client.bot_user_id})

        bot = DiscordGatewayClient(token=row.access_token, intents=0, on_dispatch=on_dispatch)
        gateway_task = asyncio.create_task(bot.run())
        await _until(lambda: asyncio.sleep(0, len(seen) == 1))
        message = parse(seen[0], bot_user_id=bot.bot_user_id)
        assert bot.bot_user_id == app_id
        assert message.text == "Salut" and message.is_dm and message.platform_user_id == person["address"]

        assert (await client.post(write, params=org, json={"text": "Mă doare spatele"})).json() == {
            "delivered": True}
        await _until(lambda: asyncio.sleep(0, len(seen) == 2))

        platform = DiscordPlatform()
        creds = {"bot_token": row.access_token}
        dm, _ = await open_dm(platform._http, row.access_token, person["address"])
        assert dm == message.identifier
        first = await platform.send(SimpleNamespace(destination={"channel_id": dm},
                                                    payload={"content": "Thinking…"}), creds=creds)
        edited = await platform.send(SimpleNamespace(
            destination={"channel_id": dm, "update_ts": first.message_id},
            payload={"content": "**Bună** ziua"}), creds=creds)
        assert first.success and edited.message_id == first.message_id
        gone = await platform.send(SimpleNamespace(destination={"channel_id": dm},
                                                   payload={"content": "x"}), creds=creds)
        await platform.delete_message(creds=creds, channel=dm, ts=gone.message_id)

        detail = (await client.get(f"/internal/runs/{run['id']}", params=org)).json()
        messages = detail["people"][0]["messages"]
        assert [(m["direction"], m["status"]) for m in messages] == [
            ("in", "sent"), ("in", "sent"), ("out", "sent")]
        assert messages[2]["body"]["content"] == "**Bună** ziua"
        assert messages[2]["body"]["edited_timestamp"]

        await client.post(f"/internal/runs/{run['id']}/end", params=org)
        await asyncio.wait_for(gateway_task, 10)
        assert bot.fatal_code == discord.AUTHENTICATION_FAILED
    finally:
        if gateway_task is not None:
            gateway_task.cancel()
        server.should_exit = True
        await serving
        await client.aclose()


async def test_an_ended_runs_gateway_closes_on_its_next_heartbeat(rsf):  # noqa: F811
    """Ended in another process (the ticker), the run's socket can't be closed
    from there: the bot's next heartbeat closes it."""
    from fastapi import WebSocketDisconnect

    org_id = await make_org(rsf)
    vault = FakeVault()
    run = await store.create_run(rsf, vault, org_id=org_id, agent_id="a1", created_by="u1",
                                 routing_config={}, people=[line("Ana")], provision=provision_into(rsf),
                                 channel="discord")

    class Socket:
        app = SimpleNamespace(state=SimpleNamespace(
            session_factory=rsf, settings=SimpleNamespace(simulation=SimpleNamespace(url="http://s"))))

        def __init__(self):
            self.sent, self.closed = [], None
            self.frames = [{"op": 2, "d": {"token": run.access_token}}, {"op": 1}, "end", {"op": 1}]

        async def accept(self):
            pass

        async def send_json(self, data):
            self.sent.append(data.get("op"))

        async def receive_json(self):
            frame = self.frames.pop(0) if self.frames else None
            if frame == "end":
                await store.end_run(rsf, vault, run.id)
                frame = self.frames.pop(0)
            if frame is None:
                raise WebSocketDisconnect()
            return frame

        async def close(self, code=1000):
            self.closed = code

    ws = Socket()
    await asyncio.wait_for(discord.gateway(ws), 5)
    assert ws.sent == [10, 0, 11]  # HELLO, READY, one heartbeat ACK while running
    assert ws.closed == discord.AUTHENTICATION_FAILED
    assert run.id not in discord.GATEWAYS._sockets
