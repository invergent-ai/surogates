"""The local fake Teams signs and checks exactly what the platform verifies and sends."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from surogates.channels.platforms import teams as teams_mod
from surogates.channels.platforms.teams import TeamsPlatform, parse


@pytest.fixture
def fake(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "fake_teams", Path(__file__).parent.parent / "scripts" / "fake_teams.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.config["state_file"] = str(tmp_path / "state.json")
    return module


@pytest.fixture
def platform(fake, monkeypatch):
    # Every platform call — keys, token, connector — goes to the fake in-process.
    monkeypatch.setattr(teams_mod, "TEAMS_OPENID_URL", "http://fake/v1/.well-known/openidconfiguration")
    monkeypatch.setattr(teams_mod, "TEAMS_LOGIN_BASE", "http://fake")
    p = TeamsPlatform()
    p._http = httpx.AsyncClient(transport=httpx.ASGITransport(app=fake.app), base_url="http://fake")
    return p


def _request(token: str, app_id: str) -> SimpleNamespace:
    return SimpleNamespace(headers={"Authorization": f"Bearer {token}"}, path_params={"app_id": app_id})


async def test_an_activity_the_fake_signs_passes_verify_and_parses(fake, platform):
    activity = fake.build_activity("Ana Pop", "hello", conversation="channel", mention=True,
                                   service="http://fake/")
    raw = json.dumps(activity).encode()
    token = fake.mint_token("http://fake/")
    assert await platform.verify(_request(token, fake.APP_ID), raw, creds={}) is True
    # The same token for another bot is refused.
    assert await platform.verify(_request(token, "someone-else"), raw, creds={}) is False

    msg = parse(activity, app_id=fake.APP_ID)
    assert msg.is_mention and msg.text == "hello"
    assert msg.thread_key == activity["id"]


async def test_the_key_survives_a_restart_through_the_state_file(fake, tmp_path):
    token = fake.mint_token("http://fake/")
    saved = json.loads((tmp_path / "state.json").read_text())
    assert saved["pem"] == fake.state["pem"]
    assert token.count(".") == 2


async def test_a_reply_round_trips_through_the_token_and_connector_endpoints(fake, platform):
    activity = fake.build_activity("Ana Pop", "hi", service="http://fake/")
    fake.state["conversations"][activity["conversation"]["id"]] = "personal"
    item = SimpleNamespace(id=1, session_id="s", payload={"content": "**hello** Ana"}, destination={
        "conversation_id": activity["conversation"]["id"], "service_url": "http://fake/",
        "reply_to_id": None, "channel_identifier": fake.APP_ID,
    })
    creds = {"app_password": fake.APP_SECRET, "tenant_id": fake.TENANT_ID}
    result = await platform.send(item, creds=creds)
    assert result.success
    [out] = [m for m in fake.state["messages"] if m["dir"] == "out"]
    assert out["text"] == "**hello** Ana" and out["id"] == result.message_id

    item.destination["update_ts"] = result.message_id
    item.payload["content"] = "edited"
    assert (await platform.send(item, creds=creds)).success
    assert out["text"] == "edited" and out["edited"]

    # A fresh process with a wrong secret gets no token, permanently.
    fresh = TeamsPlatform()
    fresh._http = platform._http
    bad = await fresh.send(item, creds={**creds, "app_password": "wrong"})
    assert not bad.success and "invalid_client" in bad.error


def test_the_connector_refuses_tokens_it_did_not_issue(fake):
    client = TestClient(fake.app)
    fake.state["conversations"]["a:x"] = "personal"
    denied = client.post("/v3/conversations/a%3Ax/activities", json={"type": "message", "text": "x"},
                         headers={"Authorization": "Bearer forged"})
    assert denied.status_code == 401
    issued = client.post(f"/{fake.TENANT_ID}/oauth2/v2.0/token", data={
        "grant_type": "client_credentials", "client_id": fake.APP_ID,
        "client_secret": fake.APP_SECRET, "scope": fake.SCOPE,
    }).json()["access_token"]
    ok = client.post("/v3/conversations/a%3Ax/activities", json={"type": "message", "text": "x"},
                     headers={"Authorization": f"Bearer {issued}"})
    assert ok.status_code == 201 and ok.json()["id"]
    missing = client.post("/v3/conversations/a%3Anope/activities", json={"type": "message"},
                          headers={"Authorization": f"Bearer {issued}"})
    assert missing.status_code == 404


async def test_the_dispatcher_takes_a_signed_activity_from_the_fake_end_to_end(fake, platform):
    from surogates.channels.dispatcher import ChannelWebhookDispatcher
    from surogates.channels.inbound import InboundOutcome
    from surogates.channels.registry import ChannelRegistry

    class _Cache:
        async def get(self, key):
            if key == f"teams:{fake.APP_ID}":
                return {"org_id": "org-1", "agent_id": "agent-1", "config": {}}
            return None

    class _Vault:
        async def resolve_ref(self, ref, *, org_id):
            return None

    handled = []

    class _Pipeline:
        async def handle(self, msg, *, routing, config, deps):
            handled.append(msg)
            return InboundOutcome.DROPPED

    registry = ChannelRegistry()
    registry.register(platform)
    app = ChannelWebhookDispatcher(
        cache=_Cache(), vault=_Vault(), pipeline=_Pipeline(),
        deps_factory=lambda *a: SimpleNamespace(redis=None),
        settings=SimpleNamespace(channels={"teams": SimpleNamespace(enabled=True)}),
        registry=registry,
    ).build_app()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://rt") as c:
        path = f"/teams/{fake.APP_ID}"
        message = fake.build_activity("Ana Pop", "hello", service="http://fake/")
        signed = {"Authorization": f"Bearer {fake.mint_token('http://fake/')}"}
        assert (await c.post(path, json=message, headers=signed)).status_code == 200
        assert (await c.post(path, json=message)).status_code == 401
        install = fake.build_activity("Ana Pop", "", service="http://fake/", kind="conversationUpdate")
        assert (await c.post(path, json=install, headers=signed)).status_code == 200

    [msg] = handled
    assert msg.text == "hello" and msg.is_dm and msg.ts == f"teams:{message['id']}"
