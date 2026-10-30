"""A voice rehearsal: Studio phones the run's +999 number through LiveKit, as
a SIP caller would, and the voice worker keeps the call inside the run."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport
from livekit import api as lk

from surogates.rehearsal import api, store
from surogates.rehearsal.ids import is_rehearsal_id, is_rehearsal_voice_number, new_voice_number
from surogates.voice import sessions as voice_sessions
from surogates.voice.sessions import CallTarget, VoiceSessions
from surogates.voice.worker import call_info, rehearsal_of
from tests.test_rehearsal_api import TOKEN
from tests.test_rehearsal_models import rsf  # noqa: F401
from tests.test_rehearsal_store import FakeVault, make_org, provision_into


class Settings:
    class rehearsal:
        url = "http://rehearsal:8004"
        channels_url = "http://channels:8003"
        internal_token = TOKEN
        livekit_public_url = "wss://calls.example"

    class voice:
        livekit_url = "ws://livekit:7880"
        livekit_api_key = "devkey"
        livekit_api_secret = "a-secret-long-enough-for-hs256-signing"
        agent_name = "surogate-voice"


def test_only_a_reserved_number_routes_from_the_runtime():
    number = new_voice_number()
    assert is_rehearsal_voice_number(number) and is_rehearsal_id(number)
    assert not is_rehearsal_voice_number("+40722000111")
    assert not is_rehearsal_voice_number("+9991234")


@pytest.mark.asyncio
async def test_voice_run_places_a_call_the_worker_reads_as_sip(rsf):  # noqa: F811
    org_id = await make_org(rsf)
    app = FastAPI()
    app.include_router(api.router)
    app.state.session_factory, app.state.settings = rsf, Settings
    app.state.vault, app.state.provision = FakeVault(), provision_into(rsf)
    app.state.http = httpx.AsyncClient()
    client = httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://rehearsal",
                               headers={"Authorization": f"Bearer {TOKEN}"})
    run = (await client.post("/internal/runs", json={
        "org_id": str(org_id), "agent_id": "a1", "created_by": "u1", "person_name": "Ana",
        "channel": "voice",
    })).json()
    number, person, org = run["phone_number_id"], run["people"][0], {"org_id": str(org_id)}
    routing = await store.routing_for(rsf, "voice", number)
    assert routing["agent_id"] == "a1" and rehearsal_of(routing) == run["id"]

    base = f"/internal/runs/{run['id']}/people/{person['id']}"
    assert (await client.post(f"{base}/messages", params=org, json={"text": "x"})).status_code == 422
    placed = (await client.post(f"{base}/call", params=org)).json()
    assert placed["url"] == "wss://calls.example"
    claims = lk.TokenVerifier(Settings.voice.livekit_api_key, Settings.voice.livekit_api_secret).verify(
        placed["token"])
    assert claims.video.room == placed["room"] and placed["room"].startswith(f"rehearsal-{run['id']}-")
    assert claims.room_config.agents[0].agent_name == "surogate-voice"
    # The worker reads the browser participant exactly as a SIP caller.
    info = call_info(placed["room"], claims.attributes)
    assert (info.called, info.caller, info.call_id) == (number, person["address"], placed["room"])

    await client.post(f"/internal/runs/{run['id']}/end", params=org)
    assert (await client.post(f"{base}/call", params=org)).status_code == 409


@pytest.mark.asyncio
async def test_a_rehearsal_call_stays_in_its_run(monkeypatch):
    seen: dict = {}

    async def identity(sf, *, platform, platform_user_id, org_id, display_name=""):
        return SimpleNamespace(user_id=uuid.uuid4())

    async def session(store, redis, *, session_key, config, channel, **kw):
        seen["config"] = config
        return uuid.uuid4()

    async def mark(sf, user_id, run_id):
        seen["marked"] = run_id

    monkeypatch.setattr(voice_sessions, "get_or_create_channel_identity", identity)
    monkeypatch.setattr(voice_sessions, "get_or_create_channel_session", session)
    monkeypatch.setattr(voice_sessions, "_mark_rehearsal", mark)
    run_id = str(uuid.uuid4())
    target = CallTarget(org_id=uuid.uuid4(), agent_id="a1", rehearsal_run_id=run_id)
    await VoiceSessions(store=None, redis=None, session_factory=None).open_call(
        target, call_id="rehearsal-x", called="+9990000000001", caller="+9991111111111")
    assert seen["config"]["memory_boundary"] == f"rehearsal:{run_id}:voice:call:rehearsal-x"
    assert seen["marked"] == run_id
