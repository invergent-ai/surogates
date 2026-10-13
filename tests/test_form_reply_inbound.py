"""A submitted WhatsApp Flow reaches the agent under its form's labels."""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace
from unittest.mock import AsyncMock

from surogates.channels.inbound import ChannelInboundPipeline, InboundOutcome
from surogates.session.events import EventType
from tests import test_channel_pipeline as pipeline
from tests.test_channel_pipeline import _make_config, _make_deps, _make_msg, _Routing

FORM = {
    "name": "vitals", "flow_id": "1187", "title": "Semne vitale", "cta": "Completează",
    "first_screen": "VITALS",
    "fields": [
        {"name": "systolic", "label": "Tensiune sistolică", "type": "number"},
        {"name": "pulse", "label": "Puls", "type": "number"},
    ],
}


def _flow_msg(token="vitals:abc"):
    return dataclasses.replace(
        _make_msg(is_dm=True, text="pulse: 72\nsystolic: 130", ts="900.0"),
        source={
            "flow_reply": True, "flow_token": token,
            "flow_fields": {"pulse": "72", "systolic": "130", "note": "ok"},
        },
    )


async def _handle(monkeypatch, msg, *, sent_form):
    async def sent_in_session(sf, session_id, token):
        return sent_form if token == "vitals:abc" else None

    monkeypatch.setattr("surogates.channels.whatsapp_flows.sent_in_session", sent_in_session)
    deps = _make_deps()

    async def runtime_config(agent_id):
        return {"whatsapp_forms": [FORM]}

    deps.runtime_config = runtime_config
    result = await ChannelInboundPipeline().handle(
        msg, routing=_Routing(platform="whatsapp", identifier="A0APP"), config=_make_config(), deps=deps,
    )
    said = [d for _, t, d in deps.session_store.events if t == EventType.USER_MESSAGE]
    return result, said, deps


async def test_a_reply_to_a_form_this_conversation_sent_is_labelled(monkeypatch):
    result, said, _ = await _handle(monkeypatch, _flow_msg(), sent_form="vitals")
    assert result == InboundOutcome.PROCESSED
    assert said[-1]["content"] == (
        "Form 'Semne vitale' submitted:\nTensiune sistolică: 130\nPuls: 72\nnote: ok"
    )
    assert said[-1]["source"]["flow_reply"] is True
    assert "flow_token" not in said[-1]["source"]


async def test_an_unknown_token_keeps_the_plain_fields(monkeypatch):
    _, said, _ = await _handle(monkeypatch, _flow_msg(token="vitals:forged"), sent_form="vitals")
    assert said[-1]["content"] == "pulse: 72\nsystolic: 130"
    assert "flow_token" not in said[-1]["source"]


async def test_a_form_reply_answers_an_open_check_in(monkeypatch):
    invitation = SimpleNamespace(
        response_state="awaiting_reply", occurrence_id="occ", program_id="prog", id="inv",
    )
    attach = AsyncMock()
    monkeypatch.setattr("surogates.programs.inbound.open_invitation_for", AsyncMock(return_value=invitation))
    monkeypatch.setattr("surogates.programs.inbound.attach_reply", attach)
    monkeypatch.setattr(
        "surogates.programs.inbound.get_occurrence",
        AsyncMock(return_value=SimpleNamespace(skill_ref="tavi-day-3")),
    )
    checkins = []

    async def emit_synthetic_user_message(self, session_id, *, content, synthetic, metadata=None):
        checkins.append(content)
        return 1

    monkeypatch.setattr(
        pipeline._FakeSessionStore, "emit_synthetic_user_message",
        emit_synthetic_user_message, raising=False,
    )
    result, said, _ = await _handle(monkeypatch, _flow_msg(), sent_form="vitals")
    assert result == InboundOutcome.PROCESSED
    attach.assert_awaited_once()
    assert checkins and checkins[0].startswith("[Check-in] Run the 'tavi-day-3' skill")
    assert said[-1]["content"].startswith("Form 'Semne vitale' submitted:")


async def test_a_token_counts_only_in_the_conversation_that_sent_it(sf):
    import uuid

    from surogates.channels.whatsapp_flows import sent_in_session
    from surogates.db.models import Event

    mine, other = uuid.uuid4(), uuid.uuid4()
    async with sf() as db:
        db.add(Event(session_id=mine, type=EventType.CHANNEL_FORM.value, data={"flow_token": "vitals:abc"}))
        await db.commit()
    assert await sent_in_session(sf, mine, "vitals:abc") == "vitals"
    assert await sent_in_session(sf, other, "vitals:abc") is None
    assert await sent_in_session(sf, mine, "vitals:nope") is None
    assert await sent_in_session(sf, mine, "") is None


def test_answers_read_as_the_options_the_person_picked():
    from surogates.channels.whatsapp_flows import form_reply_text

    form = {"name": "checkin", "title": "Check-in", "fields": [
        {"name": "site", "label": "Unde doare?", "type": "Dropdown",
         "options": [{"id": "0_knee", "title": "Genunchi"}, {"id": "1_hip", "title": "Șold"}]},
        {"name": "meds", "label": "Medicamente", "type": "CheckboxGroup",
         "options": [{"id": "a", "title": "Ibuprofen"}, {"id": "b", "title": "Paracetamol"}]},
        {"name": "ok", "label": "Sunt de acord", "type": "OptIn"},
        {"name": "no", "label": "Contact", "type": "OptIn"},
        {"name": "note", "label": "Notă", "type": "TextArea"},
        {"name": "mood", "label": "Stare", "type": "RadioButtonsGroup"},
    ]}
    assert form_reply_text(form, {
        "site": "1_hip", "meds": ["a", "b"], "ok": True, "no": False, "note": "", "mood": "bine",
    }) == (
        "Form 'Check-in' submitted:\nUnde doare?: Șold\nMedicamente: Ibuprofen, Paracetamol\n"
        "Sunt de acord: Da\nContact: Nu\nNotă: —\nStare: bine"
    )


def test_a_multi_line_answer_continues_on_indented_lines():
    from surogates.channels.whatsapp_flows import form_reply_text

    form = {"name": "n", "title": "Notă", "fields": [
        {"name": "note", "label": "Notă", "type": "TextArea"},
        {"name": "pain", "label": "Durere", "type": "TextInput"},
    ]}
    assert form_reply_text(form, {"note": "Dimineața: bine\r\nSeara: rău", "pain": "3"}) == (
        "Form 'Notă' submitted:\nNotă: Dimineața: bine\n  Seara: rău\nDurere: 3"
    )


async def test_a_program_button_token_names_the_form_by_its_flow_id(monkeypatch):
    async def sent_in_session(sf, session_id, token):
        return token.split(":", 1)[0]

    monkeypatch.setattr("surogates.channels.whatsapp_flows.sent_in_session", sent_in_session)
    deps = _make_deps()

    async def runtime_config(agent_id):
        return {"whatsapp_forms": [FORM]}

    deps.runtime_config = runtime_config
    await ChannelInboundPipeline().handle(
        _flow_msg(token="1187:abc"), routing=_Routing(platform="whatsapp", identifier="A0APP"),
        config=_make_config(), deps=deps,
    )
    said = [d for _, t, d in deps.session_store.events if t == EventType.USER_MESSAGE]
    assert said[-1]["content"].startswith("Form 'Semne vitale' submitted:\nTensiune sistolică: 130")
