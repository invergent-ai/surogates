"""A Teams rehearsal: the real adapter verifies the world's activities, logs in
to the world and posts, edits and types into it, and nothing of it reaches a
real App ID."""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import httpx
import pytest
import respx
from fastapi import FastAPI
from httpx import ASGITransport

from surogates.channels.platforms.teams import TeamsPlatform, _rehearsal_base
from surogates.rehearsal import api, store, teams
from surogates.rehearsal.ids import is_rehearsal_teams_app, new_teams_app_id
from tests.test_rehearsal_api import TOKEN, Settings
from tests.test_rehearsal_models import rsf  # noqa: F401
from tests.test_rehearsal_store import FakeVault, make_org, provision_into


def test_only_a_rehearsal_app_id_reaches_the_world(monkeypatch):
    monkeypatch.setenv("SUROGATES_REHEARSAL_URL", "http://rehearsal:8004")
    app_id = new_teams_app_id()
    assert is_rehearsal_teams_app(app_id) and _rehearsal_base(app_id) == "http://rehearsal:8004/m"
    # Entra issues version-4 GUIDs: one that happens to start with 999 is real.
    real = "999" + str(uuid.uuid4())[3:]
    assert _rehearsal_base(real) == ""


def _inbound(request: httpx.Request, app_id: str):
    return SimpleNamespace(headers=request.headers, path_params={"app_id": app_id})


@pytest.mark.asyncio
async def test_teams_run(rsf, monkeypatch):  # noqa: F811
    monkeypatch.setenv("SUROGATES_REHEARSAL_URL", "http://rehearsal:8004")
    org_id = await make_org(rsf)
    app = FastAPI()
    app.include_router(api.router)
    app.include_router(teams.router)
    vault = FakeVault()
    app.state.session_factory, app.state.settings = rsf, Settings
    app.state.vault, app.state.provision = vault, provision_into(rsf)
    app.state.http = httpx.AsyncClient()
    client = httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://rehearsal:8004",
                               headers={"Authorization": f"Bearer {TOKEN}"})
    run = (await client.post("/internal/runs", json={
        "org_id": str(org_id), "agent_id": "a1", "created_by": "u1", "person_name": "Ana",
        "channel": "teams",
    })).json()
    org, person, app_id = {"org_id": str(org_id)}, run["people"][0], run["phone_number_id"]
    row = await store.get_run(rsf, uuid.UUID(run["id"]))
    creds = {"app_password": vault.items[(org_id, f"teams_app_password_{app_id}")],
             "tenant_id": vault.items[(org_id, f"teams_tenant_id_{app_id}")]}
    assert (await store.routing_for(rsf, "teams", app_id))["agent_id"] == "a1"

    with respx.mock(assert_all_called=True) as mock:
        hook = mock.post(f"http://channels:8003/teams/{app_id}").mock(return_value=httpx.Response(200))
        await client.post(f"/internal/runs/{run['id']}/people/{person['id']}/messages", params=org,
                          json={"text": "Salut"})
    sent = hook.calls[0].request
    activity = json.loads(sent.content)
    assert activity["text"] == "Salut" and activity["serviceUrl"] == "http://rehearsal:8004/m"

    platform = TeamsPlatform()
    platform._http = httpx.AsyncClient(transport=ASGITransport(app=app))
    assert await platform.verify(_inbound(sent, app_id), sent.content, creds={})
    forged = json.dumps({**activity, "serviceUrl": "https://evil.example"}).encode()
    assert not await platform.verify(_inbound(sent, app_id), forged, creds={})
    other = new_teams_app_id()
    assert not await platform.verify(_inbound(sent, other), sent.content, creds={})

    message = platform.parse(activity, identifier=app_id)
    assert message.platform_user_id == person["address"] and message.is_dm
    destination = {"conversation_id": message.source["teams_conversation_id"],
                   "service_url": message.source["teams_service_url"], "channel_identifier": app_id}
    assert (await platform._token(app_id, creds))[0] == row.access_token
    platform._tokens.clear()
    assert (await platform._token(app_id, {**creds, "app_password": "x"}))[0] is None

    first = await platform.send(SimpleNamespace(destination=destination,
                                                payload={"content": "_Thinking…_"}), creds=creds)
    edited = await platform.send(SimpleNamespace(
        destination={**destination, "update_ts": first.message_id},
        payload={"content": "**Bună** ziua"}), creds=creds)
    assert first.success and edited.message_id == first.message_id
    url, token, _ = await platform._prepare(destination, creds)
    assert (await platform._call("POST", url, token, {"type": "typing"}))[0] == {}

    detail = (await client.get(f"/internal/runs/{run['id']}", params=org)).json()
    people = detail["people"][0]
    assert [(m["direction"], m["body"]["text"]) for m in people["messages"]] == [
        ("in", "Salut"), ("out", "**Bună** ziua")]
    assert people["messages"][1]["body"]["edited"] and people["agent_typing"]

    await client.post(f"/internal/runs/{run['id']}/end", params=org)
    platform._tokens.clear()
    assert (await platform._token(app_id, creds))[0] is None
