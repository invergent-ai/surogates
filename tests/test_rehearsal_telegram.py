"""A Telegram rehearsal: the person's updates reach the real adapter's route,
and what the adapter sends, edits and reacts with lands in the run."""

from __future__ import annotations

import json
import uuid

import httpx
import pytest
import respx
from fastapi import FastAPI
from httpx import ASGITransport

from surogates.channels.platforms.telegram import _bot_api_url
from surogates.rehearsal import api, store, telegram
from tests.test_rehearsal_api import TOKEN, Settings
from tests.test_rehearsal_models import rsf  # noqa: F401
from tests.test_rehearsal_store import FakeVault, make_org, provision_into


def test_only_a_rehearsal_bot_reaches_the_world(monkeypatch):
    monkeypatch.setenv("SUROGATES_REHEARSAL_URL", "http://rehearsal:8004")
    assert _bot_api_url("9991234567890:abc", "sendMessage") == (
        "http://rehearsal:8004/t/bot9991234567890:abc/sendMessage")
    assert _bot_api_url("7123456789:abc", "sendMessage").startswith("https://api.telegram.org/")
    assert _bot_api_url("999123456:abc", "sendMessage").startswith("https://api.telegram.org/")
    monkeypatch.delenv("SUROGATES_REHEARSAL_URL")
    assert _bot_api_url("9991234567890:abc", "sendMessage").startswith("https://api.telegram.org/")


async def _app(sf):
    org_id = await make_org(sf)
    app = FastAPI()
    app.include_router(api.router)
    app.include_router(telegram.router)
    vault = FakeVault()
    app.state.session_factory, app.state.settings = sf, Settings
    app.state.vault, app.state.provision = vault, provision_into(sf)
    app.state.http = httpx.AsyncClient()
    client = httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://rehearsal:8004",
                               headers={"Authorization": f"Bearer {TOKEN}"})
    return org_id, vault, client


@pytest.mark.asyncio
async def test_telegram_run(rsf):  # noqa: F811
    org_id, vault, client = await _app(rsf)
    run = (await client.post("/internal/runs", json={
        "org_id": str(org_id), "agent_id": "a1", "created_by": "u1", "person_name": "Ana",
        "channel": "telegram",
    })).json()
    org = {"org_id": str(org_id)}
    person = run["people"][0]
    bot = run["phone_number_id"]
    assert run["channel"] == "telegram" and bot.startswith("999") and bot.endswith("_bot")
    row = await store.get_run(rsf, uuid.UUID(run["id"]))
    assert vault.items[(org_id, f"telegram_bot_token_{bot}")] == row.access_token
    routing = await store.routing_for(rsf, "telegram", bot)
    assert routing["agent_id"] == "a1"
    assert await store.routing_for(rsf, "whatsapp", bot) is None

    with respx.mock(assert_all_called=True) as mock:
        hook = mock.post(f"http://channels:8003/telegram/{bot}").mock(
            return_value=httpx.Response(200))
        r = await client.post(f"/internal/runs/{run['id']}/people/{person['id']}/messages",
                              params=org, json={"text": "Salut"})
    assert r.json()["delivered"] is True
    sent = hook.calls[0].request
    assert sent.headers["X-Telegram-Bot-Api-Secret-Token"] == row.app_secret
    update = json.loads(sent.content)
    assert update["message"]["text"] == "Salut"
    assert update["message"]["chat"]["id"] == int(person["address"])

    bot_api = f"/t/bot{row.access_token}"
    keyboard = {"inline_keyboard": [[{"text": "Da", "callback_data": "yes"}]]}
    reply = (await client.post(f"{bot_api}/sendMessage", json={
        "chat_id": int(person["address"]), "text": "<b>Bună</b>", "parse_mode": "HTML",
        "reply_markup": keyboard,
    })).json()
    assert reply["ok"] and reply["result"]["reply_markup"] == keyboard
    message_id = reply["result"]["message_id"]

    edited = (await client.post(f"{bot_api}/editMessageText", json={
        "chat_id": int(person["address"]), "message_id": message_id, "text": "Bună ziua",
    })).json()
    assert edited["result"]["text"] == "Bună ziua" and "reply_markup" not in edited["result"]
    assert (await client.post(f"{bot_api}/setMessageReaction", json={
        "chat_id": int(person["address"]), "message_id": 1,
        "reaction": [{"type": "emoji", "emoji": "👍"}],
    })).json()["ok"]
    unknown = await client.post(f"{bot_api}/sendSticker", json={"chat_id": int(person["address"])})
    assert unknown.status_code == 400 and unknown.json()["ok"] is False

    with respx.mock(assert_all_called=True) as mock:
        hook = mock.post(f"http://channels:8003/telegram/{bot}").mock(
            return_value=httpx.Response(200))
        await client.post(f"/internal/runs/{run['id']}/people/{person['id']}/messages", params=org,
                          json={"callback": {"message_id": str(message_id), "data": "yes"}})
    tap = json.loads(hook.calls[0].request.content)["callback_query"]
    assert tap["data"] == "yes" and tap["message"]["message_id"] == message_id

    detail = (await client.get(f"/internal/runs/{run['id']}", params=org)).json()
    messages = detail["people"][0]["messages"]
    assert [m["direction"] for m in messages] == ["in", "out", "out"]
    assert messages[0]["status"] == "read" and messages[1]["body"]["text"] == "Bună ziua"
    assert messages[2]["body"]["reaction"] == {"message_id": "1", "emoji": "👍"}

    await client.post(f"/internal/runs/{run['id']}/end", params=org)
    after = await client.post(f"{bot_api}/sendMessage", json={"chat_id": 1, "text": "x"})
    assert after.status_code == 401
    assert (org_id, f"telegram_bot_token_{bot}") not in vault.items
