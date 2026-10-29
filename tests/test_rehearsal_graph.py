"""The world's Graph API answers the runtime's WhatsApp adapter like Meta."""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest
import respx
from fastapi import FastAPI
from httpx import ASGITransport

from surogates.rehearsal import store
from surogates.rehearsal.graph import router
from tests.test_rehearsal_models import rsf  # noqa: F401
from tests.test_rehearsal_store import make_run


class Settings:
    class rehearsal:
        url = "http://rehearsal:8004"
        channels_url = "http://channels:8003"


async def _world(sf):
    run = await make_run(sf)
    [person] = await store.people(sf, run.id)
    app = FastAPI()
    app.include_router(router)
    app.state.session_factory, app.state.settings = sf, Settings
    app.state.http = httpx.AsyncClient()
    client = httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://rehearsal:8004")
    base = f"/w/{run.phone_number_id}/whatsapp/v23.0/{run.phone_number_id}"
    auth = {"Authorization": f"Bearer {run.access_token}"}
    return run, person, client, base, auth


@pytest.mark.asyncio
async def test_send_outside_window_is_refused_like_meta(rsf):  # noqa: F811
    run, person, client, base, auth = await _world(rsf)
    r = await client.post(f"{base}/messages", headers=auth, json={
        "messaging_product": "whatsapp", "to": person.address, "type": "text", "text": {"body": "hi"},
    })
    assert r.status_code == 400 and r.json()["error"]["code"] == 131047


@pytest.mark.asyncio
async def test_send_inside_window_is_logged_and_reported(rsf):  # noqa: F811
    run, person, client, base, auth = await _world(rsf)
    await store.log_message(rsf, run_id=run.id, person_id=person.id, direction="in", kind="text",
                            body={"text": {"body": "salut"}}, wamid="wamid.IN1", status="delivered")
    with respx.mock(assert_all_called=True) as mock:
        hook = mock.post(f"http://channels:8003/whatsapp/{run.phone_number_id}").mock(
            return_value=httpx.Response(200),
        )
        r = await client.post(f"{base}/messages", headers=auth, json={
            "messaging_product": "whatsapp", "to": person.address, "type": "text",
            "text": {"body": "Bună, Ana!"},
        })
    assert r.status_code == 200
    wamid = r.json()["messages"][0]["id"]
    [_, out] = await store.thread(rsf, person.id)
    assert out.wamid == wamid and out.direction == "out" and out.status == "delivered"
    [sent_to] = await store.people(rsf, run.id)
    assert sent_to.agent_typing_at is None
    assert hook.call_count == 2


@pytest.mark.asyncio
async def test_wrong_token_and_other_numbers_are_refused(rsf):  # noqa: F811
    run, person, client, base, _ = await _world(rsf)
    r = await client.post(f"{base}/messages", headers={"Authorization": "Bearer nope"}, json={})
    assert r.status_code == 401 and r.json()["error"]["code"] == 190
    r = await client.post("/w/100000000000001/whatsapp/v23.0/100000000000001/messages",
                          headers={"Authorization": f"Bearer {run.access_token}"}, json={})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_read_receipt_and_media_round_trip(rsf):  # noqa: F811
    run, person, client, base, auth = await _world(rsf)
    await store.log_message(rsf, run_id=run.id, person_id=person.id, direction="in", kind="text",
                            body={}, wamid="wamid.IN1", status="delivered")
    r = await client.post(f"{base}/messages", headers=auth, json={
        "messaging_product": "whatsapp", "status": "read", "message_id": "wamid.IN1",
        "typing_indicator": {"type": "text"},
    })
    assert r.json() == {"success": True}
    assert (await store.thread(rsf, person.id))[0].status == "read"
    [typing] = await store.people(rsf, run.id)
    assert store.is_typing(typing, datetime.now(timezone.utc))

    r = await client.post(f"{base}/media", headers=auth,
                          files={"file": ("a.png", b"png", "image/png")})
    media_id = r.json()["id"]
    world = f"/w/{run.phone_number_id}/whatsapp"
    meta = (await client.get(f"{world}/v23.0/{media_id}", headers=auth)).json()
    assert meta["mime_type"] == "image/png" and meta["file_size"] == 3
    blob = await client.get(meta["url"].removeprefix("http://rehearsal:8004"), headers=auth)
    assert blob.content == b"png"
