"""The local fake Graph API keeps Flows and answers a filled form like Meta."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from surogates.channels.platforms.whatsapp import parse

FLOW_JSON = {
    "version": "7.2",
    "screens": [{
        "id": "VITALS", "title": "Semne vitale", "terminal": True,
        "data": {"pulse": {"type": "string", "__example__": "70"}},
        "layout": {"type": "SingleColumnLayout", "children": [
            {"type": "TextInput", "name": "pulse", "label": "Puls", "input-type": "number",
             "init-value": "${data.pulse}"},
            {"type": "RadioButtonsGroup", "name": "feel", "label": "Cum vă simțiți?",
             "data-source": [{"id": "good", "title": "Bine"}, {"id": "bad", "title": "Rău"}]},
            {"type": "OptIn", "name": "consent", "label": "Sunt de acord"},
            {"type": "Footer", "label": "Trimite", "on-click-action": {"name": "complete", "payload": {
                "pulse": "${form.pulse}", "feel": "${form.feel}", "consent": "${form.consent}",
            }}},
        ]},
    }],
}


@pytest.fixture
def fake(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "fake_whatsapp", Path(__file__).parent.parent / "scripts" / "fake_whatsapp.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    posted = []

    async def _post_webhook(value):
        posted.append(value)

    monkeypatch.setattr(module, "_post_webhook", _post_webhook)
    module.posted = posted
    return module


def test_a_flow_is_created_uploaded_published_and_read_back(fake):
    client = TestClient(fake.app)
    flow_id = client.post("/v23.0/1234567890/flows", json={"name": "vitals", "categories": ["OTHER"]}).json()["id"]

    upload = client.post(
        f"/v23.0/{flow_id}/assets",
        data={"name": "flow.json", "asset_type": "FLOW_JSON"},
        files={"file": ("flow.json", json.dumps(FLOW_JSON).encode(), "application/json")},
    ).json()
    assert upload == {"success": True, "validation_errors": []}
    assert client.post(f"/v23.0/{flow_id}/publish").json() == {"success": True}

    [row] = client.get("/v23.0/1234567890/flows").json()["data"]
    assert (row["id"], row["status"]) == (flow_id, "PUBLISHED")
    assert client.get(f"/v23.0/{flow_id}", params={"fields": "id,status,json_version"}).json() == {
        "id": flow_id, "status": "PUBLISHED", "json_version": "7.2",
    }
    [asset] = client.get(f"/v23.0/{flow_id}/assets").json()["data"]
    assert client.get(asset["download_url"].replace("http://testserver", "")).json() == FLOW_JSON
    # A published flow is frozen, as on Meta.
    assert client.post(
        f"/v23.0/{flow_id}/assets", files={"file": ("flow.json", b"{}", "application/json")},
    ).status_code == 400


def test_a_filled_form_comes_back_as_meta_nfm_reply(fake):
    client = TestClient(fake.app)
    flow_id = client.post("/v23.0/1234567890/flows", json={
        "name": "vitals", "categories": ["OTHER"], "flow_json": json.dumps(FLOW_JSON), "publish": True,
    }).json()["id"]
    reply = client.post("/phone/api/flow_submit", json={
        "wa_id": "40712000001", "name": "Ion", "flow_id": flow_id, "flow_token": "vitals:abc",
        "data": {"pulse": "70"}, "values": {"pulse": "72", "feel": "good", "consent": True},
        "context_id": "wamid.FORM",
    }).json()
    assert reply["response_json"] == {"pulse": "72", "feel": "good", "consent": True, "flow_token": "vitals:abc"}

    [value] = fake.posted
    [message] = value["messages"]
    assert message["context"]["id"] == "wamid.FORM"
    assert message["interactive"]["type"] == "nfm_reply"
    assert message["interactive"]["nfm_reply"]["name"] == "flow"
    assert message["interactive"]["nfm_reply"]["body"] == "Sent"

    envelope = {"object": "whatsapp_business_account", "entry": [{"id": "1", "changes": [{
        "field": "messages",
        "value": {"metadata": {"phone_number_id": fake.PNID}, **value},
    }]}]}
    inbound = parse(envelope, identifier=fake.PNID)
    assert inbound.source["flow_token"] == "vitals:abc"
    assert inbound.source["flow_fields"] == {"pulse": "72", "feel": "good", "consent": True}


def test_a_flow_message_needs_a_published_flow(fake):
    client = TestClient(fake.app)
    fake.state["messages"].append({"dir": "in", "wa_id": "40712000001", "ts": fake.time.time()})
    sent = client.post("/v23.0/100000000000001/messages", json={
        "messaging_product": "whatsapp", "to": "40712000001", "type": "interactive",
        "interactive": {"type": "flow", "action": {"name": "flow", "parameters": {"flow_id": "nope"}}},
    })
    assert sent.status_code == 400


def test_data_expressions_read_earlier_screens_answers(fake):
    two_screens = {"version": "7.2", "screens": [
        {"id": "A", "layout": {"children": [
            {"type": "TextInput", "name": "name", "label": "Nume"},
            {"type": "Footer", "label": "Next", "on-click-action": {
                "name": "navigate", "next": {"type": "screen", "name": "B"}, "payload": {"name": "${form.name}"}}},
        ]}},
        {"id": "B", "terminal": True, "layout": {"children": [
            {"type": "DatePicker", "name": "date", "label": "Data"},
            {"type": "Footer", "label": "Gata", "on-click-action": {"name": "complete", "payload": {
                "name": "${data.name}", "date": "${form.date}", "source": "fake"}}},
        ]}},
    ]}
    assert fake.flow_response(two_screens, {"name": "Ion", "date": "2026-10-13"}, {}, "t") == {
        "name": "Ion", "date": "2026-10-13", "source": "fake", "flow_token": "t",
    }


def test_the_phone_sees_ticks_typing_and_quoted_button_replies(fake):
    client = TestClient(fake.app)
    client.post("/phone/api/send", json={"wa_id": "40712000001", "text": "Bună"})
    client.post("/phone/api/send", json={
        "wa_id": "40712000001", "template_button": "Trimite", "template_payload": "send_report",
        "context_id": "wamid.TPL",
    })
    first, tapped = fake.state["messages"]
    assert tapped["body"]["button"] == {"text": "Trimite", "payload": "send_report"}
    assert tapped["body"]["context"]["id"] == "wamid.TPL"

    client.post("/v23.0/100000000000001/messages", json={
        "messaging_product": "whatsapp", "status": "read", "message_id": tapped["id"],
        "typing_indicator": {"type": "text"},
    })
    thread = client.get("/phone/api/thread", params={"wa_id": "40712000001"}).json()
    assert [m["status"] for m in thread["messages"]] == ["read", "read"]
    assert thread["typing"] is True
    assert client.get("/phone/api/thread", params={"wa_id": "40712000002"}).json()["typing"] is False
    assert client.get("/phone/api/contacts").json() == {"contacts": ["40712000001"]}
