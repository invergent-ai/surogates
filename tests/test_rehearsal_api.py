"""Ops starts a run, writes as its person, reads the thread and ends it."""

from __future__ import annotations

import uuid

import httpx
import pytest
import respx
from fastapi import FastAPI
from httpx import ASGITransport

from surogates.rehearsal.api import router
from tests.test_rehearsal_models import rsf  # noqa: F401
from tests.test_rehearsal_store import FakeVault, make_org, provision_into

TOKEN = "internal-token"


class Settings:
    class rehearsal:
        url = "http://rehearsal:8004"
        channels_url = "http://channels:8003"
        internal_token = TOKEN


async def _client(sf):
    org_id = await make_org(sf)
    app = FastAPI()
    app.include_router(router)
    app.state.session_factory, app.state.settings = sf, Settings
    app.state.vault, app.state.provision = FakeVault(), provision_into(sf)
    app.state.http = httpx.AsyncClient()
    client = httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://rehearsal:8004",
                               headers={"Authorization": f"Bearer {TOKEN}"})
    return org_id, client


async def _start(client, org_id):
    r = await client.post("/internal/runs", json={
        "org_id": str(org_id), "agent_id": "a1", "created_by": "u1", "person_name": "Ana",
        "routing_config": {},
    })
    assert r.status_code == 201
    return r.json()


@pytest.mark.asyncio
async def test_run_lifecycle(rsf):  # noqa: F811
    org_id, client = await _client(rsf)
    run = await _start(client, org_id)
    person = run["people"][0]
    assert person["name"] == "Ana" and person["messages"] == [] and person["window_open"] is False
    org = {"org_id": str(org_id)}

    with respx.mock(assert_all_called=True) as mock:
        hook = mock.post(f"http://channels:8003/whatsapp/{run['phone_number_id']}").mock(
            return_value=httpx.Response(200),
        )
        r = await client.post(f"/internal/runs/{run['id']}/people/{person['id']}/messages",
                              params=org, json={"text": "Bună, vreau o programare"})
    assert r.status_code == 200 and r.json()["delivered"] is True
    sent = hook.calls[0].request.content.decode()
    assert "programare" in sent and person["address"] in sent

    detail = (await client.get(f"/internal/runs/{run['id']}", params=org)).json()
    [message] = detail["people"][0]["messages"]
    assert message["direction"] == "in" and message["status"] == "delivered"
    assert detail["people"][0]["window_open"] is True

    listed = (await client.get("/internal/runs", params={**org, "agent_id": "a1"})).json()
    assert [r["id"] for r in listed] == [run["id"]]

    ended = (await client.post(f"/internal/runs/{run['id']}/end", params=org)).json()
    assert ended["status"] == "ended"
    r = await client.post(f"/internal/runs/{run['id']}/people/{person['id']}/messages",
                          params=org, json={"text": "mai e cineva?"})
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_other_org_and_bad_token_are_refused(rsf):  # noqa: F811
    org_id, client = await _client(rsf)
    run = await _start(client, org_id)
    r = await client.get(f"/internal/runs/{run['id']}", params={"org_id": str(uuid.uuid4())})
    assert r.status_code == 404
    r = await client.get("/internal/runs", params={"org_id": str(org_id), "agent_id": "a1"},
                         headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401
