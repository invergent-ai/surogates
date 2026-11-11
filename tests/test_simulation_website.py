"""A website simulation: the run's key routes from the runtime DB through the
API's own cache, and the visitor's session lands in the run's memory."""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
import sqlalchemy as sa
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from httpx import ASGITransport

from surogates.api.app import build_channel_routing_cache
from surogates.api.routes import website
from surogates.channels.memory_boundary import SIMULATION_BOUNDARY_PREFIX
from surogates.channels.website_keys import generate_publishable_key
from surogates.db.models import Event, Session
from surogates.simulation import actor, api, store
from surogates.simulation import website as simulation_website
from surogates.simulation.ids import (
    is_simulation_id,
    is_simulation_website_key,
    new_website_key,
)
from tests.api.test_website_multi_session import _ORIGIN, _settings, _Storage, _Store
from tests.test_simulation_api import TOKEN, Settings
from tests.test_simulation_models import rsf  # noqa: F401
from tests.test_simulation_store import FakeVault, line, make_org, provision_into


def test_only_a_simulation_key_routes_from_the_runtime():
    key = new_website_key()
    assert is_simulation_website_key(key) and is_simulation_id(key)
    real = generate_publishable_key()
    assert not is_simulation_website_key(real)
    # A real key may happen to start like ours; its length never matches.
    assert not is_simulation_website_key("surg_wk_999r" + real[12:])


class _Ops:
    def __init__(self):
        self.asked: list[str] = []

    async def get_channel_routing(self, kind, identifier):
        self.asked.append(identifier)
        return None


@pytest.mark.asyncio
async def test_website_run(rsf):  # noqa: F811
    org_id = await make_org(rsf)
    internal = FastAPI()
    internal.include_router(api.router)
    internal.state.session_factory, internal.state.settings = rsf, Settings
    internal.state.vault, internal.state.provision = FakeVault(), provision_into(rsf)
    internal.state.http = httpx.AsyncClient()
    client = httpx.AsyncClient(transport=ASGITransport(app=internal), base_url="http://simulation",
                               headers={"Authorization": f"Bearer {TOKEN}"})
    run = (await client.post("/internal/runs", json={
        "org_id": str(org_id), "agent_id": "hero-agent", "created_by": "u1",
        "batch_id": str(uuid.uuid4()), "people": [line("Ana")], "channel": "website",
        "routing_config": {"allowed_origins": [_ORIGIN]},
    })).json()
    key, person, org = run["phone_number_id"], run["people"][0], {"org_id": str(org_id)}
    assert (person["address"], person["driver"]) == ("visitor", "human")

    ops, sessions = _Ops(), _Store()
    app = FastAPI()
    app.include_router(website.router, prefix="/v1")
    app.state.settings, app.state.session_store, app.state.storage = _settings(), sessions, _Storage()
    app.state.channel_routing_cache = build_channel_routing_cache(
        settings=None, platform_client=ops, session_factory=rsf)
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="https://widget") as w:
        r = await w.post("/v1/website/sessions",
                         headers={"Authorization": f"Bearer {key}", "Origin": _ORIGIN})
        assert r.status_code == 201, r.text
        config = sessions.created[0]["config"]
        assert config["simulation_run_id"] == run["id"]
        assert config["memory_boundary"].startswith(f"{SIMULATION_BOUNDARY_PREFIX}{run['id']}:visitor:")
        assert ops.asked == []

        await client.post(f"/internal/runs/{run['id']}/end", params=org)
        app.state.channel_routing_cache = build_channel_routing_cache(
            settings=None, platform_client=ops, session_factory=rsf)
        r = await w.post("/v1/website/sessions",
                         headers={"Authorization": f"Bearer {key}", "Origin": _ORIGIN})
        assert r.status_code == 404 and ops.asked == [key]


@pytest.mark.asyncio
async def test_a_simulation_visitor_never_meets_the_paywall():
    from types import SimpleNamespace

    from surogates.api.routes._commerce_turn import authorize_commerce_turn

    session = SimpleNamespace(agent_id="a1", id="s1", config={"simulation_run_id": "r1"})
    # No app state at all: the gate must not even ask ops for the agent's mode.
    assert await authorize_commerce_turn(SimpleNamespace(app=None), session, "Salut") is None


class _Sessions:
    """The runtime's session store, as far as the widget's routes use it, on the test DB."""

    def __init__(self, sf):
        self.sf = sf

    async def create_session(self, *, session_id, org_id, agent_id, channel, config, **_):
        row = Session(id=session_id, org_id=org_id, agent_id=agent_id, channel=channel,
                      status="active", config=config)
        async with self.sf() as db:
            db.add(row)
            await db.commit()
        return row

    async def get_session(self, session_id):
        from surogates.session.store import SessionNotFoundError

        async with self.sf() as db:
            row = await db.get(Session, session_id)
        if row is None:
            raise SessionNotFoundError(str(session_id))
        row.message_count = 0
        return row

    async def emit_event(self, session_id, type, data):
        event = Event(session_id=session_id, type=str(getattr(type, "value", type)), data=data)
        async with self.sf() as db:
            db.add(event)
            await db.commit()
        return event.id

    async def update_session_status(self, session_id, status):
        return None

    async def update_session_config_key(self, session_id, key, value):
        return None


async def _visitor_world(rsf, *, autopilot=True):  # noqa: F811
    """A website run with one person, and the runtime's widget routes the simulation reaches."""
    from unittest.mock import AsyncMock

    widget = FastAPI()
    widget.state.answers, widget.state.answer_status = [], 201

    # The real route needs Postgres (an advisory lock); this one checks what the widget sends.
    @widget.post("/v1/website/sessions/{session_id}/ask_user_question/{tool_call_id}/respond")
    async def respond(session_id: uuid.UUID, tool_call_id: str, request: Request):
        widget.state.answers.append((tool_call_id, await request.json(), request.headers))
        if widget.state.answer_status != 201:
            return JSONResponse({"detail": "gone"}, status_code=widget.state.answer_status)
        event = Event(session_id=session_id, type="ask_user_question.response",
                      data={"tool_call_id": tool_call_id})
        async with rsf() as db:
            db.add(event)
            await db.commit()
        return JSONResponse({"event_id": event.id}, status_code=201)

    widget.include_router(website.router, prefix="/v1")
    widget.state.settings, widget.state.storage = _settings(), _Storage()
    widget.state.session_store = _Sessions(rsf)
    widget.state.redis = SimpleNamespace(zadd=AsyncMock())
    widget.state.channel_routing_cache = build_channel_routing_cache(
        settings=None, platform_client=_Ops(), session_factory=rsf)
    org_id = await make_org(rsf)
    state = SimpleNamespace(session_factory=rsf, settings=Settings, vault=FakeVault(),
                            provision=provision_into(rsf),
                            http=httpx.AsyncClient(transport=ASGITransport(app=widget)))
    run = await store.create_run(
        rsf, state.vault, org_id=org_id, agent_id="hero-agent", created_by="u1", channel="website",
        routing_config={"allowed_origins": [_ORIGIN], "website_api_url": "https://runtime/"},
        people=[line("Ana", autopilot=autopilot)], provision=state.provision,
    )
    [person] = await store.people(rsf, run.id)
    state.widget = widget
    return state, run, person


async def _agent_says(sf, session_id, *texts):
    async with sf() as db:
        for text in texts:
            db.add(Event(session_id=session_id, type="llm.response",
                         data={"message": {"role": "assistant", "content": text}}))
        db.add(Event(session_id=session_id, type="session.complete", data={}))
        await db.commit()


@pytest.mark.asyncio
async def test_autopilot_writes_as_a_visitor_and_reads_the_agent_once(rsf):  # noqa: F811
    state, run, person = await _visitor_world(rsf)
    assert (person.driver, person.ai_state) == ("ai", "talking")

    sent = await simulation_website.send(state, run, person, "Salut")
    assert sent == {"delivered": True, "channels_status": 202}
    [person] = await store.people(rsf, run.id)
    session_id = uuid.UUID(simulation_website.visitor_session(person)[0])
    async with rsf() as db:
        session = await db.get(Session, session_id)
        said = (await db.execute(sa.select(Event).where(Event.type == "user.message"))).scalars().all()
    assert session.config["simulation_run_id"] == str(run.id)
    assert [e.data["content"] for e in said] == ["Salut"]
    [written] = await store.thread(rsf, person.id)
    assert (written.direction, written.body, written.wamid, written.status) == (
        "in", {"text": "Salut"}, f"web:{said[0].id}", "delivered")
    assert await store.agent_working(rsf, person)

    # The same visitor writes again in the same conversation.
    await simulation_website.send(state, run, person, "Mai sunteți?")
    async with rsf() as db:
        assert len((await db.execute(sa.select(Session))).scalars().all()) == 1

    await _agent_says(rsf, session_id, "Bună! Cu ce vă ajut?", "")
    assert not await store.agent_working(rsf, person)
    assert await store.agent_turn_ended_at(rsf, person) is not None
    await simulation_website.collect(state, run, person)
    await simulation_website.collect(state, run, person)
    out = [m for m in await store.thread(rsf, person.id) if m.direction == "out"]
    assert [m.body for m in out] == [{"text": "Bună! Cu ce vă ajut?"}]


@pytest.mark.asyncio
async def test_autopilot_plays_a_visitor_end_to_end(rsf, monkeypatch):  # noqa: F811
    state, run, _ = await _visitor_world(rsf)
    moves = [actor.Move(action="write", text="Aveți programări mâine?"),
             actor.Move(action="done", note="Got a slot")]
    prompts = []

    async def generate(**kw):
        prompts.append(kw["messages"])
        return moves.pop(0)

    class _LLM:
        async def close(self):
            pass

    monkeypatch.setattr(actor, "generate_structured", generate)
    monkeypatch.setattr(actor, "_client", lambda settings: (_LLM(), "m"))
    monkeypatch.setattr(actor, "QUIET", timedelta(0))
    ai = actor.Actor(state)

    [(run, person)] = await store.ai_people(rsf)
    await ai._move(run, person)
    [(run, person)] = await store.ai_people(rsf)
    await ai._move(run, person)
    assert len(prompts) == 1

    await _agent_says(rsf, uuid.UUID(simulation_website.visitor_session(person)[0]),
                      "Da, la ora 10.")
    [(run, person)] = await store.ai_people(rsf)
    await ai._move(run, person)
    assert "la ora 10" in prompts[1][1]["content"]
    [p] = await store.people(rsf, run.id)
    assert (p.ai_state, p.ai_note) == ("done", "Got a slot")


@pytest.mark.asyncio
async def test_a_visitor_writes_text_only(rsf):  # noqa: F811
    state, run, person = await _visitor_world(rsf)
    with pytest.raises(HTTPException) as refused:
        await api.write(state, run, person, api.PersonMessage(button_reply={"id": "a", "title": "A"}))
    assert refused.value.status_code == 422


async def _agent_asks(sf, session_id, tool_call_id, questions, **extra):
    async with sf() as db:
        db.add(Event(session_id=session_id, type="tool.call", data={
            "tool_call_id": tool_call_id, "name": "ask_user_question",
            "arguments": {"questions": questions, **extra}}))
        db.add(Event(session_id=session_id, type="inbox.input_required",
                     data={"tool_call_id": tool_call_id}))
        await db.commit()


_DAY = {"prompt": "Ce zi?", "choices": [{"label": "Luni"}, {"label": "Marți"}]}


@pytest.mark.asyncio
async def test_the_agents_questions_are_logged_and_answered_as_the_widget_answers(rsf):  # noqa: F811
    state, run, person = await _visitor_world(rsf)
    await simulation_website.send(state, run, person, "Vreau o programare")
    [person] = await store.people(rsf, run.id)
    session_id = uuid.UUID(simulation_website.visitor_session(person)[0])
    await _agent_asks(rsf, session_id, "call_1", [_DAY], context="Avem locuri săptămâna asta.")
    await simulation_website.collect(state, run, person)
    await simulation_website.collect(state, run, person)
    [asked] = [m for m in await store.thread(rsf, person.id) if m.direction == "out"]
    assert (asked.kind, asked.status) == ("question", "")
    assert asked.body == {"tool_call_id": "call_1", "context": "Avem locuri săptămâna asta.",
                          "questions": [{"prompt": "Ce zi?", "choices": ["Luni", "Marți"],
                                         "allow_other": True}]}
    # The agent stays owed nothing while it waits on the visitor's answer.
    assert not await store.agent_working(rsf, person)

    # Studio's composer, for a visitor played by hand: the tap goes through the runs API.
    client = httpx.AsyncClient(transport=ASGITransport(app=_internal(rsf, state)), base_url="http://s",
                               headers={"Authorization": f"Bearer {TOKEN}"})
    r = await client.post(f"/internal/runs/{run.id}/people/{person.id}/messages",
                          params={"org_id": str(run.org_id)}, json={"answer": {
                              "tool_call_id": "call_1",
                              "responses": [{"question": "Ce zi?", "answer": "Marți"}]}})
    assert r.json() == {"delivered": True, "channels_status": 201}
    [(tool_call_id, sent, headers)] = state.widget.state.answers
    assert tool_call_id == "call_1" and sent == {"responses": [
        {"question": "Ce zi?", "answer": "Marți", "is_other": False}]}
    assert headers["origin"] == _ORIGIN and headers["x-csrf-token"]
    thread = await store.thread(rsf, person.id)
    assert [(m.kind, m.status) for m in thread] == [("text", "delivered"), ("question", "answered"),
                                                   ("answer", "delivered")]
    assert thread[-1].body["text"] == "Ce zi?: Marți"

    # Answered already elsewhere: no harm, no refusal.
    state.widget.state.answer_status = 409
    again = await api.write(state, run, person, api.PersonMessage(answer={
        "tool_call_id": "call_1", "responses": [{"question": "Ce zi?", "answer": "Luni"}]}))
    assert again["already_answered"]


@pytest.mark.asyncio
async def test_text_while_a_question_waits_is_its_answer_or_refused(rsf):  # noqa: F811
    state, run, person = await _visitor_world(rsf)
    await simulation_website.send(state, run, person, "Salut")
    [person] = await store.people(rsf, run.id)
    session_id = uuid.UUID(simulation_website.visitor_session(person)[0])
    await _agent_asks(rsf, session_id, "call_1", [_DAY])
    assert (await simulation_website.send(state, run, person, "Joi, dacă se poate"))["delivered"]
    assert state.widget.state.answers[0][1] == {"responses": [{"question": "Ce zi?",
                                                         "answer": "Joi, dacă se poate"}]}

    await _agent_asks(rsf, session_id, "call_2", [_DAY, {"prompt": "Ce oră?"}])
    with pytest.raises(HTTPException) as refused:
        await simulation_website.send(state, run, person, "Luni la 10")
    assert refused.value.status_code == 422 and "tap" in refused.value.detail


@pytest.mark.asyncio
async def test_autopilot_answers_the_agents_question_with_a_tap(rsf, monkeypatch):  # noqa: F811
    state, run, _ = await _visitor_world(rsf)
    moves = [actor.Move(action="write", text="Vreau o programare"),
             actor.Move(action="tap", tap={"answer": {"tool_call_id": "call_1", "responses": [
                 {"question": "Ce zi?", "answer": "Luni"}]}})]
    prompts = []

    async def generate(**kw):
        prompts.append(kw["messages"])
        return moves.pop(0)

    class _LLM:
        async def close(self):
            pass

    monkeypatch.setattr(actor, "generate_structured", generate)
    monkeypatch.setattr(actor, "_client", lambda settings: (_LLM(), "m"))
    monkeypatch.setattr(actor, "QUIET", timedelta(0))
    ai = actor.Actor(state)
    [(run, person)] = await store.ai_people(rsf)
    await ai._move(run, person)
    [(run, person)] = await store.ai_people(rsf)
    await _agent_asks(rsf, uuid.UUID(simulation_website.visitor_session(person)[0]), "call_1", [_DAY])
    await ai._move(run, person)
    assert '"tool_call_id": "call_1"' in prompts[1][1]["content"]
    assert "tool_call_id" in prompts[1][0]["content"]
    assert [a[1] for a in state.widget.state.answers] == [{"responses": [
        {"question": "Ce zi?", "answer": "Luni", "is_other": False}]}]


@pytest.mark.asyncio
async def test_a_hand_played_visitors_chat_is_logged_for_studio(rsf):  # noqa: F811
    state, run, person = await _visitor_world(rsf, autopilot=False)
    assert await store.visitors(rsf) == []
    await api.write(state, run, person, api.PersonMessage(text="Salut"))
    [(_, visitor)] = await store.visitors(rsf)
    assert visitor.id == person.id and visitor.driver == "human"


def _internal(rsf, state):  # noqa: F811
    app = FastAPI()
    app.include_router(api.router)
    for key, value in vars(state).items():
        setattr(app.state, key, value)
    return app


@pytest.mark.asyncio
async def test_a_visitors_widget_session_never_leaves_the_runtime(rsf):  # noqa: F811
    state, run, person = await _visitor_world(rsf)
    await simulation_website.send(state, run, person, "Salut")
    [person] = await store.people(rsf, run.id)
    assert simulation_website.visitor_session(person)
    detail = await api._detail(rsf, run)
    assert detail["people"][0]["address"] == "visitor"


@pytest.mark.asyncio
async def test_each_reply_is_logged_once_and_empty_questions_not_at_all(rsf):  # noqa: F811
    state, run, person = await _visitor_world(rsf)
    await simulation_website.send(state, run, person, "Salut")
    [person] = await store.people(rsf, run.id)
    session_id = uuid.UUID(simulation_website.visitor_session(person)[0])
    await _agent_says(rsf, session_id, "Bună!")
    await _agent_asks(rsf, session_id, "call_0", [])
    # Two readers at once (the actor and a Studio write) log each event once.
    await asyncio.gather(*(simulation_website.collect(state, run, person) for _ in range(3)))
    assert [m.body for m in await store.thread(rsf, person.id) if m.direction == "out"] == [
        {"text": "Bună!"}]
    # The cursor moves past what is logged: the next reply is all that's read.
    await _agent_says(rsf, session_id, "Cu ce vă ajut?")
    await simulation_website.collect(state, run, person)
    assert [m.body["text"] for m in await store.thread(rsf, person.id)] == [
        "Salut", "Bună!", "Cu ce vă ajut?"]


@pytest.mark.asyncio
async def test_an_expired_widget_session_is_renewed_once(rsf):  # noqa: F811
    state, run, person = await _visitor_world(rsf)
    await simulation_website.send(state, run, person, "Salut")
    [person] = await store.people(rsf, run.id)
    first = simulation_website.visitor_session(person)
    stale = " ".join((first[0], first[1], "expired"))
    await store.update_person(rsf, person.id, address=stale)
    person.address = stale
    assert (await simulation_website.send(state, run, person, "Mai sunteți?"))["delivered"]
    [person] = await store.people(rsf, run.id)
    assert simulation_website.visitor_session(person)[0] != first[0]

    # A key that no longer opens sessions: a clear refusal, not a silent loop.
    person.address = stale
    run.phone_number_id = "surg_wk_gone"
    assert (await simulation_website.send(state, run, person, "Alo?"))["delivered"] is False


@pytest.mark.asyncio
async def test_one_read_logs_every_new_reply_and_question_and_closes_answered_ones(rsf):  # noqa: F811
    state, run, person = await _visitor_world(rsf)
    await simulation_website.send(state, run, person, "Salut")
    [person] = await store.people(rsf, run.id)
    session_id = uuid.UUID(simulation_website.visitor_session(person)[0])
    await _agent_says(rsf, session_id, "Bună!", "")
    await _agent_asks(rsf, session_id, "call_1", [_DAY])
    async with rsf() as db:
        db.add(Event(session_id=session_id, type="tool.result", data={"tool_call_id": "call_1"}))
        await db.commit()
    await _agent_asks(rsf, session_id, "call_2", [_DAY])
    await _agent_asks(rsf, session_id, "call_3", [])
    await simulation_website.collect(state, run, person)
    out = [m for m in await store.thread(rsf, person.id) if m.direction == "out"]
    assert [(m.kind, m.body.get("text") or m.body["tool_call_id"], m.status) for m in out] == [
        ("text", "Bună!", "delivered"), ("question", "call_1", "answered"), ("question", "call_2", "")]
    assert all(m.created_at for m in out) and [m.wamid for m in out] == sorted(m.wamid for m in out)

    # Nothing new said, but the open question was answered meanwhile.
    async with rsf() as db:
        db.add(Event(session_id=session_id, type="ask_user_question.response",
                     data={"tool_call_id": "call_2"}))
        await db.commit()
    await simulation_website.collect(state, run, person)
    assert [m.status for m in await store.thread(rsf, person.id) if m.direction == "out"] == [
        "delivered", "answered", "answered"]
