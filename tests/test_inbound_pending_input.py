import dataclasses
import uuid
from unittest.mock import AsyncMock

import pytest

from surogates.channels.inbound import ChannelInboundPipeline, InboundOutcome
from surogates.session.events import EventType

from tests.test_channel_pipeline import (
    SESSION_ID,
    USER_ID,
    _make_config,
    _make_deps,
    _make_msg,
    _make_routing,
    _Routing,
)


_CONSENT = {
    "tool_call_id": "tc1",
    "questions": [{
        "prompt": "Sunteți de acord să vă trimit întrebările domnului doctor?",
        "choices": [{"label": "Da, sunt de acord"}, {"label": "Nu acum"}],
    }],
    "context": "",
    "user_id": USER_ID,
}
_OPEN = {"tool_call_id": "tc1", "questions": [{"prompt": "Când a fost procedura?"}], "context": "", "user_id": USER_ID}


async def _handle_while_pending(monkeypatch, pending, msg, platform="whatsapp"):
    deps = _make_deps()

    async def pending_input(session_id):
        return pending

    deps.pending_input = pending_input
    resolve = AsyncMock(return_value=True)
    monkeypatch.setattr("surogates.session.interactive_input.resolve_input_response", resolve)
    result = await ChannelInboundPipeline().handle(
        msg, routing=_Routing(platform=platform, identifier="A0APP"), config=_make_config(), deps=deps,
    )
    said = [data for _, event_type, data in deps.session_store.events if event_type == EventType.USER_MESSAGE]
    return result, resolve, said


@pytest.mark.parametrize("platform", ["whatsapp", "telegram", "slack"])
@pytest.mark.parametrize("pending, text", [
    (_CONSENT, "da, sunt de acord"),
    (_CONSENT, "NU ACUM"),
    (_OPEN, "acum 5 zile"),
])
async def test_a_choice_or_an_open_answer_resolves_the_question(monkeypatch, platform, pending, text):
    result, resolve, said = await _handle_while_pending(
        monkeypatch, pending, _make_msg(is_dm=True, text=text, ts="710.0"), platform,
    )
    assert (result, said) == (InboundOutcome.DROPPED, [])
    resolve.assert_awaited_once()


@pytest.mark.parametrize("platform", ["whatsapp", "telegram", "slack"])
async def test_anything_else_dismisses_the_question_and_reaches_the_agent(monkeypatch, platform):
    """"Can I stop the aspirin?" under a consent question was recorded as an
    "other" answer, and the agent just asked for consent again."""
    result, resolve, said = await _handle_while_pending(
        monkeypatch, _CONSENT,
        _make_msg(is_dm=True, text="Pot să opresc aspirina câteva zile?", ts="711.0"), platform,
    )
    assert result == InboundOutcome.PROCESSED
    resolve.assert_not_awaited()
    assert [(d["content"], d["dismisses_question"]) for d in said] == [
        ("Pot să opresc aspirina câteva zile?", "tc1"),
    ]


async def test_a_photo_is_never_an_answer(monkeypatch):
    """A discharge letter's photo reached the agent as its caption only."""
    from surogates.channels.inbound import InboundFileRef

    msg = dataclasses.replace(_make_msg(is_dm=True, text="acum 5 zile", ts="712.0", kind="image"), files=[InboundFileRef(url="media1", filename="scrisoare.jpg", mime_type="image/jpeg", size=None, file_id="media1")])
    result, resolve, said = await _handle_while_pending(monkeypatch, _OPEN, msg)
    assert result == InboundOutcome.PROCESSED
    resolve.assert_not_awaited()
    assert said[0]["dismisses_question"] == "tc1"
    assert said[0]["source"]["files"] == [{"id": "media1", "name": "scrisoare.jpg"}]


async def test_a_flow_reply_is_never_an_answer(monkeypatch):
    msg = dataclasses.replace(_make_msg(is_dm=True, text="nume: Ion", ts="713.0"), source={"flow_reply": True})
    result, resolve, said = await _handle_while_pending(monkeypatch, _OPEN, msg)
    assert result == InboundOutcome.PROCESSED
    resolve.assert_not_awaited()


async def test_no_pending_input_preserves_normal_turn():
    deps = _make_deps()
    nudge_calls = []

    async def pending_input(session_id):
        return None

    async def input_nudge(session_id, msg, text):
        nudge_calls.append((session_id, text))

    deps.pending_input = pending_input
    deps.input_nudge = input_nudge

    msg = _make_msg(is_dm=True, identifier="D1", ts="701.0")
    result = await ChannelInboundPipeline().handle(
        msg,
        routing=_make_routing(),
        config=_make_config(),
        deps=deps,
    )

    assert result == InboundOutcome.PROCESSED
    assert nudge_calls == []
    assert deps._enqueued
    assert any(event_type == EventType.USER_MESSAGE for _, event_type, _ in deps.session_store.events)


@pytest.mark.parametrize("platform, text", [
    ("slack", "/goal status"),
    # Slack takes a message that starts with "/" for its own command: its users type a space first.
    ("slack", " /goal status"),
    ("telegram", "/goal status"),
    ("whatsapp", "/goal status"),
])
async def test_a_command_typed_while_a_question_waits_goes_on_as_a_message(monkeypatch, platform, text):
    deps = _make_deps()
    nudges = []

    async def pending_input(session_id):
        return {"tool_call_id": "tc1", "questions": [{"prompt": "Which quarter?"}], "context": "", "user_id": USER_ID}

    async def input_nudge(session_id, msg, said):
        nudges.append(said)

    deps.pending_input = pending_input
    deps.input_nudge = input_nudge
    resolve = AsyncMock(return_value=True)
    monkeypatch.setattr("surogates.session.interactive_input.resolve_input_response", resolve)

    result = await ChannelInboundPipeline().handle(
        _make_msg(is_dm=True, text=text, ts="702.0"),
        routing=_Routing(platform=platform, identifier="A0APP"),
        config=_make_config(),
        deps=deps,
    )

    # Never the question's answer, nor held back for it: written as its user's message, whose
    # command the question's wait reads, and its wake runs.
    assert (result, nudges) == (InboundOutcome.PROCESSED, [])
    resolve.assert_not_awaited()
    said = [data for _, event_type, data in deps.session_store.events if event_type == EventType.USER_MESSAGE]
    assert [data["content"] for data in said] == [text]
    assert deps._enqueued


@pytest.mark.parametrize("pending, expected", [
    # An answer resumes the waiting turn: the person sees it working again.
    ({"tool_call_id": "tc1", "questions": [{"prompt": "Da sau nu?"}], "context": "", "user_id": USER_ID}, InboundOutcome.DROPPED),
    (None, InboundOutcome.PROCESSED),
])
async def test_typing_starts_for_a_new_turn_and_for_an_answer(monkeypatch, pending, expected):
    deps = _make_deps()
    typing = []

    async def pending_input(session_id):
        return pending

    async def keep_typing(session_id, msg):
        typing.append(session_id)

    deps.pending_input = pending_input
    deps.keep_typing = keep_typing
    monkeypatch.setattr(
        "surogates.session.interactive_input.resolve_input_response", AsyncMock(return_value=True),
    )

    result = await ChannelInboundPipeline().handle(
        _make_msg(is_dm=True, text="Da", ts="703.0"),
        routing=_Routing(platform="whatsapp", identifier="A0APP"),
        config=_make_config(),
        deps=deps,
    )

    assert (result, len(typing)) == (expected, 1)


@pytest.mark.parametrize("platform", ["whatsapp", "telegram", "slack"])
@pytest.mark.parametrize("text", ["Da, sunt de acord", "Pot să opresc aspirina?"])
async def test_only_the_person_asked_answers_or_dismisses_the_question(monkeypatch, platform, text):
    """In a shared Slack thread a colleague's message answered or dismissed the
    question the agent had asked someone else."""
    asked_someone_else = {**_CONSENT, "user_id": uuid.uuid4()}
    result, resolve, said = await _handle_while_pending(
        monkeypatch, asked_someone_else, _make_msg(is_dm=True, text=text, ts="714.0"), platform,
    )
    assert result == InboundOutcome.PROCESSED
    resolve.assert_not_awaited()
    assert [d["content"] for d in said] == [text]
    assert "dismisses_question" not in said[0]


@pytest.mark.parametrize("platform", ["whatsapp", "telegram", "slack"])
@pytest.mark.parametrize("text", ["acum 5 zile", "Da, sunt de acord"])
async def test_a_reply_to_several_questions_reaches_the_agent_as_the_persons_message(
    monkeypatch, platform, text,
):
    """One reply cannot be split between several questions: it went to each of
    them as the same answer.  It is the person's message, and the agent reads it."""
    several = {**_OPEN, "questions": [*_OPEN["questions"], *_CONSENT["questions"]]}
    result, resolve, said = await _handle_while_pending(
        monkeypatch, several, _make_msg(is_dm=True, text=text, ts="715.0"), platform,
    )
    assert result == InboundOutcome.PROCESSED
    resolve.assert_not_awaited()
    assert [(d["content"], d["dismisses_question"]) for d in said] == [(text, "tc1")]
