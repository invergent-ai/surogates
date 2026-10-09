"""A local-folder chat's browser taken over on its user's computer, and handed back, as the dispatcher sees it.

Against the real database and queue: the control route's own events, the orphan sweeper's query and
one of its passes, and what a wake would find past the chat's cursor.
"""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from surogates.api.app import _install_browser_api_dependencies
from surogates.api.routes import browser as browser_routes
from surogates.browser.control import paused_by_user_result
from surogates.browser.registry import BrowserEntry
from surogates.channels.memory_boundary import PROJECT_BOUNDARY_PREFIX
from surogates.config import SHARED_WORK_QUEUE_KEY, encode_queue_member
from surogates.devices.browser import tell_pane
from surogates.harness.loop_messages import maybe_inject_browser_pause
from surogates.harness.loop_pending import _actionable_pending_events
from surogates.harness.slash_skill import build_expanded_message
from surogates.orchestrator.dispatcher import Orchestrator
from surogates.session.events import EventType
from surogates.session.store import SessionStore
from surogates.tenant.auth.jwt import create_access_token, create_service_account_session_token
from surogates.tenant.auth.middleware import get_current_tenant
from surogates.tenant.context import TenantContext

from tests.test_steer_loop import _final_response

from .conftest import create_org, create_user, issue_service_account_token
from .test_devices import AGENT_ID, add_user, api  # noqa: F401  (api is a fixture)
from .test_workstream_spend import CAPPED, Ops, worker
from .test_workstream_threads import TODO_CALL, harness_of, live_turn, replayed, waking, woken

pytestmark = pytest.mark.asyncio(loop_scope="session")

# The sweeper's own threshold, and a chat left alone for longer.
STALE, LEFT = 60, 600

ANSWERED = (EventType.LLM_RESPONSE, {"message": {"role": "assistant", "content": "It is open."}})


class Computer:
    """One user's computer in a test: the chats of an agent on its folders, the API they are told
    through, and the dispatcher's sweeper for that agent."""

    def __init__(self, session_store, session_factory, redis_client, org_id: UUID, user_id: UUID) -> None:
        self.store, self.factory, self.redis = session_store, session_factory, redis_client
        self.org_id, self.user_id = org_id, user_id
        self.agent_id = f"takeover-agent-{uuid4()}"
        self.device_id = uuid4()
        self.wakes: list[str] = []
        self.sweeper = Orchestrator(
            redis_client=redis_client, session_store=session_store, harness_factory=lambda _sid: None,
            agent_id=self.agent_id, queue_key=SHARED_WORK_QUEUE_KEY,
        )

    def served(self, *, session_scope_id: UUID | None = None) -> FastAPI:
        """The browser routes with the API's own emitter and wake, which enqueues on the real queue,
        for a caller who is the computer's user, or a worker with a token for one session."""
        app = FastAPI()
        app.include_router(browser_routes.router, prefix="/v1")
        app.state.redis, app.state.session_store = self.redis, self.store
        _install_browser_api_dependencies(app, SimpleNamespace(browser=SimpleNamespace(backend=None)))
        enqueue = app.state.session_wake

        async def wake(session_id: str) -> None:
            self.wakes.append(session_id)
            await enqueue(session_id)

        app.state.session_wake = wake

        async def tenant() -> TenantContext:
            return TenantContext(
                org_id=self.org_id, user_id=None if session_scope_id else self.user_id, org_config={},
                user_preferences={}, permissions=frozenset(), asset_root="/tmp/surogates-test",
                session_scope_id=session_scope_id,
            )

        app.dependency_overrides[get_current_tenant] = tenant
        return app

    async def chat(self, *events: tuple[EventType, dict], **other) -> UUID:
        """A chat of the agent on a folder of this computer, left alone since *events*, which a turn read.

        *other* says where it is not this computer's user's chat with the agent: another computer's
        (device_id), another agent's (agent_id), or another user's (user_id).
        """
        device_id = other.pop("device_id", self.device_id)
        session = await self.store.create_session(
            **{"user_id": self.user_id, "org_id": self.org_id, "agent_id": self.agent_id, **other},
            config={"execution": {"kind": "device", "device_id": str(device_id)}},
        )
        read = 0
        for kind, data in events:
            read = await self.store.emit_event(session.id, kind, data)
        if read:
            lease = await self.store.try_acquire_lease(session.id, "the-turns-worker")
            await self.store.advance_harness_cursor(session.id, read, lease.lease_token)
            await self.store.release_lease(session.id, lease.lease_token)
        await self.left(session.id)
        return session.id

    async def idle(self, **other) -> UUID:
        """A chat whose last turn ended with an answer and left it active, as a command's answer does."""
        return await self.chat((EventType.USER_MESSAGE, {"content": "Open the report."}), ANSWERED, **other)

    async def told_taken_over(self, **other) -> UUID:
        """An idle chat whose log says its user took its browser over, as the control route writes it."""
        chat = await self.idle(**other)
        await self.store.emit_event(
            chat, EventType.BROWSER_CONTROL_GRANTED,
            {"session_id": str(chat), "owner_user_id": str(self.user_id), "computer": True},
        )
        await self.left(chat)
        return chat

    async def turn(self, chat: UUID, said: str, tool: str, result: str, *, by: UUID | None = None) -> None:
        """A turn of *chat*: its user says *said*, a call of *tool* answers *result*, the model answers,
        and the chat is left alone, active, its log read. The call is the chat's own, or made *by* a
        session working under it."""
        call = {"id": f"call-{uuid4().hex[:8]}", "type": "function", "function": {"name": tool, "arguments": "{}"}}
        asked = {"tool_call_id": call["id"], "name": tool}
        emit = self.store.emit_event
        await emit(chat, EventType.USER_MESSAGE, {"content": said})
        if by is None:
            await emit(chat, EventType.LLM_RESPONSE, {"message": {"role": "assistant", "content": "", "tool_calls": [call]}})
        for kind, data in ((EventType.TOOL_CALL, asked), (EventType.TOOL_RESULT, {**asked, "content": result, "elapsed_ms": 3})):
            await emit(by or chat, kind, data)
        read = await emit(chat, *ANSWERED)
        lease = await self.store.try_acquire_lease(chat, "the-turns-worker")
        await self.store.advance_harness_cursor(chat, read, lease.lease_token)
        await self.store.release_lease(chat, lease.lease_token)
        await self.left(chat)

    async def stopped(self, chat: UUID, **turn) -> None:
        """The chat's agent meets the pause: asked for a click while its user holds the browser, its
        browser call is answered paused, as the computer answers every one, and it tells its user so."""
        await self.turn(chat, "Click Next.", "browser_click", paused_by_user_result(), **turn)

    async def under(self, chat: UUID) -> UUID:
        """A session working under *chat*, as a sub-agent's is: in its folder, on its computer."""
        root = await self.store.get_session(chat)
        child = await self.store.create_session(
            user_id=root.user_id, org_id=root.org_id, agent_id=root.agent_id, parent_id=chat,
            config={**root.config, "sandbox_root_session_id": str(chat)},
        )
        return child.id

    async def handed_back(self, chat: UUID) -> dict:
        """What the chat was last told of its browser's hand back."""
        [*_, told] = await self.store.get_events(chat, types=[EventType.BROWSER_CONTROL_RETURNED])
        return told.data

    async def left(self, *chats: UUID) -> None:
        """Nothing has happened in *chats* for ten minutes, well past the sweeper's threshold."""
        async with self.factory() as db:
            for chat in chats:
                await db.execute(
                    text("UPDATE sessions SET updated_at = now() - make_interval(secs => :s) WHERE id = :sid"),
                    {"s": LEFT, "sid": chat},
                )
            await db.commit()

    async def control(self, chat: UUID, action: str, *, token_of: UUID | None = None) -> dict:
        """Post *action* to the chat's control route: as its user, or with a worker's token, which
        covers the one session *token_of* and names the user it speaks for."""
        said = {"action": action, **({"owner_user_id": str(self.user_id)} if token_of else {})}
        path = f"/v1{'/api' if token_of else ''}/sessions/{chat}/browser/control"
        app = self.served(session_scope_id=token_of)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(path, json=said)
        assert response.status_code == 200, response.text
        # Its events bumped the chat's clock: it is left alone again since.
        await self.left(chat)
        return response.json()

    async def orphans(self) -> set[UUID]:
        return {o.id for o in await self.store.find_orphaned_sessions(stale_seconds=STALE, agent_id=self.agent_id)}

    async def sweep(self) -> int:
        return await self.sweeper._sweep_orphans_once(stale_seconds=STALE, reason="orchestrator_sweeper")

    async def log(self, chat: UUID) -> list[str]:
        return [event.type for event in await self.store.get_events(chat)]

    async def work(self, chat: UUID) -> list[str]:
        """What a wake of *chat* would find to do: the events past its cursor that start a turn."""
        pending = _actionable_pending_events(await self.store.get_events(chat), await self.store.get_harness_cursor(chat))
        return [event.type for event in pending]

    async def queued(self, chat: UUID) -> bool:
        member = encode_queue_member(org_id=str(self.org_id), agent_id=self.agent_id, session_id=str(chat))
        return await self.redis.zscore(SHARED_WORK_QUEUE_KEY, member) is not None

    async def unqueue(self, *chats: UUID) -> None:
        for chat in chats:
            member = encode_queue_member(org_id=str(self.org_id), agent_id=self.agent_id, session_id=str(chat))
            await self.redis.zrem(SHARED_WORK_QUEUE_KEY, member)


@pytest_asyncio.fixture(loop_scope="session")
async def computer(session_store, session_factory, redis_client):
    org_id = await create_org(session_factory)
    return Computer(session_store, session_factory, redis_client, org_id, await create_user(session_factory, org_id))


async def test_a_take_over_leaves_an_idle_chat_to_its_user(computer):
    chat = await computer.idle()
    assert (await computer.orphans(), await computer.work(chat)) == (set(), [])

    assert (await computer.control(chat, "acquire"))["outcome"] == "granted"

    # Its user took the browser over to stop the agent: the chat does not look crashed, and a wake has no work.
    assert await computer.orphans() == set()
    assert await computer.work(chat) == []
    # One real pass of the sweeper leaves it alone: not recovered, and not queued for a wake.
    assert await computer.sweep() == 0
    assert await computer.log(chat) == ["user.message", "llm.response", "browser.control_granted"]
    assert not await computer.queued(chat)
    assert computer.wakes == []


async def test_a_hand_back_wakes_the_agent_the_take_over_had_stopped_once(computer):
    chat = await computer.idle()
    await computer.control(chat, "acquire")
    await computer.stopped(chat)
    assert not await computer.queued(chat)

    assert (await computer.control(chat, "release"))["outcome"] == "released"

    try:
        # Handed back, its agent goes on: the hand back says so, queues the chat, and is its wake's work.
        assert await computer.handed_back(chat) == {
            "session_id": str(chat), "released_by": str(computer.user_id), "computer": True, "resumes": True,
        }
        assert computer.wakes == [str(chat)]
        assert await computer.queued(chat)
        assert await computer.work(chat) == ["browser.control_returned"]
        # Handed back already: a repeat tells the chat nothing more and wakes nobody.
        await computer.control(chat, "release")
        assert computer.wakes == [str(chat)]
        assert (await computer.log(chat)).count("browser.control_returned") == 1
    finally:
        await computer.unqueue(chat)


async def test_a_hand_back_that_stopped_no_agent_is_for_the_chats_pane_alone(computer):
    chat = await computer.idle()
    await computer.control(chat, "acquire")

    assert (await computer.control(chat, "release"))["outcome"] == "released"

    # No browser call of the chat's was answered paused while its user held the browser: the chat is
    # told the hand back, and that is all. Nobody is woken, nothing is pending, nothing is swept.
    assert await computer.handed_back(chat) == {
        "session_id": str(chat), "released_by": str(computer.user_id), "computer": True,
    }
    assert computer.wakes == []
    assert not await computer.queued(chat)
    assert await computer.work(chat) == []
    assert await computer.orphans() == set()
    assert await computer.sweep() == 0
    assert await computer.log(chat) == [
        "user.message", "llm.response", "browser.control_granted", "browser.control_returned",
    ]


QUOTED = json.dumps({"value": json.loads(paused_by_user_result())})


@pytest.mark.parametrize(("met", "resumes"), [
    ("its own call, since the take-over", True),
    ("a sub-agent's call, since the take-over", True),
    ("its own call, before the take-over", False),
    ("another chat's call", False),
    ("a call of another user's session that names the chat", False),
    ("a tool that is no browser's", False),
    ("a page that quotes the pause", False),
])
async def test_a_hand_back_resumes_a_chat_only_for_a_browser_call_of_its_own_answered_paused_since_the_take_over(
    computer, session_factory, met, resumes,
):
    chat, another = await computer.idle(), await computer.idle()
    if met == "its own call, before the take-over":
        # Met while the browser was held from another chat, and handed back there since.
        await computer.stopped(chat)
    await computer.control(chat, "acquire")
    if met == "its own call, since the take-over":
        await computer.stopped(chat)
    elif met == "a sub-agent's call, since the take-over":
        await computer.stopped(chat, by=await computer.under(chat))
    elif met == "another chat's call":
        await computer.stopped(another)
    elif met == "a call of another user's session that names the chat":
        # As no session is made: one under a chat is its user's. Not found among theirs, it is not the chat's.
        stranger = await computer.store.create_session(
            user_id=await create_user(session_factory, computer.org_id), org_id=computer.org_id,
            agent_id=computer.agent_id, config={"sandbox_root_session_id": str(chat)},
        )
        await computer.stopped(chat, by=stranger.id)
    elif met == "a tool that is no browser's":
        await computer.turn(chat, "Read the note.", "read_file", paused_by_user_result())
    elif met == "a page that quotes the pause":
        await computer.turn(chat, "What does the page say?", "browser_evaluate", QUOTED)

    await computer.control(chat, "release")

    try:
        assert (await computer.handed_back(chat)).get("resumes", False) is resumes
        assert computer.wakes == ([str(chat)] if resumes else [])
    finally:
        await computer.unqueue(chat)


async def test_a_later_take_over_that_stopped_nothing_is_handed_back_for_the_pane_alone(computer):
    chat = await computer.idle()
    await computer.control(chat, "acquire")
    await computer.stopped(chat)
    await computer.control(chat, "release")

    try:
        # Taken over again, with no browser call of the chat's meanwhile: what the first take-over
        # stopped was handed back with it, and does not count for this one.
        await computer.control(chat, "acquire")
        await computer.control(chat, "release")
        assert "resumes" not in await computer.handed_back(chat)
        assert computer.wakes == [str(chat)]
    finally:
        await computer.unqueue(chat)


# As many connections as posts arrive at once, and no long wait for one: a telling that needed a second
# connection while it held its first would leave none, and every post would wait this out and fail.
POOL, POOL_WAIT_S = 4, 3.0


@asynccontextmanager
async def pooled(pg_url, session_factory, redis_client, connections: int):
    """A computer whose API has a database pool of *connections*, and its own store on it."""
    engine = create_async_engine(
        pg_url, pool_size=connections, max_overflow=0, pool_timeout=POOL_WAIT_S, connect_args={"statement_cache_size": 0},
    )
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    store = SessionStore(async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False))
    try:
        yield Computer(store, session_factory, redis_client, org_id, user_id)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture(loop_scope="session")
async def narrow(pg_url, session_factory, redis_client):
    async with pooled(pg_url, session_factory, redis_client, POOL) as computer:
        yield computer


async def test_a_take_over_and_a_hand_back_are_told_through_one_connection(pg_url, session_factory, redis_client):
    # A pool of one: anything in the telling that held a connection while it asked for another would wait
    # for itself until the pool gave up.
    async with pooled(pg_url, session_factory, redis_client, 1) as single:
        chat, held = await single.idle(), await single.told_taken_over()
        try:
            assert (await single.control(chat, "acquire"))["outcome"] == "granted"
            await single.stopped(chat)
            assert await single.control(chat, "release") == {"outcome": "released"}
            assert (await single.log(chat))[-1] == "browser.control_returned"
            # The other chat that still said its user held the browser was told on that one connection too.
            assert (await single.log(held))[-1] == "browser.control_returned"
        finally:
            await single.unqueue(chat)


async def test_as_many_take_overs_at_once_as_the_pool_is_wide_are_each_told_and_leave_a_read_its_connection(narrow):
    chats = [await narrow.idle() for _ in range(POOL)]
    started = time.monotonic()

    async def an_unrelated_read() -> float:
        await narrow.store.get_session(chats[0])
        return time.monotonic() - started

    *answers, read_after = await asyncio.gather(
        *(narrow.control(chat, "acquire") for chat in chats), an_unrelated_read(),
    )

    assert [answer["outcome"] for answer in answers] == ["granted"] * POOL
    # Nothing waited for a connection another post held while it asked for a second.
    assert read_after < POOL_WAIT_S / 2
    assert time.monotonic() - started < POOL_WAIT_S / 2


async def test_as_many_hand_backs_of_one_chat_at_once_as_the_pool_is_wide_tell_it_one(narrow):
    chat = await narrow.idle()
    await narrow.control(chat, "acquire")
    await narrow.stopped(chat)
    started = time.monotonic()

    try:
        answers = await asyncio.gather(*(narrow.control(chat, "release") for _ in range(POOL)))
        assert answers == [{"outcome": "released"}] * POOL
        assert time.monotonic() - started < POOL_WAIT_S / 2
        assert (await narrow.log(chat)).count("browser.control_returned") == 1
        assert narrow.wakes == [str(chat)]
    finally:
        await narrow.unqueue(chat)


async def test_two_hand_backs_posted_together_tell_the_chat_one_and_wake_its_agent_once(computer, monkeypatch):
    chat = await computer.idle()
    await computer.control(chat, "acquire")
    await computer.stopped(chat)
    # The chat is open in two windows, and each posts the hand back. The first is slow to be written:
    # time enough for the second to read what the chat was last told.
    emit = computer.store.emit_event

    async def slow_to_tell(session_id, event_type, *args, **kwargs):
        if event_type is EventType.BROWSER_CONTROL_RETURNED:
            await asyncio.sleep(0.3)
        return await emit(session_id, event_type, *args, **kwargs)

    monkeypatch.setattr(computer.store, "emit_event", slow_to_tell)

    try:
        answers = await asyncio.gather(computer.control(chat, "release"), computer.control(chat, "release"))
        assert answers == [{"outcome": "released"}] * 2
        assert (await computer.log(chat)).count("browser.control_returned") == 1
        assert computer.wakes == [str(chat)]
    finally:
        await computer.unqueue(chat)


async def test_a_chat_that_met_the_pause_while_another_held_the_browser_is_not_woken_at_the_hand_back(computer):
    # The browser is held for every chat of the agent's there: this one's call answered paused too.
    held, other = await computer.idle(), await computer.idle()
    await computer.control(held, "acquire")
    await computer.stopped(held)
    await computer.stopped(other)
    before = await computer.log(other)

    await computer.control(held, "release")

    try:
        # The chat the browser was taken over from goes on. The other is told nothing, and is left alone.
        assert computer.wakes == [str(held)]
        assert await computer.log(other) == before
        assert await computer.work(other) == []
        assert not await computer.queued(other)
        assert await computer.orphans() == {held}
    finally:
        await computer.unqueue(held)


async def test_the_clouds_hand_back_is_work_and_is_recovered_as_it_was(computer):
    # A chat in the cloud, left active, its browser's release written as the cloud's route writes it.
    session = await computer.store.create_session(user_id=computer.user_id, org_id=computer.org_id, agent_id=computer.agent_id)
    chat = session.id
    read = 0
    for kind, data in ((EventType.USER_MESSAGE, {"content": "Open the report."}), ANSWERED):
        read = await computer.store.emit_event(chat, kind, data)
    lease = await computer.store.try_acquire_lease(chat, "the-turns-worker")
    await computer.store.advance_harness_cursor(chat, read, lease.lease_token)
    await computer.store.release_lease(chat, lease.lease_token)
    await computer.store.emit_event(
        chat, EventType.BROWSER_CONTROL_RETURNED, {"session_id": str(chat), "released_by": str(computer.user_id)},
    )
    await computer.left(chat)

    assert await computer.work(chat) == ["browser.control_returned"]
    assert await computer.orphans() == {chat}


async def test_a_hand_back_whose_wake_was_lost_is_found_by_the_sweeper(computer):
    chat = await computer.idle()
    await computer.control(chat, "acquire")
    await computer.stopped(chat)
    await computer.control(chat, "release")
    # The wake the hand back queued never ran.
    await computer.unqueue(chat)

    try:
        assert await computer.orphans() == {chat}
        assert await computer.sweep() == 1
        assert await computer.queued(chat)
    finally:
        await computer.unqueue(chat)


async def test_a_take_over_of_a_chat_no_turn_has_run_in_leaves_it_waiting_for_its_first_message(computer):
    # A chat on a folder with no events waits for its first message: the sweeper leaves it, and would fail
    # it at its third pass otherwise. Its browser taken over from it changes nothing of that.
    chat = await computer.chat()
    await computer.control(chat, "acquire")

    assert await computer.orphans() == set()
    assert await computer.sweep() == 0
    assert await computer.log(chat) == ["browser.control_granted"]


@pytest.mark.parametrize(
    "told", [EventType.BROWSER_PROVISIONED, EventType.BROWSER_DESTROYED, EventType.BROWSER_UNAVAILABLE],
    ids=lambda kind: kind.value,
)
async def test_a_sub_agents_browser_told_to_its_idle_chats_pane_leaves_the_chat_alone(computer, told):
    chat = await computer.idle()
    child = uuid4()

    # As the worker tells the chat's pane of a sub-agent's tab on the computer: written to the chat's own log.
    await tell_pane(computer.store, child, told, {"sandbox_root_session_id": str(chat)})
    await computer.left(chat)

    assert await computer.log(chat) == ["user.message", "llm.response", told.value]
    assert await computer.orphans() == set()
    assert await computer.work(chat) == []
    assert await computer.sweep() == 0
    assert not await computer.queued(chat)


async def test_a_chat_whose_worker_died_mid_turn_is_still_recovered_once_taken_over(computer):
    # The turn's own events are under the take-over: a call asked and begun, and no result.
    call = {"id": "call-1", "type": "function", "function": {"name": "browser_navigate", "arguments": "{}"}}
    chat = await computer.chat((EventType.USER_MESSAGE, {"content": "Open the report."}))
    await computer.store.emit_event(chat, EventType.LLM_RESPONSE, {"message": {"role": "assistant", "tool_calls": [call]}})
    await computer.store.emit_event(chat, EventType.TOOL_CALL, {"tool_call_id": "call-1", "name": "browser_navigate"})
    await computer.control(chat, "acquire")

    try:
        assert await computer.orphans() == {chat}
        assert await computer.work(chat) == ["llm.response", "tool.call"]
        assert await computer.sweep() == 1
        assert await computer.queued(chat)
    finally:
        await computer.unqueue(chat)


async def test_a_take_over_wakes_and_sweeps_none_of_the_agents_other_chats_on_the_computer(computer):
    # The browser is held for every chat of the agent's there: each answers its browser calls paused.
    held, other, never_run = await computer.idle(), await computer.idle(), await computer.chat()
    before = {chat: await computer.log(chat) for chat in (other, never_run)}

    await computer.control(held, "acquire")
    await computer.left(other, never_run)

    assert await computer.orphans() == set()
    assert await computer.sweep() == 0
    assert computer.wakes == []
    for chat in (held, other, never_run):
        assert await computer.work(chat) == []
        assert not await computer.queued(chat)
    # The others are told nothing of it: the take-over is told to the chat it was made from.
    assert {chat: await computer.log(chat) for chat in (other, never_run)} == before


async def test_a_turn_while_the_computers_browser_is_held_is_not_told_that_the_clouds_is(computer):
    chat = await computer.idle()
    await computer.control(chat, "acquire")

    # The harness reads the cloud's lease, which a take-over on a computer never takes: a turn its user
    # starts meanwhile gets no notice ahead of its browser calls, which the computer answers paused.
    control = computer.served().state.browser_control
    assert await control.held_by(str(chat)) is None
    session = await computer.store.get_session(chat)
    assert await maybe_inject_browser_pause(session=session, browser_control=control) is None


async def test_a_hand_back_made_from_another_chat_is_told_to_the_chat_first_taken_over_and_its_agent_is_left_alone(computer):
    first, other = await computer.idle(), await computer.idle()
    await computer.control(first, "acquire")
    # Its agent met the pause, and so did the other chat's, the browser being held for both.
    await computer.stopped(first)
    await computer.stopped(other)
    # The first chat gone from the computer, its user hands the browser back from another: that chat's
    # pane tells its own route of the take-over, then of the hand back.
    await computer.control(other, "acquire")
    await computer.control(other, "release")
    await computer.left(first)

    # The first chat no longer says its user holds the browser: it is told the hand back, and where it was made.
    assert await computer.handed_back(first) == {
        "session_id": str(first), "released_by": str(computer.user_id), "computer": True,
        "handed_back_from": str(other),
    }
    assert (await computer.log(first))[-2:] == ["llm.response", "browser.control_returned"]
    # For its pane: its agent is not woken, has no work, and the chat does not look crashed. Nor is the
    # other's, whose own take-over, told a moment before its hand back, stopped nothing.
    assert "resumes" not in await computer.handed_back(other)
    assert computer.wakes == []
    assert (await computer.work(first), await computer.work(other)) == ([], [])
    assert await computer.orphans() == set()
    assert await computer.sweep() == 0
    assert not await computer.queued(first)


async def test_a_chat_told_of_a_hand_back_made_elsewhere_is_taken_over_and_handed_back_anew_as_any(computer):
    first, other = await computer.idle(), await computer.idle()
    await computer.control(first, "acquire")
    await computer.control(other, "acquire")
    await computer.control(other, "release")
    told = len(await computer.log(first))

    try:
        # Nothing stands to hand back in the first chat: its pane's release at its next load is passed over.
        await computer.control(first, "release")
        assert len(await computer.log(first)) == told
        # Taken over anew, it is told anew; its agent stopped by that, and the browser handed back from
        # itself, its own agent is woken.
        assert (await computer.control(first, "acquire"))["outcome"] == "granted"
        await computer.stopped(first)
        await computer.control(first, "release")
        assert (await computer.log(first))[told] == "browser.control_granted"
        assert (await computer.handed_back(first)).get("resumes") is True
        assert computer.wakes == [str(first)]
        assert await computer.work(first) == ["browser.control_returned"]
    finally:
        await computer.unqueue(first, other)


async def test_a_hand_back_is_told_to_no_chat_but_the_users_own_with_the_agent_on_that_computer_still_taken_over(
    computer, session_factory,
):
    here = await computer.idle()
    # The browser on this computer is one for the agent's chats there: these still say their user holds it.
    standing = [await computer.told_taken_over(), await computer.told_taken_over()]
    # These do not, or are not that browser's: never taken over, handed back already, the agent's on
    # another computer, another agent's here, another user's, and one its user deleted.
    never = await computer.idle()
    handed_back = await computer.told_taken_over()
    await computer.control(handed_back, "release")
    deleted = await computer.told_taken_over()
    await computer.store.update_session_status(deleted, "archived")
    untold = [
        never, handed_back, deleted,
        await computer.told_taken_over(device_id=uuid4()),
        await computer.told_taken_over(agent_id=f"another-agent-{uuid4()}"),
        await computer.told_taken_over(user_id=await create_user(session_factory, computer.org_id)),
    ]
    before = {chat: await computer.log(chat) for chat in untold}

    await computer.control(here, "acquire")
    await computer.control(here, "release")

    for chat in standing:
        assert (await computer.log(chat))[-2:] == ["browser.control_granted", "browser.control_returned"]
    assert {chat: await computer.log(chat) for chat in untold} == before
    assert computer.wakes == []


async def test_a_hand_back_with_no_take_over_standing_in_its_chat_tells_no_other_chat(computer):
    first, other = await computer.told_taken_over(), await computer.idle()

    # As a chat's pane posts at its load, where the computer says nobody holds the browser: passed over.
    await computer.control(other, "release")

    assert (await computer.log(first))[-1] == "browser.control_granted"
    assert computer.wakes == []


async def test_a_token_for_one_chat_hands_back_no_other_chats_take_over(computer):
    first, other = await computer.told_taken_over(), await computer.idle()

    # A worker's token covers its own session: it tells that one, and no other chat of the computer.
    await computer.control(other, "acquire", token_of=other)
    await computer.control(other, "release", token_of=other)

    try:
        assert (await computer.log(other))[-2:] == ["browser.control_granted", "browser.control_returned"]
        assert (await computer.log(first))[-1] == "browser.control_granted"
    finally:
        await computer.unqueue(other)


async def test_the_chats_still_told_taken_over_are_looked_for_in_their_own_organisation(computer, session_factory):
    # A chat with no user of its own, as a service's is: the organisation alone tells two such apart.
    another_org = await create_org(session_factory)
    ours = await computer.told_taken_over(user_id=None)
    await computer.told_taken_over(user_id=None, org_id=another_org)

    async def told(org_id: UUID) -> list[UUID]:
        return await computer.store.chats_told_taken_over(
            device_id=computer.device_id, org_id=org_id, agent_id=computer.agent_id, user_id=None,
        )

    assert await told(computer.org_id) == [ours]
    assert len(await told(another_org)) == 1


async def test_only_a_hand_back_is_passed_over_for_being_a_computers(computer):
    chat = await computer.idle()

    # A message nobody answered is found, and is a wake's work, whatever it carries.
    await computer.store.emit_event(chat, EventType.USER_MESSAGE, {"content": "Go on.", "computer": True})
    await computer.left(chat)

    assert await computer.orphans() == {chat}
    assert await computer.work(chat) == ["user.message"]


# -- Who may take a chat's browser over, hand it back, or read its state: the app's own sign-in check --

NO_SUCH_CHAT = (404, {"detail": "No browser for session"})


class Asking:
    """The real app, each caller with a real token: a user's chats on a folder of their computer,
    their browser routes told through the API's own emitter, and each wake recorded."""

    def __init__(self, the_api, session_factory) -> None:
        self.api, self.factory = the_api, session_factory
        state = the_api.app.state
        self.store = state.session_store
        self.device_id = uuid4()
        self.wakes: list[str] = []
        _install_browser_api_dependencies(the_api.app, SimpleNamespace(browser=SimpleNamespace(backend=None)))
        enqueue = state.session_wake

        async def wake(session_id: str) -> None:
            self.wakes.append(session_id)
            await enqueue(session_id)

        state.session_wake = wake

    async def chat(self, *, taken_over: bool = False, **config) -> UUID:
        """A chat of the app's user on a folder of their computer, its agent's tab open there."""
        session = await self.store.create_session(
            user_id=self.api.user_id, org_id=self.api.org_id, agent_id=AGENT_ID,
            config={
                "execution": {"kind": "device", "device_id": str(self.device_id)},
                "workspace_path": "/home/u/project",
                **config,
            },
        )
        await tell_pane(self.store, session.id, EventType.BROWSER_PROVISIONED)
        if taken_over:
            await self.store.emit_event(session.id, EventType.BROWSER_CONTROL_GRANTED, {
                "session_id": str(session.id), "owner_user_id": str(self.api.user_id), "computer": True,
            })
        return session.id

    async def callers(self, chat: UUID, another_chat: UUID) -> dict[str, tuple[str, dict[str, str]]]:
        """Each caller of *chat*'s routes: the path its token is taken on, and its sign-in."""
        org = self.api.org_id
        _, another_user = await add_user(self.factory, org)
        administrator = create_access_token(
            org, await create_user(self.factory, org), {"admin", "sessions:read", "sessions:write"},
            auth_time=int(time.time()),
        )
        service = await issue_service_account_token(self.factory, org)

        def bearer(token: str) -> dict[str, str]:
            return {"Authorization": f"Bearer {token}"}

        return {
            "its user": ("/v1", self.api.auth()),
            "its own session's token": ("/v1/api", bearer(create_service_account_session_token(org, service.id, chat))),
            "another user of the organisation": ("/v1", bearer(another_user)),
            "an administrator of the organisation": ("/v1", bearer(administrator)),
            "another chat's token": ("/v1/api", bearer(create_service_account_session_token(org, service.id, another_chat))),
            "a service of the organisation": ("/v1/api", bearer(service.token)),
        }

    async def state(self, chat: UUID, caller: tuple[str, dict[str, str]]) -> tuple[int, dict]:
        prefix, sign_in = caller
        response = await self.api.client.get(f"{prefix}/sessions/{chat}/browser/state", headers=sign_in)
        return response.status_code, response.json()

    async def control(self, chat: UUID, action: str, caller: tuple[str, dict[str, str]]) -> tuple[int, dict]:
        prefix, sign_in = caller
        # On the service path a caller names the user it speaks for: here always the chat's own.
        said = {"action": action, **({"owner_user_id": str(self.api.user_id)} if prefix == "/v1/api" else {})}
        response = await self.api.client.post(f"{prefix}/sessions/{chat}/browser/control", json=said, headers=sign_in)
        return response.status_code, response.json()

    async def log(self, chat: UUID) -> list[str]:
        return [event.type for event in await self.store.get_events(chat)]

    async def unqueue(self, *chats: UUID) -> None:
        for chat in chats:
            member = encode_queue_member(org_id=str(self.api.org_id), agent_id=AGENT_ID, session_id=str(chat))
            await self.api.app.state.redis.zrem(SHARED_WORK_QUEUE_KEY, member)

    @property
    def user(self) -> tuple[str, dict[str, str]]:
        """The chats' own user, as a caller of their routes."""
        return "/v1", self.api.auth()

    async def says(self, chat: UUID, said: str) -> None:
        """The chat's user types *said*: a chat whose turn had ended is resumed, as the message route does."""
        if (await self.store.get_session(chat)).status != "active":
            await self.store.resume_session(chat)
        await self.store.emit_event(chat, EventType.USER_MESSAGE, {"content": said})

    async def turn(self, chat: UUID, said: str, answer: str, *, tool: str | None = None, result: str = "") -> None:
        """A whole turn of *chat*, as its worker writes one: its user says *said*, the model calls
        *tool*, which answers *result*, then gives its *answer*, and the turn ends."""
        emit = self.store.emit_event
        await self.says(chat, said)
        await emit(chat, EventType.LLM_REQUEST, {})
        if tool is not None:
            call = {"id": f"call-{uuid4().hex[:8]}", "type": "function", "function": {"name": tool, "arguments": "{}"}}
            asked = {"tool_call_id": call["id"], "name": tool}
            await emit(chat, EventType.LLM_RESPONSE, {"message": {"role": "assistant", "content": "", "tool_calls": [call]}})
            await emit(chat, EventType.TOOL_CALL, asked)
            await emit(chat, EventType.TOOL_RESULT, {**asked, "content": result, "elapsed_ms": 3})
            await emit(chat, EventType.LLM_REQUEST, {})
        await emit(chat, EventType.LLM_RESPONSE, {"message": {"role": "assistant", "content": answer}})
        await self.ends(chat)

    async def ends(self, chat: UUID) -> None:
        """The chat's turn ends as a turn ends: completed, its cursor at the end of its log."""
        session = await self.store.get_session(chat)
        lease = await self.store.try_acquire_lease(chat, "the-turns-worker", ttl_seconds=60)
        await harness_of(self.api)._complete_session(session, [{"role": "assistant", "content": "Done."}], lease, reason="completed")
        await self.store.release_lease(chat, lease.lease_token)

    async def stopped_while_held(self, **config) -> UUID:
        """A chat whose agent opened a page, whose user then took the browser over, and whose agent met
        the pause at its next turn and said so: its turn ended, as every turn does."""
        chat = await self.chat(**config)
        await self.turn(chat, "Open the report.", "It is open.", tool="browser_navigate", result='{"title": "Report"}')
        assert (await self.control(chat, "acquire", self.user))[0] == 200
        await self.turn(chat, "Click Next.", WAITING, tool="browser_click", result=paused_by_user_result())
        return chat

    async def hands_back(self, chat: UUID) -> None:
        assert await self.control(chat, "release", self.user) == (200, {"outcome": "released"})

    async def session(self, chat: UUID):
        return await self.store.get_session(chat)

    async def resumes(self, chat: UUID) -> list[dict]:
        """What resumed the chat without a message of its user's."""
        resumed = await self.store.get_events(chat, types=[EventType.SESSION_RESUME])
        return [event.data for event in resumed if event.data]

    @asynccontextmanager
    async def a_clouds_browser_under(self, *chats: UUID):
        """A cloud browser's entry under each chat's id, which the cloud's routes answer the whole
        organisation from: no chat on a computer has one, and a refusal must not come to depend on that."""
        registry = self.api.app.state.browser_registry
        for chat in chats:
            await registry.set(BrowserEntry(
                session_id=str(chat), org_id=str(self.api.org_id), user_id=str(self.api.user_id),
                rest_url="http://browser.test:10001", cdp_url="ws://browser.test:9222",
                live_view_url="ws://browser.test:443", provisioned_at=datetime.now(timezone.utc),
            ))
        try:
            yield
        finally:
            for chat in chats:
                await registry.delete(str(chat))


@pytest_asyncio.fixture(loop_scope="session")
async def asking(api, session_factory):  # noqa: F811  (the fixture, by its name)
    return Asking(api, session_factory)


THEIRS = ["its user", "its own session's token"]
NOT_THEIRS = [
    "another user of the organisation", "an administrator of the organisation", "another chat's token",
    "a service of the organisation",
]


async def test_only_a_chats_own_user_or_its_own_sessions_token_reads_its_browsers_state(asking):
    chat, another = await asking.chat(), await asking.chat()
    callers = await asking.callers(chat, another)
    on_computer = {"status": "live", "control_owner": None, "live_view_path": "", "computer": True}

    async with asking.a_clouds_browser_under(chat):
        answers = {who: await asking.state(chat, caller) for who, caller in callers.items()}

    assert {who: answers[who] for who in THEIRS} == {who: (200, on_computer) for who in THEIRS}
    # Anyone else is answered as for a chat that does not exist.
    assert await asking.state(uuid4(), callers["its user"]) == NO_SUCH_CHAT
    assert {who: answers[who] for who in NOT_THEIRS} == {who: NO_SUCH_CHAT for who in NOT_THEIRS}


@pytest.mark.parametrize("who", NOT_THEIRS)
async def test_nobody_but_a_chats_own_user_takes_its_browser_over_or_hands_it_back(asking, who):
    chat, held, another = await asking.chat(), await asking.chat(taken_over=True), await asking.chat()
    caller = (await asking.callers(chat, another))[who]
    before = {of: await asking.log(of) for of in (chat, held)}

    async with asking.a_clouds_browser_under(chat, held):
        answers = [
            await asking.control(chat, "acquire", caller), await asking.control(chat, "release", caller),
            # A chat its user holds the browser from: nobody else hands it back either.
            await asking.control(held, "acquire", caller), await asking.control(held, "release", caller),
        ]

    assert answers == [NO_SUCH_CHAT] * 4
    assert await asking.control(uuid4(), "acquire", (await asking.callers(chat, another))["its user"]) == NO_SUCH_CHAT
    # Neither chat is told anything, and nobody is woken.
    assert {of: await asking.log(of) for of in (chat, held)} == before
    assert asking.wakes == []


async def test_a_chats_own_user_takes_its_browser_over_and_hands_it_back_and_ends_their_other_chats_take_overs(asking):
    chat, held, another = await asking.chat(), await asking.chat(taken_over=True), await asking.chat()
    user = (await asking.callers(chat, another))["its user"]

    try:
        assert await asking.control(chat, "acquire", user) == (200, {"outcome": "granted", "owner_user_id": str(asking.api.user_id)})
        assert await asking.control(chat, "release", user) == (200, {"outcome": "released"})
        assert (await asking.log(chat))[-2:] == ["browser.control_granted", "browser.control_returned"]
        # Their other chat on the computer that still said they held the browser is told too.
        assert (await asking.log(held))[-2:] == ["browser.control_granted", "browser.control_returned"]
    finally:
        await asking.unqueue(chat)


async def test_a_chats_own_sessions_token_tells_that_chat_and_no_other_of_its_users(asking):
    chat, held, another = await asking.chat(), await asking.chat(taken_over=True), await asking.chat()
    token = (await asking.callers(chat, another))["its own session's token"]
    before = await asking.log(held)

    try:
        assert (await asking.control(chat, "acquire", token))[0] == 200
        assert await asking.control(chat, "release", token) == (200, {"outcome": "released"})
        assert (await asking.log(chat))[-2:] == ["browser.control_granted", "browser.control_returned"]
        # The token is for one session: the user's other chat keeps what it said.
        assert await asking.log(held) == before
    finally:
        await asking.unqueue(chat)


# -- A hand back gives the agent the take-over had stopped a turn: the real wake, replay and loop --

WAITING = "You have the browser. Hand it back and I will go on."
# What the model reads at the hand back, word for word.
HANDED_BACK = {
    "role": "user",
    "content": (
        "[The user has handed the browser back. The browser tools work again: go on with what you were "
        "doing when they took it over. They may have changed the page meanwhile, so read it again before "
        "you act on it.]"
    ),
}


async def test_a_hand_back_gives_a_finished_chat_whose_agent_was_stopped_a_turn_in_which_it_reads_it_can_go_on(
    asking, monkeypatch,
):
    chat = await asking.stopped_while_held()
    assert (await asking.session(chat)).status == "completed"

    await asking.hands_back(chat)

    try:
        assert asking.wakes == [str(chat)]
        sent = await woken(asking.api, monkeypatch, await asking.session(chat))
        # The model reads the conversation as it was, then the harness's note, as a message of its own.
        assert sent[-2:] == [{"role": "assistant", "content": WAITING}, HANDED_BACK]
        assert await asking.resumes(chat) == [{"source": "browser_hand_back"}]
        assert (await asking.session(chat)).status == "active"
        # What a later turn rebuilds is what this one was sent, and the model's answer after it.
        *replay, answer = await replayed(asking.api, await asking.session(chat))
        assert (replay, answer["content"]) == (sent, "Noted.")
    finally:
        await asking.unqueue(chat)


async def test_a_hand_back_that_stopped_no_agent_gives_a_finished_chat_no_turn(asking, monkeypatch):
    chat = await asking.chat()
    await asking.turn(chat, "Open the report.", "It is open.", tool="browser_navigate", result='{"title": "Report"}')
    await asking.control(chat, "acquire", asking.user)

    await asking.hands_back(chat)

    # Its user took the browser over and handed it back while the agent did nothing: nobody is woken,
    # and a wake that came all the same would run nothing, resume nothing, and tell the model nothing.
    assert asking.wakes == []
    harness, handed = waking(asking.api, monkeypatch)
    await harness.wake(chat)
    assert handed == []
    assert await asking.resumes(chat) == []
    assert (await asking.session(chat)).status == "completed"
    assert HANDED_BACK not in await replayed(asking.api, await asking.session(chat))


COMMANDS = {
    "/compress": ("_handle_compress_command", "Context is too small to compress — only 4 messages.", False),
    "/clear": ("_handle_clear_command", "Conversation cleared.", False),
    "/goal status": ("_handle_goal_command", "No outcome is active.", True),
}


async def command_answered(asking, chat: UUID, command: str, *, meanwhile=None) -> None:
    """The chat's user types *command*, and its handler answers it, as each leaves the chat: active,
    its answer the last word, and only an outcome's command moving the cursor past it. *meanwhile*
    happens while its wake is answering it."""
    _handler, answer, moves_the_cursor = COMMANDS[command]
    emit = asking.store.emit_event
    await asking.says(chat, command)
    await emit(chat, EventType.HARNESS_WAKE, {"worker_id": "the-commands-worker", "cursor": 0})
    if meanwhile is not None:
        await meanwhile()
    if command == "/clear":
        await emit(chat, EventType.CONTEXT_COMPACT, {"compacted_messages": [], "strategy": "clear"})
    answered = await emit(chat, EventType.LLM_RESPONSE, {"message": {"role": "assistant", "content": answer}})
    if moves_the_cursor:
        lease = await asking.store.try_acquire_lease(chat, "the-commands-worker", ttl_seconds=60)
        await asking.store.advance_harness_cursor(chat, answered, lease.lease_token)
        await asking.store.release_lease(chat, lease.lease_token)


def commands_of(harness) -> dict[str, AsyncMock]:
    """The handlers of the commands a wake could run again, each watched."""
    watched = {command: AsyncMock() for command in COMMANDS}
    for command, (handler, _answer, _cursor) in COMMANDS.items():
        setattr(harness, handler, watched[command])
    return watched


@pytest.mark.parametrize("command", list(COMMANDS))
async def test_a_hand_back_never_runs_the_users_last_command_again_and_gives_the_stopped_agent_its_turn(
    asking, monkeypatch, command,
):
    chat = await asking.stopped_while_held()
    # The user's last message is a command, answered: it left the chat active.
    await command_answered(asking, chat, command)

    await asking.hands_back(chat)

    try:
        harness, handed = waking(asking.api, monkeypatch)
        ran = commands_of(harness)
        await harness.wake(chat)
        assert [name for name, handler in ran.items() if handler.await_count] == []
        [conversation] = handed
        assert conversation[-2:] == [{"role": "assistant", "content": COMMANDS[command][1]}, HANDED_BACK]
        # A worker that dies in that turn, after its request, leaves it to be run again: still no command.
        await asking.store.emit_event(chat, EventType.LLM_REQUEST, {})
        harness, handed = waking(asking.api, monkeypatch)
        ran = commands_of(harness)
        await harness.wake(chat)
        assert [name for name, handler in ran.items() if handler.await_count] == []
        assert [conversation.count(HANDED_BACK) for conversation in handed] == [1]
    finally:
        await asking.unqueue(chat)


@pytest.mark.parametrize("command", list(COMMANDS))
async def test_a_hand_back_that_stopped_no_agent_wakes_nobody_in_a_chat_left_active_by_a_command(asking, command):
    chat = await asking.chat()
    await asking.turn(chat, "Open the report.", "It is open.", tool="browser_navigate", result='{"title": "Report"}')
    await command_answered(asking, chat, command)
    await asking.control(chat, "acquire", asking.user)

    await asking.hands_back(chat)

    # Nothing wakes the chat, so nothing runs its user's command again: not the hand back, and not
    # the sweeper, to which the chat does not look left half done.
    assert asking.wakes == []
    stale = [o.id for o in await asking.store.find_orphaned_sessions(stale_seconds=0, agent_id=AGENT_ID)]
    assert chat not in stale


async def test_a_hand_back_gives_one_turn_however_often_the_chat_is_woken(asking, monkeypatch):
    chat = await asking.stopped_while_held()
    await asking.hands_back(chat)
    # The pane posts the hand back again, as a second window of the chat does at its load.
    await asking.hands_back(chat)

    try:
        assert asking.wakes == [str(chat)]
        sent = await woken(asking.api, monkeypatch, await asking.session(chat))
        assert sent.count(HANDED_BACK) == 1
        # Its turn over, a second wake, as a queue that delivers twice gives, finds nothing to do.
        await asking.ends(chat)
        harness, handed = waking(asking.api, monkeypatch)
        await harness.wake(chat)
        assert handed == []
        assert await asking.resumes(chat) == [{"source": "browser_hand_back"}]
        assert (await asking.session(chat)).status == "completed"
    finally:
        await asking.unqueue(chat)


async def test_a_hand_backs_turn_cut_off_by_its_workers_death_is_run_once_more_and_reads_the_hand_back_once(
    asking, monkeypatch,
):
    chat = await asking.stopped_while_held()
    await asking.hands_back(chat)
    # The first worker revived the chat, made its request, and died before the model answered.
    harness, handed = waking(asking.api, monkeypatch)
    await harness.wake(chat)
    await asking.store.emit_event(chat, EventType.LLM_REQUEST, {})

    try:
        # The sweeper's wake runs the turn again: the chat is active now, and its request is in the log.
        sent = await woken(asking.api, monkeypatch, await asking.session(chat))
        assert sent[-2:] == [{"role": "assistant", "content": WAITING}, HANDED_BACK]
        assert sent.count(HANDED_BACK) == 1
        assert await asking.resumes(chat) == [{"source": "browser_hand_back"}]
    finally:
        await asking.unqueue(chat)


@pytest.mark.parametrize("command", list(COMMANDS))
async def test_a_hand_back_that_lands_while_a_command_is_being_answered_gives_its_turn_and_runs_no_command_again(
    asking, monkeypatch, command,
):
    chat = await asking.stopped_while_held()

    async def user_hands_back():
        await asking.hands_back(chat)

    # The command's own wake is under way, and moves the cursor past the hand back where it moves it.
    await command_answered(asking, chat, command, meanwhile=user_hands_back)

    try:
        harness, handed = waking(asking.api, monkeypatch)
        ran = commands_of(harness)
        await harness.wake(chat)
        assert [name for name, handler in ran.items() if handler.await_count] == []
        assert [conversation[-2:] for conversation in handed] == [
            [{"role": "assistant", "content": COMMANDS[command][1]}, HANDED_BACK],
        ]
    finally:
        await asking.unqueue(chat)


async def test_a_hand_back_that_lands_as_a_commands_turn_is_ending_still_runs_no_command(asking, monkeypatch):
    chat = await asking.stopped_while_held()
    # The user's last message is a command, answered; the browser is handed back before that turn's
    # end is written, and the turn then ends as a session's does.
    await command_answered(asking, chat, "/goal status")
    await asking.hands_back(chat)
    await asking.ends(chat)

    try:
        harness, handed = waking(asking.api, monkeypatch)
        ran = commands_of(harness)
        await harness.wake(chat)
        assert [name for name, handler in ran.items() if handler.await_count] == []
        assert [conversation[-1] for conversation in handed] == [HANDED_BACK]
        assert await asking.resumes(chat) == [{"source": "browser_hand_back"}]
    finally:
        await asking.unqueue(chat)


@pytest.mark.parametrize("status", ["paused", "failed"])
async def test_a_hand_back_leaves_a_chat_its_user_stopped_or_that_failed_for_them_to_start_again(asking, monkeypatch, status):
    chat = await asking.stopped_while_held()
    await asking.store.update_session_status(chat, status)

    await asking.hands_back(chat)

    try:
        harness, handed = waking(asking.api, monkeypatch)
        await harness.wake(chat)
        assert handed == []
        assert (await asking.session(chat)).status == status
        # The hand back is still read: at the turn their next message starts.
        await asking.says(chat, "Go on.")
        sent = await woken(asking.api, monkeypatch, await asking.session(chat))
        assert sent[-2:] == [{"role": "user", "content": "Go on."}, HANDED_BACK]
    finally:
        await asking.unqueue(chat)


async def test_a_hand_back_during_a_tool_call_of_a_running_turn_is_read_in_that_turn_and_gives_no_second(
    asking, monkeypatch,
):
    chat = await asking.stopped_while_held()
    await asking.says(chat, "Note down where we are.")

    async def user_hands_back():
        await asking.hands_back(chat)

    try:
        requests = await live_turn(
            asking.api, monkeypatch, await asking.session(chat), [TODO_CALL, _final_response("Going on.")],
            during_tool=user_hands_back,
        )
        *_, called, result, note = requests[1]
        assert [called["role"], result["role"]] == ["assistant", "tool"]
        assert note == HANDED_BACK
        # Read in its turn, the hand back gives no second one once that turn ends.
        assert asking.wakes == [str(chat)]
        await asking.ends(chat)
        harness, handed = waking(asking.api, monkeypatch)
        await harness.wake(chat)
        assert handed == []
    finally:
        await asking.unqueue(chat)


async def test_a_hand_back_during_the_agents_reply_is_read_before_its_turn_ends(asking, monkeypatch):
    chat = await asking.stopped_while_held()
    await asking.says(chat, "Where are we?")

    async def user_hands_back():
        await asking.hands_back(chat)

    try:
        requests = await live_turn(
            asking.api, monkeypatch, await asking.session(chat),
            [_final_response("Waiting for the browser."), _final_response("Going on.")],
            during_reply=user_hands_back,
        )
        assert len(requests) == 2
        answer, note = requests[1][-2:]
        assert (answer["role"], answer["content"]) == ("assistant", "Waiting for the browser.")
        assert note == HANDED_BACK
    finally:
        await asking.unqueue(chat)


BOARD_PACK = "Lay the pack out as the board likes it: one page per figure."


async def test_a_hand_backs_turn_reads_the_skill_the_users_last_message_ran(asking, monkeypatch):
    chat = await asking.chat()
    await asking.turn(chat, "Open the report.", "It is open.", tool="browser_navigate", result='{"title": "Report"}')
    await asking.control(chat, "acquire", asking.user)
    # The user's last message ran a skill, at its own wake, and that turn met the pause.
    emit = asking.store.emit_event
    await asking.says(chat, "/board-pack Q3")
    await emit(chat, EventType.SKILL_INVOKED, {"skill": "board-pack", "raw_message": "/board-pack Q3", "staged_at": None})
    await emit(chat, EventType.LLM_REQUEST, {})
    call = {"id": "call-click", "type": "function", "function": {"name": "browser_click", "arguments": "{}"}}
    asked = {"tool_call_id": "call-click", "name": "browser_click"}
    await emit(chat, EventType.LLM_RESPONSE, {"message": {"role": "assistant", "content": "", "tool_calls": [call]}})
    await emit(chat, EventType.TOOL_CALL, asked)
    await emit(chat, EventType.TOOL_RESULT, {**asked, "content": paused_by_user_result(), "elapsed_ms": 3})
    await emit(chat, EventType.LLM_REQUEST, {})
    await emit(chat, EventType.LLM_RESPONSE, {"message": {"role": "assistant", "content": WAITING}})
    await asking.ends(chat)

    await asking.hands_back(chat)

    try:
        harness, handed = waking(asking.api, monkeypatch)
        harness._tools.dispatch = AsyncMock(return_value=json.dumps({"success": True, "content": BOARD_PACK}))
        await harness.wake(chat)
        [conversation] = handed
        # As its own wake sent it, so the model still has the skill's instructions, and the prompt cache the conversation.
        expanded = build_expanded_message(name="board-pack", args="Q3", skill_body=BOARD_PACK)
        assert {"role": "user", "content": expanded} in conversation
        assert {"role": "user", "content": "/board-pack Q3"} not in conversation
        assert conversation[-1] == HANDED_BACK
        # Read again, not run again.
        assert len(await asking.store.get_events(chat, types=[EventType.SKILL_INVOKED])) == 1
    finally:
        await asking.unqueue(chat)


IN_A_PROJECT = {"workspace_boundary": f"{PROJECT_BOUNDARY_PREFIX}{uuid4()}"}


async def test_a_hand_backs_turn_in_a_projects_chat_is_held_and_spent_against_the_users_allowance(asking, monkeypatch):
    # No message route admitted it: as a thread's report, the wake that revives the chat holds its turn.
    chat = await asking.stopped_while_held(**IN_A_PROJECT)
    await asking.hands_back(chat)

    try:
        ops = Ops()
        harness, ran = worker(asking.api, monkeypatch, CAPPED, ops)
        await harness.wake(chat)
        assert ran == [chat]
        assert (ops.held, ops.spent) == ([("allowance", str(asking.api.user_id), "web")], [("allowance", "hold-1", 1500)])
    finally:
        await asking.unqueue(chat)


async def test_a_hand_back_waits_while_the_users_limit_is_spent(asking, monkeypatch):
    chat = await asking.stopped_while_held(**IN_A_PROJECT)
    await asking.hands_back(chat)

    try:
        harness, ran = worker(asking.api, monkeypatch, CAPPED, Ops(allowance_left=False))
        await harness.wake(chat)
        # No turn, and the chat is as it was: not resumed, and not failed for a limit nobody typed into.
        assert ran == []
        assert (await asking.session(chat)).status == "completed"
        assert await asking.resumes(chat) == []
        assert await asking.store.get_events(chat, types=[EventType.SESSION_FAIL]) == []
        # The user's next message, once their limit allows it, reads the hand back.
        assert HANDED_BACK in await replayed(asking.api, await asking.session(chat))
    finally:
        await asking.unqueue(chat)
