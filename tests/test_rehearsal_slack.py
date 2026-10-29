"""A Slack rehearsal: the member's events reach the real adapter's routes signed
as Slack signs, and what the adapter posts, edits, deletes and opens lands in
the run."""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from urllib.parse import parse_qs

import httpx
import pytest
import respx
from fastapi import FastAPI
from httpx import ASGITransport

from surogates.channels.platforms.slack import _api_base
from surogates.rehearsal import api, slack, store
from tests.test_rehearsal_api import TOKEN, Settings
from tests.test_rehearsal_models import rsf  # noqa: F401
from tests.test_rehearsal_store import FakeVault, make_org, provision_into


def test_only_a_rehearsal_token_reaches_the_world(monkeypatch):
    monkeypatch.setenv("SUROGATES_REHEARSAL_URL", "http://rehearsal:8004")
    assert _api_base("xoxb-9991234567890123-abc") == "http://rehearsal:8004/s/api/"
    assert _api_base("xoxb-1234567890-abc").startswith("https://slack.com/")


def _signed_by(request: httpx.Request, secret: str) -> bool:
    ts = request.headers["X-Slack-Request-Timestamp"]
    expected = "v0=" + hmac.new(secret.encode(), b"v0:" + ts.encode() + b":" + request.content,
                                hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, request.headers["X-Slack-Signature"])


@pytest.mark.asyncio
async def test_slack_run(rsf):  # noqa: F811
    org_id = await make_org(rsf)
    app = FastAPI()
    app.include_router(api.router)
    app.include_router(slack.router)
    vault = FakeVault()
    app.state.session_factory, app.state.settings = rsf, Settings
    app.state.vault, app.state.provision = vault, provision_into(rsf)
    app.state.http = httpx.AsyncClient()
    client = httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://rehearsal:8004",
                               headers={"Authorization": f"Bearer {TOKEN}"})
    run = (await client.post("/internal/runs", json={
        "org_id": str(org_id), "agent_id": "a1", "created_by": "u1", "person_name": "Ana",
        "channel": "slack",
    })).json()
    org, person, app_id = {"org_id": str(org_id)}, run["people"][0], run["phone_number_id"]
    row = await store.get_run(rsf, uuid.UUID(run["id"]))
    assert vault.items[(org_id, f"slack_signing_secret_{app_id}")] == row.app_secret
    assert person["address"].startswith("U999")
    assert (await store.routing_for(rsf, "slack", app_id))["agent_id"] == "a1"

    with respx.mock(assert_all_called=True) as mock:
        hook = mock.post(f"http://channels:8003/slack/{app_id}").mock(return_value=httpx.Response(200))
        await client.post(f"/internal/runs/{run['id']}/people/{person['id']}/messages", params=org,
                          json={"text": "Salut"})
    assert _signed_by(hook.calls[0].request, row.app_secret)
    event = json.loads(hook.calls[0].request.content)["event"]
    assert event["text"] == "Salut" and event["channel_type"] == "im"

    web = httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://rehearsal:8004/s/api/",
                            headers={"Authorization": f"Bearer {row.access_token}"})
    assert (await web.post("auth.test")).json()["app_id"] == app_id
    dm = (await web.post("conversations.open", data={"users": person["address"]})).json()["channel"]["id"]
    blocks = [{"type": "actions", "block_id": "q", "elements": [
        {"type": "button", "action_id": "answer", "value": "q1", "text": {"type": "plain_text", "text": "Answer"}},
    ]}]
    posted = (await web.post("chat.postMessage", json={"channel": dm, "text": "_Thinking…_"})).json()
    assert (await web.post("chat.update", json={
        "channel": dm, "ts": posted["ts"], "text": "Ce simptome?", "blocks": blocks,
    })).json()["ok"]
    gone = (await web.post("chat.postMessage", json={"channel": dm, "text": "temp"})).json()
    assert (await web.post("chat.delete", json={"channel": dm, "ts": gone["ts"]})).json()["ok"]
    assert (await web.post("files.upload")).json() == {"ok": False, "error": "unknown_method"}

    with respx.mock(assert_all_called=True) as mock:
        hook = mock.post(f"http://channels:8003/slack/{app_id}/interact").mock(
            return_value=httpx.Response(200))
        await client.post(f"/internal/runs/{run['id']}/people/{person['id']}/messages", params=org,
                          json={"action": {"ts": posted["ts"], "action_id": "answer"}})
    click = json.loads(parse_qs(hook.calls[0].request.content.decode())["payload"][0])
    assert click["type"] == "block_actions" and click["actions"][0]["value"] == "q1"

    view = {"type": "modal", "blocks": [{"type": "input", "block_id": "b1", "element": {
        "type": "plain_text_input", "action_id": "a1"}}]}
    opened = (await web.post("views.open", json={"trigger_id": click["trigger_id"],
                                                  "view": view})).json()["view"]
    with respx.mock(assert_all_called=True) as mock:
        hook = mock.post(f"http://channels:8003/slack/{app_id}/interact").mock(
            return_value=httpx.Response(200))
        await client.post(f"/internal/runs/{run['id']}/people/{person['id']}/messages", params=org,
                          json={"view": {"id": opened["id"], "values": {"b1": "durere"}}})
    submitted = json.loads(parse_qs(hook.calls[0].request.content.decode())["payload"][0])
    assert submitted["view"]["state"]["values"]["b1"]["a1"]["value"] == "durere"

    detail = (await client.get(f"/internal/runs/{run['id']}", params=org)).json()
    messages = detail["people"][0]["messages"]
    assert [(m["direction"], m["kind"]) for m in messages] == [
        ("in", "message"), ("out", "message"), ("out", "view")]
    assert messages[1]["body"]["text"] == "Ce simptome?" and messages[1]["body"]["edited"]
    assert messages[2]["status"] == "closed"
