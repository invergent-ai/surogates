"""A local-folder chat's browser taken over on its user's computer, and handed back, as the dispatcher sees it.

Against the real database and queue: the control route's own events, the orphan sweeper's query and
one of its passes, and what a wake would find past the chat's cursor.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from surogates.api.app import _install_browser_api_dependencies
from surogates.api.routes import browser as browser_routes
from surogates.config import SHARED_WORK_QUEUE_KEY, encode_queue_member
from surogates.devices.browser import tell_pane
from surogates.harness.loop_messages import maybe_inject_browser_pause
from surogates.harness.loop_pending import _actionable_pending_events
from surogates.orchestrator.dispatcher import Orchestrator
from surogates.session.events import EventType
from surogates.tenant.auth.middleware import get_current_tenant
from surogates.tenant.context import TenantContext

from .conftest import create_org, create_user

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

    def api(self, *, session_scope_id: UUID | None = None) -> FastAPI:
        """The browser routes with the API's own emitter and wake, which enqueues on the real queue."""
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

    async def chat(self, *events: tuple[EventType, dict], **config) -> UUID:
        """A chat of the agent on a folder of this computer, left alone since *events*, which a turn read."""
        session = await self.store.create_session(
            user_id=self.user_id, org_id=self.org_id, agent_id=self.agent_id,
            config={"execution": {"kind": "device", "device_id": str(self.device_id)}, **config},
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

    async def idle(self) -> UUID:
        """A chat whose last turn ended with an answer and left it active, as a command's answer does."""
        return await self.chat((EventType.USER_MESSAGE, {"content": "Open the report."}), ANSWERED)

    async def left(self, *chats: UUID) -> None:
        """Nothing has happened in *chats* for ten minutes, well past the sweeper's threshold."""
        async with self.factory() as db:
            for chat in chats:
                await db.execute(
                    text("UPDATE sessions SET updated_at = now() - make_interval(secs => :s) WHERE id = :sid"),
                    {"s": LEFT, "sid": chat},
                )
            await db.commit()

    async def control(self, chat: UUID, action: str, **api) -> dict:
        async with AsyncClient(transport=ASGITransport(app=self.api(**api)), base_url="http://test") as client:
            response = await client.post(f"/v1/sessions/{chat}/browser/control", json={"action": action})
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


async def test_a_hand_back_wakes_the_agent_once(computer):
    chat = await computer.idle()
    await computer.control(chat, "acquire")
    assert not await computer.queued(chat)

    assert (await computer.control(chat, "release"))["outcome"] == "released"

    try:
        # Handed back, its agent goes on: queued by the hand back itself, with the hand back for its wake to act on.
        assert computer.wakes == [str(chat)]
        assert await computer.queued(chat)
        assert await computer.work(chat) == ["browser.control_returned"]
        # Handed back already: a repeat tells the chat nothing more and wakes nobody.
        await computer.control(chat, "release")
        assert computer.wakes == [str(chat)]
        assert await computer.log(chat) == [
            "user.message", "llm.response", "browser.control_granted", "browser.control_returned",
        ]
    finally:
        await computer.unqueue(chat)


async def test_a_hand_back_whose_wake_was_lost_is_found_by_the_sweeper(computer):
    chat = await computer.idle()
    await computer.control(chat, "acquire")
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
    control = computer.api().state.browser_control
    assert await control.held_by(str(chat)) is None
    session = await computer.store.get_session(chat)
    assert await maybe_inject_browser_pause(session=session, browser_control=control) is None
