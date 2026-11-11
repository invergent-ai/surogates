"""Ops starts a run, writes as its person, reads the thread and ends it."""

from __future__ import annotations

import json
import uuid

import httpx
import pytest
import respx
from fastapi import FastAPI
from httpx import ASGITransport

from surogates.simulation.api import router
from tests.test_simulation_models import rsf  # noqa: F401
from tests.test_simulation_store import FakeVault, line, make_org, provision_into

TOKEN = "internal-token"


class Settings:
    class simulation:
        url = "http://simulation:8004"
        channels_url = "http://channels:8003"
        internal_token = TOKEN


async def _client(sf):
    org_id = await make_org(sf)
    app = FastAPI()
    app.include_router(router)
    app.state.session_factory, app.state.settings = sf, Settings
    app.state.vault, app.state.provision = FakeVault(), provision_into(sf)
    app.state.http = httpx.AsyncClient()
    client = httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://simulation:8004",
                               headers={"Authorization": f"Bearer {TOKEN}"})
    return org_id, client


async def _start(client, org_id):
    r = await client.post("/internal/runs", json={
        "org_id": str(org_id), "agent_id": "a1", "created_by": "u1",
        "batch_id": str(uuid.uuid4()), "people": [line("Ana")],
        "routing_config": {}, "flows": [{"id": "f1", "name": "intake", "json": {"screens": []}}],
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

    with respx.mock(assert_all_called=True) as mock:
        hook = mock.post(f"http://channels:8003/whatsapp/{run['phone_number_id']}").mock(
            return_value=httpx.Response(200),
        )
        await client.post(f"/internal/runs/{run['id']}/people/{person['id']}/messages",
                          params=org, json={"quick_reply": {"text": "Confirm", "payload": "yes"}})
    tapped = json.loads(hook.calls[0].request.content)["entry"][0]["changes"][0]["value"]["messages"][0]
    assert tapped["type"] == "button" and tapped["button"] == {"text": "Confirm", "payload": "yes"}

    with respx.mock(assert_all_called=True) as mock:
        hook = mock.post(f"http://channels:8003/whatsapp/{run['phone_number_id']}").mock(
            return_value=httpx.Response(200),
        )
        await client.post(f"/internal/runs/{run['id']}/people/{person['id']}/messages", params=org,
                          json={"form_reply": {"flow_token": "intake:abc", "varsta": "54"}})
    filled = json.loads(hook.calls[0].request.content)["entry"][0]["changes"][0]["value"]["messages"][0]
    reply = filled["interactive"]["nfm_reply"]
    assert filled["interactive"]["type"] == "nfm_reply"
    assert json.loads(reply["response_json"]) == {"flow_token": "intake:abc", "varsta": "54"}

    detail = (await client.get(f"/internal/runs/{run['id']}", params=org)).json()
    message = detail["people"][0]["messages"][0]
    assert message["direction"] == "in" and message["status"] == "delivered"
    assert detail["people"][0]["window_open"] is True
    assert detail["flows"][0]["name"] == "intake"

    listed = (await client.get("/internal/runs", params={**org, "agent_id": "a1"})).json()
    assert [r["id"] for r in listed] == [run["id"]]
    assert listed[0]["messages"] == 3 and listed[0]["driver"] == "human"

    review = {"verdict": "mixed", "summary": "Slow to answer", "findings": [
        {"title": "Never said when", "state": "applied", "applied_at": "2026-11-02T10:00:00+00:00",
         "proposal": {"target": "skill", "name": "echipa"}},
        {"title": "Too long", "state": "dismissed", "proposal": None},
    ]}
    r = await client.put(f"/internal/runs/{run['id']}/review", params=org, json=review)
    assert r.json()["review"] == review
    listed = (await client.get("/internal/runs", params={**org, "agent_id": "a1"})).json()
    assert listed[0]["verdict"] == "mixed"
    assert listed[0]["changes"] == [{"run_id": run["id"], "index": 0, "title": "Never said when",
                                     "target": "skill", "name": "echipa", "person_id": None,
                                     "test": None, "applied_at": "2026-11-02T10:00:00+00:00"}]
    assert listed[0]["ended_at"] is None

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


@pytest.mark.asyncio
async def test_channels_are_the_ones_this_deployment_runs(rsf):  # noqa: F811
    from types import SimpleNamespace as NS

    _, client = await _client(rsf)
    off, on = NS(enabled=False), NS(enabled=True)
    client._transport.app.state.settings = NS(
        simulation=Settings.simulation,
        channels=NS(whatsapp=off, website=off, slack=on, teams=off, discord=on, telegram=off),
        voice=NS(livekit_api_key=""),
    )
    r = await client.get("/internal/channels")
    assert r.status_code == 200
    assert r.json() == {"channels": ["whatsapp", "website", "slack", "discord"]}

    client._transport.app.state.settings.voice.livekit_api_key = "key"
    assert (await client.get("/internal/channels")).json()["channels"][-1] == "voice"


@pytest.mark.asyncio
async def test_a_run_starts_from_plan_lines(rsf):  # noqa: F811
    from tests.test_simulation_store import line

    org_id, client = await _client(rsf)
    batch = str(uuid.uuid4())
    start = {"org_id": str(org_id), "agent_id": "a1", "created_by": "u1", "batch_id": batch}
    r = await client.post("/internal/runs", json={**start, "channel": "voice",
                                                  "people": [line("Ana", autopilot=True)]})
    assert r.status_code == 201
    run = r.json()
    [ana] = run["people"]
    assert run["batch_id"] == batch and ana["driver"] == "human"
    assert (ana["about"], ana["test"], ana["member_id"]) == ("Ana, a customer", "Gets an answer", None)
    r = await client.put(f"/internal/runs/{run['id']}/people/{ana['id']}/driver",
                         params={"org_id": str(org_id)}, json={"driver": "ai"})
    assert r.status_code == 422

    r = await client.post("/internal/runs", json={**start, "channel": "website",
                                                  "people": [line("Bo", autopilot=True)]})
    assert r.json()["people"][0]["driver"] == "human"

    r = await client.post("/internal/runs", json={**start, "people": [
        line("Ioana", member_id=str(uuid.uuid4()))]})
    assert r.status_code == 422

    r = await client.post("/internal/runs", json={**start, "people": []})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_runs_of_one_plan_are_listed_read_and_ended_as_one_simulation(rsf):  # noqa: F811
    from tests.test_simulation_store import line

    from surogates.simulation import store

    org_id, client = await _client(rsf)
    org = {"org_id": str(org_id)}
    # Started before plans: no batch, so it is a batch of its own.
    old = await store.create_run(rsf, FakeVault(), org_id=org_id, agent_id="a1", created_by="u1",
                                 routing_config={}, people=[line("Ana")], provision=provision_into(rsf))
    loose = {"id": str(old.id)}
    batch = str(uuid.uuid4())
    start = {"org_id": str(org_id), "agent_id": "a1", "created_by": "u1", "batch_id": batch}
    chat = (await client.post("/internal/runs", json={
        **start, "people": [line("Ana", test="Books a slot"), line("Bo")]})).json()
    web = (await client.post("/internal/runs", json={
        **start, "channel": "website", "people": [line("Cy")]})).json()
    ana, bo = chat["people"]
    review = {"verdict": "failed", "summary": "", "grades": [
        {"person_id": ana["id"], "passed": True, "reason": "Booked", "message_id": None},
        {"person_id": bo["id"], "passed": False, "reason": "Never answered", "message_id": 7},
    ], "findings": [
        {"title": "Too long", "state": "dismissed", "proposal": None},
        {"title": "Answer Bo", "state": "applied", "applied_at": "2026-11-11T10:00:00+00:00",
         "person_id": bo["id"], "proposal": {"target": "skill", "name": "triaj"}},
    ]}
    await client.put(f"/internal/runs/{chat['id']}/review", params=org, json=review)

    listed = (await client.get("/internal/batches", params={**org, "agent_id": "a1"})).json()
    assert [b["id"] for b in listed] == [batch, loose["id"]]
    plan = listed[0]
    assert (plan["status"], plan["channels"], plan["via"]) == ("running", ["whatsapp", "website"], None)
    assert (plan["passed"], plan["graded"], plan["total"]) == (1, 2, 3)
    people = {p["name"]: p for p in plan["people"]}
    assert people["Ana"] == {"name": "Ana", "channel": "whatsapp", "about": "Ana, a customer",
                             "test": "Books a slot", "member_id": None, "ai_state": None,
                             "grade": {"passed": True, "reason": "Booked", "message_id": None}}
    assert people["Cy"]["grade"] is None and people["Bo"]["grade"]["message_id"] == 7
    assert plan["changes"] == [{"run_id": chat["id"], "index": 1, "title": "Answer Bo",
                                "target": "skill", "name": "triaj", "person_id": bo["id"],
                                "test": "Gets an answer",
                                "applied_at": "2026-11-11T10:00:00+00:00"}]
    assert listed[1]["people"][0]["name"] == "Ana" and listed[1]["total"] == 1

    runs = (await client.get("/internal/runs", params={**org, "agent_id": "a1"})).json()
    summary = next(r for r in runs if r["id"] == chat["id"])
    assert summary["batch_id"] == batch and summary["changes"] == plan["changes"]

    detail = (await client.get(f"/internal/batches/{batch}", params=org)).json()
    assert detail["id"] == batch and [r["id"] for r in detail["runs"]] == [chat["id"], web["id"]]
    assert (await client.get(f"/internal/batches/{batch}",
                             params={"org_id": str(uuid.uuid4())})).status_code == 404
    single = (await client.get(f"/internal/batches/{loose['id']}", params=org)).json()
    assert [r["id"] for r in single["runs"]] == [loose["id"]]

    ended = (await client.post(f"/internal/batches/{batch}/end", params=org)).json()
    assert [r["status"] for r in ended["runs"]] == ["ended", "ended"]
    listed = (await client.get("/internal/batches", params={**org, "agent_id": "a1"})).json()
    assert (listed[0]["status"], listed[1]["status"]) == ("ended", "running")
