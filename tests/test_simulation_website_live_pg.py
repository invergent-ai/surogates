"""A website visitor played by Autopilot against the runtime's real widget
routes and session store, on Postgres: the answer route takes an advisory
lock SQLite doesn't have.

Set ``SUROGATE_TEST_PG_URL`` to an empty scratch database; the fixture drops
its ``public`` schema.
"""

from __future__ import annotations

import os
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
import sqlalchemy as sa
from fastapi import FastAPI
from httpx import ASGITransport
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from surogates.api.app import build_channel_routing_cache
from surogates.api.routes import website
from surogates.db.engine import apply_observability_ddl
from surogates.db.models import Base, Event
from surogates.session.events import EventType
from surogates.session.store import SessionStore
from surogates.simulation import store
from surogates.simulation import website as simulation_website
from tests.api.test_website_multi_session import _ORIGIN, _settings, _Storage
from tests.test_simulation_api import Settings
from tests.test_simulation_store import FakeVault, line, make_org

URL = os.environ.get("SUROGATE_TEST_PG_URL")
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not URL, reason="set SUROGATE_TEST_PG_URL to a scratch Postgres database"),
]


@pytest_asyncio.fixture
async def pg():
    engine = create_async_engine(URL)
    async with engine.begin() as conn:
        await conn.exec_driver_sql("DROP SCHEMA public CASCADE")
        await conn.exec_driver_sql("CREATE SCHEMA public")
        await conn.run_sync(Base.metadata.create_all)
        await apply_observability_ddl(conn)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


class _Ops:
    async def get_channel_routing(self, kind, identifier):
        return None


async def test_a_visitor_answers_through_the_real_answer_route(pg):
    widget = FastAPI()
    widget.include_router(website.router, prefix="/v1")
    sessions = SessionStore(pg)
    widget.state.settings, widget.state.storage = _settings(), _Storage()
    widget.state.session_store, widget.state.redis = sessions, SimpleNamespace(zadd=AsyncMock())
    widget.state.channel_routing_cache = build_channel_routing_cache(
        settings=None, platform_client=_Ops(), session_factory=pg)
    state = SimpleNamespace(session_factory=pg, settings=Settings,
                            http=httpx.AsyncClient(transport=ASGITransport(app=widget)))
    run = await store.create_run(
        pg, FakeVault(), org_id=await make_org(pg), agent_id="hero-agent", created_by="u1",
        channel="website", people=[line("Ana", autopilot=True)],
        routing_config={"allowed_origins": [_ORIGIN], "website_api_url": "https://runtime"},
    )
    [person] = await store.people(pg, run.id)

    assert (await simulation_website.send(state, run, person, "Vreau o programare"))["delivered"]
    session_id = uuid.UUID(simulation_website.visitor_session(person)[0])
    questions = [{"prompt": "Ce zi?", "choices": [{"label": "Luni"}, {"label": "Marți"}]}]
    await sessions.emit_event(session_id, EventType.TOOL_CALL, {
        "tool_call_id": "call_1", "name": "ask_user_question", "arguments": {"questions": questions}})
    await sessions.emit_event(session_id, EventType.INBOX_INPUT_REQUIRED, {
        "tool_call_id": "call_1", "questions": questions, "context": "", "message": ""})

    answers = [{"question": "Ce zi?", "answer": "Luni"}]
    assert await simulation_website.answer(state, run, person, "call_1", answers) == {
        "delivered": True, "channels_status": 201}
    async with pg() as db:
        [recorded] = (await db.execute(sa.select(Event.data).where(
            Event.session_id == session_id, Event.type == "ask_user_question.response"))).scalars()
    assert recorded["tool_call_id"] == "call_1" and recorded["responses"][0]["answer"] == "Luni"
    thread = await store.thread(pg, person.id)
    assert [(m.kind, m.status) for m in thread] == [
        ("text", "delivered"), ("question", "answered"), ("answer", "delivered")]

    # Answered: the route says so with a 409, which is no failure.
    again = await simulation_website.answer(state, run, person, "call_1", answers)
    assert again["already_answered"] and again["channels_status"] == 409
