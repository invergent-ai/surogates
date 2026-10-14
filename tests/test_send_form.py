"""send_form: an agent's published WhatsApp Flow, sent inside the 24h window."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from surogates.channels.platforms.whatsapp import WhatsAppPlatform
from surogates.channels.whatsapp_flows import forms_from_payload
from surogates.harness.loop import _form_sent
from surogates.session.events import EventType
from surogates.session.store import _build_channel_payload
from surogates.tools.builtin.send_form import SEND_FORM_SCHEMA, describe_forms, handle_send_form

FORM = {
    "name": "vitals", "flow_id": "1187", "title": "Semne vitale", "cta": "Completează",
    "first_screen": "VITALS", "description": "daily blood pressure and pulse",
    "fields": [
        {"name": "systolic", "label": "Tensiune sistolică", "type": "number"},
        {"name": "pulse", "label": "Puls", "type": "number"},
    ],
}
SESSION_ID = uuid.uuid4()


class _Store:
    def __init__(self, channel="whatsapp"):
        self.events = []
        self.session = SimpleNamespace(
            channel=channel, org_id=uuid.uuid4(),
            config={"whatsapp_channel_id": "40712000001", "channel_identifier": "100000000000001"},
        )

    async def get_session(self, session_id):
        return self.session

    async def emit_event(self, session_id, event_type, data):
        self.events.append((event_type, data))
        return len(self.events)


def _kwargs(store, forms=(FORM,)):
    return {
        "session_store": store, "session_id": str(SESSION_ID), "session_factory": object(),
        "session_config": {"whatsapp_forms": list(forms)},
    }


@pytest.fixture
def window(monkeypatch):
    def _set(hours_ago):
        async def last_inbound(*a, **k):
            return datetime.now(timezone.utc) - timedelta(hours=hours_ago)
        monkeypatch.setattr("surogates.followup.store.last_inbound", last_inbound)
    _set(1)
    return _set


def test_forms_default_empty_and_skip_unusable_entries():
    assert forms_from_payload(None) == ()
    assert forms_from_payload([{"name": "x"}, "junk", FORM]) == (FORM,)


def test_the_schema_lists_the_agents_forms_without_touching_the_original():
    from surogates.tools.registry import ToolRegistry
    from surogates.tools.builtin import send_form

    registry = ToolRegistry()
    send_form.register(registry)
    [schema] = registry.get_schemas()
    described = describe_forms(schema, (FORM,))
    assert described["function"]["parameters"]["properties"]["form"]["enum"] == ["vitals"]
    assert "- vitals: Semne vitale — daily blood pressure and pulse" in described["function"]["description"]
    assert "systolic (Tensiune sistolică)" in described["function"]["description"]
    assert "enum" not in SEND_FORM_SCHEMA.parameters["properties"]["form"]


def test_the_schema_tells_the_model_each_choice_fields_answers():
    form = {**FORM, "fields": [
        {"name": "site", "label": "Where?", "type": "Dropdown", "description": "Pick one",
         "options": [{"id": "0_knee", "title": "Knee"}, {"id": "1_hip", "title": "Hip"}]},
    ]}
    schema = {"function": {"description": "d", "parameters": SEND_FORM_SCHEMA.parameters}}
    described = describe_forms(schema, (form,))["function"]["description"]
    assert "site (Where?; Pick one; answers: Knee | Hip)" in described


async def test_a_form_goes_out_as_an_interactive_flow(window):
    store = _Store()
    result = await handle_send_form(
        {"form": "vitals", "message": "Vă rog completați valorile de azi.",
         "prefill": {"pulse": 72, "weight": 80}},
        **_kwargs(store),
    )
    assert result.startswith("Form sent.") and "weight" in result
    [(event_type, data)] = store.events
    assert event_type == EventType.CHANNEL_FORM
    token = data["flow_token"]
    assert token.startswith("vitals:") and data["form"] == "vitals"
    assert data["interactive"] == {
        "type": "flow",
        "body": {"text": "Vă rog completați valorile de azi."},
        "action": {"name": "flow", "parameters": {
            "flow_message_version": "3",
            "flow_token": token,
            "flow_id": "1187",
            "flow_cta": "Completează",
            "flow_action": "navigate",
            "flow_action_payload": {"screen": "VITALS", "data": {"pulse": 72}},
        }},
    }


async def test_an_unknown_form_lists_the_real_ones(window):
    store = _Store()
    result = await handle_send_form({"form": "nope", "message": "x"}, **_kwargs(store))
    assert result == "Not sent: there is no form 'nope'. This agent's forms: vitals."
    assert store.events == []


async def test_a_closed_window_refuses(window):
    window(30)
    store = _Store()
    result = await handle_send_form({"form": "vitals", "message": "x"}, **_kwargs(store))
    assert result.startswith("Not sent: the person has not written in the last 24 hours")
    assert store.events == []


async def test_only_in_a_whatsapp_conversation(window):
    store = _Store(channel="slack")
    result = await handle_send_form({"form": "vitals", "message": "x"}, **_kwargs(store))
    assert result.startswith("Not sent: forms cannot be sent in a")


def test_the_form_event_reaches_only_the_whatsapp_outbox():
    data = {"interactive": {"type": "flow"}, "flow_token": "vitals:x"}
    assert _build_channel_payload(EventType.CHANNEL_FORM, data, "whatsapp") == {"flow": {"type": "flow"}}
    assert _build_channel_payload(EventType.CHANNEL_FORM, data, "slack") == {}


async def test_the_platform_posts_the_flow(monkeypatch):
    sent = {}

    async def _fake_send(client, *, token, phone_number_id, payload, api_version):
        sent.update(payload)
        return "wamid.9", None

    monkeypatch.setattr("surogates.channels.platforms.whatsapp.send_message", _fake_send)
    item = SimpleNamespace(
        destination={"wa_id": "40712000001", "phone_number_id": "127"},
        payload={"flow": {"type": "flow", "body": {"text": "hi"}}},
    )
    result = await WhatsAppPlatform().send(item, creds={"access_token": "T"})
    assert result.success
    assert sent["type"] == "interactive" and sent["interactive"]["type"] == "flow"


def test_only_a_sent_form_ends_the_turn():
    calls = [{"id": "c1", "function": {"name": "send_form"}}]
    assert _form_sent(calls, [{"tool_call_id": "c1", "content": "Form sent. End your turn"}])
    assert not _form_sent(calls, [{"tool_call_id": "c1", "content": "Not sent: no"}])


def test_send_form_is_offered_only_where_there_are_forms():
    from surogates.harness.tool_schemas import drop_unusable_tools

    schemas = [{"function": {"name": n}} for n in ("send_form", "memory")]
    names = lambda **kw: [s["function"]["name"] for s in drop_unusable_tools(  # noqa: E731
        schemas, has_kbs=True, has_channel=True, makes_routines=False, **kw)]
    assert names() == ["memory"]
    assert names(has_forms=True) == ["send_form", "memory"]
