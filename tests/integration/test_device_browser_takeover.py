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
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import surogates.harness.loop as loop_module
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
from surogates.runtime import SLASH_COMMAND_IDS, SlashCommandConfig
from surogates.session.events import EventType
from surogates.session.store import CONTROL_LOCK_WAIT_MS, SessionStore
from surogates.tenant.auth.jwt import create_access_token, create_service_account_session_token
from surogates.tenant.auth.middleware import get_current_tenant
from surogates.tenant.auth.oauth import OAuthTokens
from surogates.tenant.context import TenantContext

from tests.test_steer_loop import _final_response

from .conftest import create_org, create_user, issue_service_account_token
from .test_devices import AGENT_ID, add_user, api  # noqa: F401  (api is a fixture)
from .test_oauth import add_computer, signed_in, window_session
from .test_workstream_spend import CAPPED, Down, Ops, worker
from .test_workstream_spend import served as metered
from .test_workstream_threads import TODO_CALL, harness_of, live_turn, replayed, waking, woken

pytestmark = pytest.mark.asyncio(loop_scope="session")

# The sweeper's own threshold, and a chat left alone for longer.
STALE, LEFT = 60, 600

ANSWERED = (EventType.LLM_RESPONSE, {"message": {"role": "assistant", "content": "It is open."}})

# What the control route answers a release of a chat on a folder: whether its agent goes on by itself.
GOES_ON = {"outcome": "released", "resumes": True}
FOR_THE_PANE = {"outcome": "released", "resumes": False}


class Computer:
    """One user's computer in a test: the chats of an agent on its folders, Surogate Desktop signed in
    on it, the API they are told through, and the dispatcher's sweeper for that agent."""

    def __init__(self, session_store, session_factory, redis_client, org_id: UUID, user_id: UUID, *, pool=None) -> None:
        self.store, self.factory, self.redis = session_store, session_factory, redis_client
        # The database the API itself asks: the tests' own, or a pool as narrow as a test makes it.
        self.pool = pool or session_factory
        self.org_id, self.user_id = org_id, user_id
        self.agent_id = f"takeover-agent-{uuid4()}"
        self.device_id = uuid4()
        self.desktop: UUID | None = None
        self.wakes: list[str] = []
        self.sweeper = Orchestrator(
            redis_client=redis_client, session_store=session_store, harness_factory=lambda _sid: None,
            agent_id=self.agent_id, queue_key=SHARED_WORK_QUEUE_KEY,
        )

    async def signed_in(self) -> Computer:
        """Surogate Desktop is signed in on this computer, by its user, and added it."""
        self.desktop = await self.sign_in(on=self.device_id)
        return self

    async def sign_in(self, *, on: UUID | None, days_ago: int = 0) -> UUID:
        """A sign-in of Surogate Desktop by the computer's user, *days_ago*, which added the computer
        *on*, or none yet: the sign-in whose session the web client in that desktop's window posts with."""
        tokens = OAuthTokens(self.factory)
        grant = await tokens.issue(
            org_id=self.org_id, user_id=self.user_id, agent_id=self.agent_id, client_id="surogate-desktop",
            auth_time=int(time.time()) - days_ago * 86400,
        )
        if on is not None:
            await tokens.bind(grant.family_id, on)
        return grant.family_id

    def served(self, *, session_scope_id: UUID | None = None, sign_in: UUID | None = None) -> FastAPI:
        """The browser routes with the API's own emitter and wake, which enqueues on the real queue,
        for a caller who is the computer's user, under *sign_in* where the desktop's, or a worker with
        a token for one session."""
        app = FastAPI()
        app.include_router(browser_routes.router, prefix="/v1")
        app.state.redis, app.state.session_store, app.state.session_factory = self.redis, self.store, self.pool
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
                session_scope_id=session_scope_id, oauth_family_id=sign_in,
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

    async def resumed(self, chat: UUID) -> list[dict]:
        """What gave the chat a turn with no message of its user's."""
        return [event.data for event in await self.store.get_events(chat, types=[EventType.SESSION_RESUME])]

    async def left(self, *chats: UUID) -> None:
        """Nothing has happened in *chats* for ten minutes, well past the sweeper's threshold."""
        async with self.factory() as db:
            for chat in chats:
                await db.execute(
                    text("UPDATE sessions SET updated_at = now() - make_interval(secs => :s) WHERE id = :sid"),
                    {"s": LEFT, "sid": chat},
                )
            await db.commit()

    async def control(
        self, chat: UUID, action: str, *, token_of: UUID | None = None, handed_back: bool | None = None,
        answered: int = 200, leaves: bool = True, **who,
    ) -> dict:
        """Post *action* to the chat's control route, as the web client in the desktop's window on this
        computer posts it, unless *who* gives another ``sign_in`` (None: a browser's, the desktop's
        on another computer, one that ended); or with a worker's token, which covers the one session
        *token_of* and names the user it speaks for. *handed_back* is the pane's word, with a release,
        that its user confirmed the hand back in the desktop."""
        said: dict = {"action": action, **({"owner_user_id": str(self.user_id)} if token_of else {})}
        if handed_back is not None:
            said["handed_back"] = handed_back
        path = f"/v1{'/api' if token_of else ''}/sessions/{chat}/browser/control"
        app = self.served(session_scope_id=token_of, sign_in=None if token_of else who.get("sign_in", self.desktop))
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(path, json=said)
        assert response.status_code == answered, response.text
        # Its events bumped the chat's clock: it is left alone again since, unless a test *leaves* it as it is.
        if leaves:
            await self.left(chat)
        return response.json()

    async def hands_back(self, chat: UUID, **who) -> dict:
        """The chat's user hands the browser back, and confirms it in the desktop: its pane posts that."""
        return await self.control(chat, "release", handed_back=True, **who)

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
    user_id = await create_user(session_factory, org_id)
    return await Computer(session_store, session_factory, redis_client, org_id, user_id).signed_in()


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


async def test_a_hand_back_its_user_confirmed_gives_the_chats_agent_one_turn(computer):
    chat = await computer.idle()
    await computer.control(chat, "acquire")
    assert not await computer.queued(chat)

    assert await computer.hands_back(chat) == GOES_ON

    try:
        # The chat's pane is told the agent goes on. The resume written with it is the turn: what queues
        # the chat, and its wake's work.
        assert await computer.handed_back(chat) == {
            "session_id": str(chat), "released_by": str(computer.user_id), "computer": True, "resumes": True,
        }
        assert (await computer.log(chat))[-2:] == ["browser.control_returned", "session.resume"]
        assert await computer.resumed(chat) == [{"source": "browser_hand_back"}]
        assert computer.wakes == [str(chat)]
        assert await computer.queued(chat)
        assert await computer.work(chat) == ["session.resume"]
        # Handed back already: a repeat is answered as the hand back was, tells the chat nothing more,
        # and gives no second turn.
        assert await computer.hands_back(chat) == GOES_ON
        assert computer.wakes == [str(chat)]
        log = await computer.log(chat)
        assert (log.count("browser.control_returned"), log.count("session.resume")) == (1, 1)
        # A release that is no confirmed hand back is answered that nobody goes on for it.
        assert await computer.control(chat, "release") == FOR_THE_PANE
        # Once the turn was read, there is none to come: one posted then hands nothing back.
        await computer.store.emit_event(chat, EventType.LLM_REQUEST, {})
        assert await computer.hands_back(chat) == FOR_THE_PANE
        assert computer.wakes == [str(chat)]
    finally:
        await computer.unqueue(chat)


QUOTED = json.dumps({"value": json.loads(paused_by_user_result())})
SINCE_THE_TAKE_OVER = [
    "nothing", "its agent met the pause", "a sub-agent of its met the pause", "its agent read a page that quotes the pause",
]


async def since_the_take_over(computer, chat: UUID, happened: str) -> None:
    if happened == "its agent met the pause":
        await computer.stopped(chat)
    elif happened == "a sub-agent of its met the pause":
        await computer.stopped(chat, by=await computer.under(chat))
    elif happened == "its agent read a page that quotes the pause":
        await computer.turn(chat, "What does the page say?", "browser_evaluate", QUOTED)


@pytest.mark.parametrize("happened", SINCE_THE_TAKE_OVER)
async def test_a_confirmed_hand_back_gives_the_turn_whatever_the_chats_log_says_of_the_take_over(computer, happened):
    chat = await computer.idle()
    await computer.control(chat, "acquire")
    await since_the_take_over(computer, chat, happened)

    try:
        # Nothing is read from the log: the agent that asked its user to take the browser, and waited
        # with no browser call of its own, goes on as the one that met the pause does.
        assert await computer.hands_back(chat) == GOES_ON
        assert computer.wakes == [str(chat)]
    finally:
        await computer.unqueue(chat)


@pytest.mark.parametrize("happened", SINCE_THE_TAKE_OVER)
@pytest.mark.parametrize("said", [{}, {"handed_back": False}], ids=["no word of it", "said not to be one"])
async def test_a_release_that_is_no_confirmed_hand_back_is_for_the_chats_pane_alone(computer, said, happened):
    chat = await computer.idle()
    await computer.control(chat, "acquire")
    await since_the_take_over(computer, chat, happened)
    before = await computer.log(chat)

    # As the pane posts one at a chat's opening, where the app ended while its user held the browser, and
    # for a chat the browser was not taken over from: nobody handed anything back there.
    assert await computer.control(chat, "release", **said) == FOR_THE_PANE

    # The chat is told, and that is all: nobody is woken, nothing is pending, nothing is swept, and
    # nothing is left for a later turn to read.
    assert await computer.handed_back(chat) == {
        "session_id": str(chat), "released_by": str(computer.user_id), "computer": True,
    }
    assert await computer.log(chat) == [*before, "browser.control_returned"]
    assert computer.wakes == []
    assert not await computer.queued(chat)
    assert await computer.work(chat) == []
    assert await computer.orphans() == set()
    assert await computer.sweep() == 0


@pytest.mark.parametrize("posted_from", [
    "the web client in a browser", "the desktop on another computer of theirs", "a desktop that added no computer",
    "a desktop whose sign-in ended", "a desktop whose sign-in is past its thirty days",
])
async def test_a_hand_back_is_taken_as_confirmed_only_from_the_desktops_window_on_the_chats_computer(
    computer, posted_from,
):
    chat = await computer.idle()
    await computer.control(chat, "acquire")
    await computer.stopped(chat)
    if posted_from == "the web client in a browser":
        sign_in = None
    elif posted_from == "the desktop on another computer of theirs":
        sign_in = await computer.sign_in(on=uuid4())
    elif posted_from == "a desktop that added no computer":
        sign_in = await computer.sign_in(on=None)
    elif posted_from == "a desktop whose sign-in is past its thirty days":
        sign_in = await computer.sign_in(on=computer.device_id, days_ago=31)
    else:
        sign_in = await computer.sign_in(on=computer.device_id)
        assert await OAuthTokens(computer.factory).end(
            sign_in, org_id=computer.org_id, user_id=computer.user_id, agent_id=computer.agent_id,
        )

    # The confirmation is drawn by the desktop on the chat's computer: from anywhere else nothing was
    # confirmed there, whatever the post says.
    assert await computer.hands_back(chat, sign_in=sign_in) == FOR_THE_PANE

    assert "resumes" not in await computer.handed_back(chat)
    assert (computer.wakes, await computer.resumed(chat), await computer.work(chat)) == ([], [], [])
    assert await computer.orphans() == set()


async def test_a_hand_back_to_a_chat_with_a_turn_under_way_is_made_and_gives_no_turn(computer):
    chat = await computer.idle()
    await computer.control(chat, "acquire")
    lease = await computer.store.try_acquire_lease(chat, "the-turns-worker")

    try:
        # The browser is the agent's again on the computer whatever the server answers: it answers 200,
        # and that the agent does not go on by itself.
        assert await computer.hands_back(chat) == FOR_THE_PANE
    finally:
        await computer.store.release_lease(chat, lease.lease_token)
    await computer.left(chat)

    # Nothing is kept for when that turn is over.
    assert "resumes" not in await computer.handed_back(chat)
    assert (computer.wakes, await computer.resumed(chat), await computer.work(chat)) == ([], [], [])
    assert await computer.orphans() == set()
    assert not await computer.queued(chat)


@pytest.mark.parametrize("status", ["paused", "failed"])
async def test_a_hand_back_to_a_chat_its_user_stopped_or_that_failed_is_made_and_gives_no_turn(computer, status):
    chat = await computer.idle()
    await computer.control(chat, "acquire")
    await computer.store.update_session_status(chat, status)

    assert await computer.hands_back(chat) == FOR_THE_PANE

    assert (await computer.store.get_session(chat)).status == status
    assert (computer.wakes, await computer.resumed(chat), await computer.work(chat)) == ([], [], [])
    assert not await computer.queued(chat)


async def test_a_take_over_and_a_hand_back_posted_from_a_sub_agents_view_are_its_chats(computer):
    chat = await computer.idle()
    child = await computer.under(chat)

    assert (await computer.control(child, "acquire"))["outcome"] == "granted"
    # Told to the chat already: a second, from the chat's own view, changes nothing.
    assert (await computer.control(chat, "acquire"))["outcome"] == "refreshed"
    assert await computer.hands_back(child) == GOES_ON

    try:
        # The chat is told both, and its agent is given the turn: the browser is held for the chat and
        # every session under it. The sub-agent is told nothing, and is not woken.
        assert (await computer.log(chat))[-3:] == ["browser.control_granted", "browser.control_returned", "session.resume"]
        assert (await computer.handed_back(chat))["session_id"] == str(chat)
        assert await computer.log(child) == []
        assert computer.wakes == [str(chat)]
        assert not await computer.queued(child)
    finally:
        await computer.unqueue(chat, child)


async def test_a_session_that_names_another_users_chat_as_the_one_it_works_under_tells_that_chat_nothing(
    computer, session_factory,
):
    # As no session is made: one under a chat is its user's. The chat is another user's, held by them.
    theirs = await computer.told_taken_over(user_id=await create_user(session_factory, computer.org_id))
    before = await computer.log(theirs)
    stranger = await computer.store.create_session(
        user_id=computer.user_id, org_id=computer.org_id, agent_id=computer.agent_id,
        config={
            "execution": {"kind": "device", "device_id": str(computer.device_id)},
            "sandbox_root_session_id": str(theirs),
        },
    )

    # Its own user's posts are answered as for a chat that has no browser: neither told, nor handed back.
    no_such_chat = {"detail": "No browser for session"}
    assert await computer.control(stranger.id, "acquire", answered=404) == no_such_chat
    assert await computer.control(stranger.id, "release", handed_back=True, answered=404) == no_such_chat

    assert await computer.log(theirs) == before
    assert await computer.log(stranger.id) == []
    assert computer.wakes == []


# As many connections as posts arrive at once, and no long wait for one: a telling that needed a second
# connection while it held its first would leave none, and every post would wait this out and fail.
@pytest.mark.parametrize("status", ["paused", "failed", "archived"])
async def test_a_chat_stopped_failed_or_deleted_as_its_hand_back_is_posted_is_given_no_turn(computer, monkeypatch, status):
    chat = await computer.idle()
    await computer.control(chat, "acquire")
    tell = computer.store.tell_browser_control

    # The route has looked at the chat, and held its turn against its user's limit, when the stop lands.
    async def stopped_meanwhile(session_id, event_type, *args, **kwargs):
        if event_type is EventType.BROWSER_CONTROL_RETURNED:
            await computer.store.update_session_status(session_id, status)
        return await tell(session_id, event_type, *args, **kwargs)

    monkeypatch.setattr(computer.store, "tell_browser_control", stopped_meanwhile)

    assert await computer.hands_back(chat) == FOR_THE_PANE

    # The hand back is made and told, and says no turn was given: the stop is not undone.
    assert (await computer.log(chat))[-1] == "browser.control_returned"
    assert "resumes" not in await computer.handed_back(chat)
    assert (await computer.store.get_session(chat)).status == status
    assert (computer.wakes, await computer.resumed(chat), await computer.work(chat)) == ([], [], [])
    assert not await computer.queued(chat)
    # A repeat is answered the same.
    assert await computer.hands_back(chat) == FOR_THE_PANE


async def test_a_hand_back_whose_wake_could_not_be_queued_has_given_its_turn_and_a_repeat_says_so(computer, monkeypatch):
    chat = await computer.idle()
    await computer.control(chat, "acquire")
    served = computer.served

    def with_the_queue_away(**who) -> FastAPI:
        app = served(**who)

        async def no_queue(session_id: str) -> None:
            raise RuntimeError("the queue is away")

        app.state.session_wake = no_queue
        return app

    monkeypatch.setattr(computer, "served", with_the_queue_away)
    with pytest.raises(RuntimeError, match="the queue is away"):
        await computer.hands_back(chat)
    monkeypatch.undo()
    await computer.left(chat)

    try:
        # What the log says is what happened: the turn was given, and only its wake is missing.
        assert (await computer.handed_back(chat))["resumes"] is True
        assert await computer.resumed(chat) == [{"source": "browser_hand_back"}]
        assert (await computer.store.get_session(chat)).status == "active"
        assert not await computer.queued(chat)
        # A repeat is answered that the agent goes on, and gives no second turn.
        assert await computer.hands_back(chat) == GOES_ON
        log = await computer.log(chat)
        assert (log.count("browser.control_returned"), log.count("session.resume")) == (1, 1)
        # The sweeper finds the chat, and queues the wake.
        assert await computer.orphans() == {chat}
        assert await computer.sweep() == 1
        assert await computer.queued(chat)
    finally:
        await computer.unqueue(chat)


async def test_a_hand_back_and_the_turn_it_gives_are_announced_to_the_chats_listeners_once_written(
    session_factory, redis_client,
):
    org_id = await create_org(session_factory)
    announcing = SessionStore(session_factory, redis=redis_client)
    computer = await Computer(announcing, session_factory, redis_client, org_id, await create_user(session_factory, org_id)).signed_in()
    chat = await computer.idle()
    listening = redis_client.pubsub()
    await listening.subscribe(f"surogates:session:{chat}")

    async def heard(expected: int) -> list[str]:
        """The kinds of the events announced since, read until *expected* came and a moment more."""
        kinds: list[str] = []
        until = time.monotonic() + 5.0
        while time.monotonic() < until:
            message = await listening.get_message(ignore_subscribe_messages=True, timeout=0.3)
            if message is not None:
                kinds.append(message["data"].decode().split(":", 1)[1])
            elif len(kinds) >= expected:
                break
        return kinds

    try:
        await computer.control(chat, "acquire")
        assert await heard(1) == ["browser.control_granted"]
        # The pane hears the hand back, and whoever follows the chat hears its turn begin.
        await computer.hands_back(chat)
        assert await heard(2) == ["browser.control_returned", "session.resume"]
    finally:
        await listening.aclose()
        await computer.unqueue(chat)


async def test_a_hand_back_whose_turn_cannot_be_written_is_not_told_and_can_be_made_again(computer, monkeypatch):
    chat = await computer.idle()
    await computer.store.update_session_status(chat, "completed")
    await computer.control(chat, "acquire")
    write = SessionStore._write_event

    async def no_resume(self, db, event) -> None:
        if event.event_type is EventType.SESSION_RESUME:
            raise RuntimeError("the database went away")
        await write(self, db, event)

    monkeypatch.setattr(SessionStore, "_write_event", no_resume, raising=False)
    with pytest.raises(RuntimeError, match="the database went away"):
        await computer.hands_back(chat)
    monkeypatch.undo()

    try:
        # The telling, the chat made active and its resume are written together or not at all: the
        # take-over still stands, and no hand back says it gave a turn that was never written.
        assert (await computer.log(chat))[-1] == "browser.control_granted"
        assert (await computer.store.get_session(chat)).status == "completed"
        assert (computer.wakes, await computer.resumed(chat)) == ([], [])
        # Made again, it gives the turn.
        assert await computer.hands_back(chat) == GOES_ON
        assert (await computer.log(chat))[-2:] == ["browser.control_returned", "session.resume"]
        assert computer.wakes == [str(chat)]
    finally:
        await computer.unqueue(chat)


POOL, POOL_WAIT_S = 4, 3.0


@asynccontextmanager
async def pooled(pg_url, session_factory, redis_client, connections: int):
    """A computer whose API has a database pool of *connections*: its store's, and every other read
    and write a post makes."""
    engine = create_async_engine(
        pg_url, pool_size=connections, max_overflow=0, pool_timeout=POOL_WAIT_S, connect_args={"statement_cache_size": 0},
    )
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    narrow = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield await Computer(SessionStore(narrow), session_factory, redis_client, org_id, user_id, pool=narrow).signed_in()
    finally:
        await engine.dispose()


@pytest_asyncio.fixture(loop_scope="session")
async def narrow(pg_url, session_factory, redis_client):
    async with pooled(pg_url, session_factory, redis_client, POOL) as computer:
        yield computer


async def test_a_take_over_and_a_hand_back_are_told_through_one_connection(pg_url, session_factory, redis_client):
    # A pool of one: anything in a post that held a connection while it asked for another would wait
    # for itself until the pool gave up.
    async with pooled(pg_url, session_factory, redis_client, 1) as single:
        chat, held = await single.idle(), await single.told_taken_over()
        try:
            assert (await single.control(chat, "acquire"))["outcome"] == "granted"
            assert await single.hands_back(chat) == GOES_ON
            assert (await single.log(chat))[-2:] == ["browser.control_returned", "session.resume"]
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


async def test_as_many_hand_backs_of_one_chat_at_once_as_the_pool_is_wide_give_it_one_turn_and_leave_a_read_its_connection(
    narrow,
):
    chat, another = await narrow.idle(), await narrow.idle()
    await narrow.control(chat, "acquire")
    started = time.monotonic()

    async def an_unrelated_read() -> float:
        await narrow.store.get_session(another)
        return time.monotonic() - started

    try:
        *answers, read_after = await asyncio.gather(*(narrow.hands_back(chat) for _ in range(POOL)), an_unrelated_read())
        # Each is answered that the agent goes on, as it does, for the one of them that was told.
        assert answers == [GOES_ON] * POOL
        assert read_after < POOL_WAIT_S / 2
        assert time.monotonic() - started < POOL_WAIT_S / 2
        log = await narrow.log(chat)
        assert (log.count("browser.control_returned"), log.count("session.resume")) == (1, 1)
        assert narrow.wakes == [str(chat)]
    finally:
        await narrow.unqueue(chat)


BUSY = {"detail": "The chat is being told of its browser by another request. Post it again."}


HELD = {
    "its lock": select(func.pg_advisory_xact_lock(func.hashtext(text("'browser-control:' || :chat")))),
    "its row": text("SELECT 1 FROM sessions WHERE id = CAST(:chat AS uuid) FOR UPDATE"),
}


@pytest.mark.parametrize("held", HELD)
@pytest.mark.parametrize("posted", ["acquire", "release"])
async def test_posts_kept_waiting_for_one_chats_lock_are_answered_busy_and_leave_the_pool_its_connections(
    pg_url, session_factory, redis_client, posted, held,
):
    async with pooled(pg_url, session_factory, redis_client, 2) as narrow:
        chat, other = await (narrow.idle() if posted == "acquire" else narrow.told_taken_over()), await narrow.idle()
        before = await narrow.log(chat)
        said = {"handed_back": True} if posted == "release" else {}
        # Whatever holds the chat's lock, or the row a telling writes to under it, holds it for longer
        # than a telling takes: here, from outside.
        async with session_factory() as holder:
            await holder.execute(HELD[held], {"chat": str(chat)})
            started = time.monotonic()
            # As many posts for that chat as the pool is wide: each waits on a connection of its own.
            answers = await asyncio.wait_for(
                asyncio.gather(*(narrow.control(chat, posted, answered=503, leaves=False, **said) for _ in range(2))), 10.0,
            )
            waited = time.monotonic() - started
            # They give up, are answered that nothing was told, and the pool has its connections back
            # while the lock is still held: a read of another chat is not kept waiting. Each waited its
            # time for the lock, and the one that got it for the row.
            assert answers == [BUSY, BUSY]
            assert CONTROL_LOCK_WAIT_MS / 1000 <= waited < 2 * CONTROL_LOCK_WAIT_MS / 1000 + 1
            await asyncio.wait_for(narrow.store.get_session(other), 1.0)
            await holder.rollback()
        try:
            # Nothing was told, and no turn given. Posted again, it is.
            assert await narrow.log(chat) == before
            assert narrow.wakes == []
            again = await narrow.control(chat, posted, **said)
            assert again == (GOES_ON if posted == "release" else {"outcome": "granted", "owner_user_id": str(narrow.user_id)})
        finally:
            await narrow.unqueue(chat)


async def test_two_hand_backs_posted_together_tell_the_chat_one_and_give_its_agent_one_turn(computer, monkeypatch):
    chat = await computer.idle()
    await computer.control(chat, "acquire")
    # The chat is open in two windows, and each posts the hand back. The first is slow to be written:
    # time enough for the second to read what the chat was last told, were it not made to wait.
    write = SessionStore._write_event

    async def slow_to_tell(self, db, event) -> None:
        if event.event_type is EventType.BROWSER_CONTROL_RETURNED:
            await asyncio.sleep(0.3)
        await write(self, db, event)

    monkeypatch.setattr(SessionStore, "_write_event", slow_to_tell)

    try:
        answers = await asyncio.gather(computer.hands_back(chat), computer.hands_back(chat))
        assert answers == [GOES_ON, GOES_ON]
        log = await computer.log(chat)
        assert (log.count("browser.control_returned"), log.count("session.resume")) == (1, 1)
        assert computer.wakes == [str(chat)]
    finally:
        await computer.unqueue(chat)


async def test_a_hand_back_gives_its_turn_to_the_chat_it_was_taken_over_from_and_to_no_other(computer):
    # The browser is held for every chat of the agent's there: this other one's call answered paused too.
    held, other = await computer.idle(), await computer.idle()
    await computer.control(held, "acquire")
    await computer.stopped(held)
    await computer.stopped(other)
    before = await computer.log(other)

    await computer.hands_back(held)

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


@pytest.mark.parametrize("left", ["active", "completed"], ids=["left active by a command", "its turn ended"])
async def test_a_hand_back_whose_wake_was_lost_is_found_by_the_sweeper(computer, left):
    chat = await computer.idle()
    await computer.store.update_session_status(chat, left)
    await computer.control(chat, "acquire")
    await computer.hands_back(chat)
    # The wake the hand back queued never ran.
    await computer.unqueue(chat)

    try:
        # The chat is active for its turn, as a typed message leaves one, and looks left half done.
        assert (await computer.store.get_session(chat)).status == "active"
        assert await computer.orphans() == {chat}
        assert await computer.sweep() == 1
        assert await computer.queued(chat)
    finally:
        await computer.unqueue(chat)


async def test_a_hand_back_whose_user_took_the_browser_over_again_before_its_wake_ran_is_left_alone_by_the_sweeper(computer):
    chat = await computer.idle()
    await computer.control(chat, "acquire")
    await computer.hands_back(chat)
    # The wake the hand back queued never ran, and its user has the browser again.
    await computer.unqueue(chat)
    assert await computer.orphans() == {chat}
    assert (await computer.control(chat, "acquire"))["outcome"] == "granted"

    try:
        # A wake would find no work in it, so the sweeper recovers nothing: pass after pass would fail the chat.
        assert (await computer.log(chat))[-3:] == ["browser.control_returned", "session.resume", "browser.control_granted"]
        assert await computer.work(chat) == []
        assert await computer.orphans() == set()
        assert await computer.sweep() == 0
        # Handed back anew, and that wake lost too: found as any is.
        await computer.hands_back(chat)
        await computer.unqueue(chat)
        assert await computer.orphans() == {chat}
    finally:
        await computer.unqueue(chat)


async def test_a_resume_passed_over_for_a_later_take_over_is_a_hand_backs_and_the_take_over_a_computers(computer):
    emit = computer.store.emit_event
    another_resume, the_clouds = await computer.idle(), await computer.idle()
    # A resume for any other reason is a turn to run, whoever took the browser over after it.
    await emit(another_resume, EventType.SESSION_RESUME, {"source": "user_retry"})
    await emit(another_resume, EventType.BROWSER_CONTROL_GRANTED, {"computer": True})
    # And a hand back's is, where what followed it names no computer.
    await emit(the_clouds, EventType.SESSION_RESUME, {"source": "browser_hand_back"})
    await emit(the_clouds, EventType.BROWSER_CONTROL_GRANTED, {"owner_user_id": str(computer.user_id)})
    await computer.left(another_resume, the_clouds)

    assert await computer.orphans() == {another_resume, the_clouds}


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


async def test_a_release_made_from_another_chat_for_one_that_is_gone_is_told_to_both_and_wakes_nobody(computer):
    first, other = await computer.idle(), await computer.idle()
    await computer.control(first, "acquire")
    # Its agent met the pause, and so did the other chat's, the browser being held for both.
    await computer.stopped(first)
    await computer.stopped(other)
    # The first chat gone from the computer, its user hands the browser back from another: that chat's
    # pane tells its own route of the take-over, then of a release that is no hand back of its own.
    await computer.control(other, "acquire")
    assert await computer.control(other, "release") == FOR_THE_PANE
    await computer.left(first)

    # The first chat no longer says its user holds the browser: it is told the hand back, and where it was made.
    assert await computer.handed_back(first) == {
        "session_id": str(first), "released_by": str(computer.user_id), "computer": True,
        "handed_back_from": str(other),
    }
    assert (await computer.log(first))[-2:] == ["llm.response", "browser.control_returned"]
    # For the panes: neither agent is woken or has work, and neither chat looks crashed.
    assert "resumes" not in await computer.handed_back(other)
    assert computer.wakes == []
    assert (await computer.resumed(first), await computer.resumed(other)) == ([], [])
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
        # Nothing stands to hand back in the first chat: its pane's release at its next load is passed over,
        # and so is a hand back said to be confirmed.
        assert await computer.control(first, "release") == FOR_THE_PANE
        assert await computer.hands_back(first) == FOR_THE_PANE
        assert len(await computer.log(first)) == told
        # Taken over anew, it is told anew; handed back from itself, its own agent is given the turn.
        assert (await computer.control(first, "acquire"))["outcome"] == "granted"
        assert await computer.hands_back(first) == GOES_ON
        assert (await computer.log(first))[told:] == ["browser.control_granted", "browser.control_returned", "session.resume"]
        assert computer.wakes == [str(first)]
        assert await computer.work(first) == ["session.resume"]
    finally:
        await computer.unqueue(first, other)


async def test_a_hand_back_is_told_to_no_chat_but_the_users_own_with_the_agent_on_that_computer_still_taken_over(
    computer, session_factory,
):
    here = await computer.idle()
    # A chat handed back already, before any of the others was taken over.
    handed_back = await computer.told_taken_over()
    await computer.control(handed_back, "release")
    # The browser on this computer is one for the agent's chats there: these still say their user holds it.
    standing = [await computer.told_taken_over(), await computer.told_taken_over()]
    # These do not, or are not that browser's: never taken over, handed back already, the agent's on
    # another computer, another agent's here, another user's, and one its user deleted.
    never = await computer.idle()
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
    await computer.hands_back(here)

    try:
        # Each is told for its pane, naming the chat the hand back was made from: only that one's agent goes on.
        for chat in standing:
            assert (await computer.log(chat))[-2:] == ["browser.control_granted", "browser.control_returned"]
            assert await computer.handed_back(chat) == {
                "session_id": str(chat), "released_by": str(computer.user_id), "computer": True,
                "handed_back_from": str(here),
            }
        assert {chat: await computer.log(chat) for chat in untold} == before
        assert computer.wakes == [str(here)]
    finally:
        await computer.unqueue(here)


async def test_a_hand_back_with_no_take_over_standing_in_its_chat_tells_no_chat_and_gives_no_turn(computer):
    first, other = await computer.told_taken_over(), await computer.idle()

    # As a chat's pane posts at its load, where the computer says nobody holds the browser: passed over.
    # And one said to be confirmed, with nothing to hand back, is passed over as well.
    assert await computer.control(other, "release") == FOR_THE_PANE
    assert await computer.hands_back(other) == FOR_THE_PANE

    assert (await computer.log(first))[-1] == "browser.control_granted"
    assert (await computer.log(other))[-1] == "llm.response"
    assert computer.wakes == []


async def test_a_token_for_one_chat_hands_back_no_other_chats_take_over_and_gives_no_turn(computer):
    first, other = await computer.told_taken_over(), await computer.idle()

    # A worker's token covers its own session: it tells that one, and no other chat of the computer.
    # It is no desktop's window: nothing it says was confirmed there.
    await computer.control(other, "acquire", token_of=other)
    assert await computer.control(other, "release", token_of=other, handed_back=True) == FOR_THE_PANE

    assert (await computer.log(other))[-2:] == ["browser.control_granted", "browser.control_returned"]
    assert (await computer.log(first))[-1] == "browser.control_granted"
    assert computer.wakes == []


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


async def test_a_computers_hand_back_is_passed_over_by_the_sweeper_whatever_it_says_it_gave(computer):
    chat = await computer.told_taken_over()
    # The hand back's own event is for the pane even where it says it gave a turn: the turn is the
    # resume written with it, and without one there is nothing to recover.
    await computer.store.emit_event(chat, EventType.BROWSER_CONTROL_RETURNED, {
        "session_id": str(chat), "released_by": str(computer.user_id), "computer": True, "resumes": True,
    })
    await computer.left(chat)

    assert await computer.orphans() == set()
    assert await computer.sweep() == 0
    assert not await computer.queued(chat)


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
    Surogate Desktop signed in on it, their browser routes told through the API's own emitter, and
    each wake recorded."""

    def __init__(self, the_api, session_factory) -> None:
        self.api, self.factory = the_api, session_factory
        state = the_api.app.state
        self.store = state.session_store
        self.device_id = uuid4()
        self.window: tuple[str, dict[str, str]] = ("/v1", {})
        self.wakes: list[str] = []
        _install_browser_api_dependencies(the_api.app, SimpleNamespace(browser=SimpleNamespace(backend=None)))
        enqueue = state.session_wake

        async def wake(session_id: str) -> None:
            self.wakes.append(session_id)
            await enqueue(session_id)

        state.session_wake = wake

    async def signed_in(self) -> Asking:
        """Surogate Desktop signs in as the app's user and adds their computer, and its window is given
        a session of that sign-in: the one the web client there posts with."""
        desktop = await signed_in(self.api)
        self.device_id = UUID(await add_computer(self.api, desktop["access_token"]))
        self.window = ("/v1", self.api.auth((await window_session(self.api, desktop["access_token"]))["access_token"]))
        return self

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
            "its user, in the desktop's window": self.window,
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

    async def control(
        self, chat: UUID, action: str, caller: tuple[str, dict[str, str]], *, handed_back: bool | None = None,
    ) -> tuple[int, dict]:
        prefix, sign_in = caller
        # On the service path a caller names the user it speaks for: here always the chat's own.
        said: dict = {"action": action, **({"owner_user_id": str(self.api.user_id)} if prefix == "/v1/api" else {})}
        if handed_back is not None:
            said["handed_back"] = handed_back
        response = await self.api.client.post(f"{prefix}/sessions/{chat}/browser/control", json=said, headers=sign_in)
        return response.status_code, response.json()

    async def log(self, chat: UUID) -> list[str]:
        return [event.type for event in await self.store.get_events(chat)]

    async def unqueue(self, *chats: UUID) -> None:
        for chat in chats:
            member = encode_queue_member(org_id=str(self.api.org_id), agent_id=AGENT_ID, session_id=str(chat))
            await self.api.app.state.redis.zrem(SHARED_WORK_QUEUE_KEY, member)

    async def queued(self, chat: UUID) -> bool:
        member = encode_queue_member(org_id=str(self.api.org_id), agent_id=AGENT_ID, session_id=str(chat))
        return await self.api.app.state.redis.zscore(SHARED_WORK_QUEUE_KEY, member) is not None

    @property
    def user(self) -> tuple[str, dict[str, str]]:
        """The chats' own user, as the web client in a browser calls their routes."""
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

    async def held(self, **config) -> UUID:
        """A chat whose agent opened a page and ended its turn, and whose user then took the browser
        over, in the desktop."""
        chat = await self.chat(**config)
        await self.turn(chat, "Open the report.", OPENED, tool="browser_navigate", result='{"title": "Report"}')
        assert (await self.control(chat, "acquire", self.window))[0] == 200
        return chat

    async def stopped_while_held(self, **config) -> UUID:
        """A chat whose user holds the browser, and whose agent met the pause at its next turn and said
        so: its turn ended, as every turn does."""
        chat = await self.held(**config)
        await self.turn(chat, "Click Next.", WAITING, tool="browser_click", result=paused_by_user_result())
        return chat

    async def hands_back(self, chat: UUID, *, goes_on: bool = True) -> None:
        """The chat's user hands the browser back and confirms it in the desktop, whose window's pane
        posts it. The route answers whether the agent *goes_on* by itself."""
        answer = await self.control(chat, "release", self.window, handed_back=True)
        assert answer == (200, GOES_ON if goes_on else FOR_THE_PANE)

    @asynccontextmanager
    async def no_turn_seen_under_way(self, monkeypatch):
        """The route looks for a turn under way a moment before a worker takes the chat: it sees none."""
        with monkeypatch.context() as patch:
            patch.setattr(self.store, "has_live_lease", AsyncMock(return_value=False))
            yield

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
    return await Asking(api, session_factory).signed_in()


THEIRS = ["its user", "its user, in the desktop's window", "its own session's token"]
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
            # A chat its user holds the browser from: nobody else hands it back either, whatever they say of it.
            await asking.control(held, "acquire", caller), await asking.control(held, "release", caller),
            await asking.control(held, "release", caller, handed_back=True),
        ]

    assert answers == [NO_SUCH_CHAT] * 5
    assert await asking.control(uuid4(), "acquire", (await asking.callers(chat, another))["its user"]) == NO_SUCH_CHAT
    # Neither chat is told anything, and nobody is woken.
    assert {of: await asking.log(of) for of in (chat, held)} == before
    assert asking.wakes == []


async def test_a_chats_own_user_takes_its_browser_over_and_hands_it_back_and_ends_their_other_chats_take_overs(asking):
    chat, held = await asking.chat(), await asking.chat(taken_over=True)

    try:
        taken_over = (200, {"outcome": "granted", "owner_user_id": str(asking.api.user_id)})
        assert await asking.control(chat, "acquire", asking.window) == taken_over
        assert await asking.control(chat, "release", asking.window, handed_back=True) == (200, GOES_ON)
        assert (await asking.log(chat))[-3:] == ["browser.control_granted", "browser.control_returned", "session.resume"]
        # Their other chat on the computer that still said they held the browser is told too, for its pane.
        assert (await asking.log(held))[-2:] == ["browser.control_granted", "browser.control_returned"]
        assert asking.wakes == [str(chat)]
    finally:
        await asking.unqueue(chat)


async def test_a_chats_own_sessions_token_tells_that_chat_and_no_other_of_its_users(asking):
    chat, held, another = await asking.chat(), await asking.chat(taken_over=True), await asking.chat()
    token = (await asking.callers(chat, another))["its own session's token"]
    before = await asking.log(held)

    assert (await asking.control(chat, "acquire", token))[0] == 200
    assert await asking.control(chat, "release", token) == (200, FOR_THE_PANE)
    assert (await asking.log(chat))[-2:] == ["browser.control_granted", "browser.control_returned"]
    # The token is for one session: the user's other chat keeps what it said.
    assert await asking.log(held) == before
    assert asking.wakes == []


# -- A hand back its user confirmed gives the chat's agent a turn: the real wake, replay and loop --

OPENED = "It is open."
WAITING = "You have the browser. Hand it back and I will go on."
SIGN_IN = "Sign in to the bank, then hand the browser back to me."
# What the model reads at the hand back, word for word.
HANDED_BACK = {
    "role": "user",
    "content": (
        "[The user has handed the browser back. The browser tools work again: go on with what you were "
        "doing when they took it over. They may have changed the page meanwhile, so read it again before "
        "you act on it.]"
    ),
}


@pytest.mark.parametrize("waited", ["having met the pause", "having asked its user to sign in"])
async def test_a_hand_back_its_user_confirmed_gives_a_finished_chat_a_turn_in_which_its_agent_reads_it_can_go_on(
    asking, monkeypatch, waited,
):
    if waited == "having met the pause":
        chat, last = await asking.stopped_while_held(), WAITING
    else:
        # No browser call of the agent's met the pause: it asked its user to take the browser, and its turn ended.
        chat, last = await asking.chat(), SIGN_IN
        await asking.turn(chat, "Open my bank.", SIGN_IN, tool="browser_navigate", result='{"title": "Sign in"}')
        await asking.control(chat, "acquire", asking.window)
    assert (await asking.session(chat)).status == "completed"

    await asking.hands_back(chat)

    try:
        # As a typed message leaves a finished chat: active again, and queued.
        assert asking.wakes == [str(chat)]
        assert (await asking.session(chat)).status == "active"
        sent = await woken(asking.api, monkeypatch, await asking.session(chat))
        # The model reads the conversation as it was, then the harness's note, as a message of its own.
        assert sent[-2:] == [{"role": "assistant", "content": last}, HANDED_BACK]
        # The route's resume is the turn: its wake writes no other.
        assert await asking.resumes(chat) == [{"source": "browser_hand_back"}]
        # What a later turn rebuilds is what this one was sent, and the model's answer after it.
        *replay, answer = await replayed(asking.api, await asking.session(chat))
        assert (replay, answer["content"]) == (sent, "Noted.")
    finally:
        await asking.unqueue(chat)


@pytest.mark.parametrize("release", [
    "the pane's own, at the chat's opening", "its user's, from a browser", "its own session's token's",
])
async def test_a_release_that_is_no_confirmed_hand_back_gives_no_turn_and_leaves_nothing_for_a_later_one(
    asking, monkeypatch, release,
):
    # Its agent met the pause while its user held the browser, and waits to hear of the hand back.
    chat, another = await asking.stopped_while_held(), await asking.chat()
    callers = await asking.callers(chat, another)

    if release == "the pane's own, at the chat's opening":
        # The app ended while its user held the browser: started again, nobody holds it there, and the
        # pane posts that. Nobody handed anything back.
        answer = await asking.control(chat, "release", asking.window)
    elif release == "its user's, from a browser":
        answer = await asking.control(chat, "release", callers["its user"], handed_back=True)
    else:
        answer = await asking.control(chat, "release", callers["its own session's token"], handed_back=True)

    assert answer == (200, FOR_THE_PANE)
    # Nobody is woken, and a wake that came all the same would run nothing and resume nothing.
    assert asking.wakes == []
    harness, handed = waking(asking.api, monkeypatch)
    await harness.wake(chat)
    assert handed == []
    assert await asking.resumes(chat) == []
    assert (await asking.session(chat)).status == "completed"
    # Their next message is read by itself: no note waited for it.
    await asking.says(chat, "Go on.")
    sent = await woken(asking.api, monkeypatch, await asking.session(chat))
    assert sent[-2:] == [{"role": "assistant", "content": WAITING}, {"role": "user", "content": "Go on."}]
    assert HANDED_BACK not in sent


# Every command the harness answers itself in a chat on a folder: by its own handler, or, for one
# such a chat refuses, at the gate.
COMMANDS = [
    "/compress", "/clear", "/goal status", "/mission status", "/auto-research status", "/code status",
    "/loop", "/loop 5m check the build",
]
# One its agent has switched off, which the gate answers too.
SWITCHED_OFF = "/goal pause"


def answering(asking, monkeypatch, command: str = ""):
    """A harness whose wake runs for real, each command answered by its own handler; and the
    conversations it handed the model, where a wake came to a turn."""
    harness, handed = waking(asking.api, monkeypatch)
    del harness._handle_loop_command  # the real one, not waking's stand-in
    state = asking.api.app.state
    harness._redis, harness._session_factory = state.redis, state.session_factory
    # The chat's own user, who owns what a command of theirs makes; and a window to report on.
    harness._tenant = harness._acting_principal = SimpleNamespace(
        org_id=asking.api.org_id, user_id=asking.api.user_id, service_account_id=None, asset_root="/tmp/test",
    )
    harness._compressor.context_length = 200_000
    off = {"goal"} if command == SWITCHED_OFF else set()
    harness._slash_commands = SlashCommandConfig(commands=frozenset(SLASH_COMMAND_IDS - off))
    return harness, handed


async def command_answered(asking, monkeypatch, chat: UUID, command: str, *, meanwhile=None) -> str:
    """The chat's user types *command*, and its own wake answers it, as the harness answers each:
    the model is not asked. *meanwhile* happens while that wake is under way. Returns the answer."""
    await asking.says(chat, command)
    harness, handed = answering(asking, monkeypatch, command)
    if meanwhile is not None:
        build = harness._build_system_prompt

        async def and_meanwhile(session):
            await meanwhile()
            return await build(session)

        harness._build_system_prompt = and_meanwhile
    before = await asking.log(chat)
    await harness.wake(chat)
    written = (await asking.log(chat))[len(before):]
    assert handed == [] and "llm.request" not in written
    assert written.count("llm.response") == 1
    [*_, answer] = await asking.store.get_events(chat, types=[EventType.LLM_RESPONSE])
    return answer.data["message"]["content"]


def watching(harness, monkeypatch) -> dict[str, AsyncMock]:
    """Everything a wake runs a command of its user's with, each watched in place of being run: every
    command's handler, the gate's answer, and what expands a skill or consults an expert."""
    handlers = [name for name in dir(type(harness)) if name.startswith("_handle_") and name.endswith("_command")]
    assert len(handlers) >= 7
    watched = {name: AsyncMock() for name in [*handlers, "_emit_loop_response"]}
    for name, watch in watched.items():
        setattr(harness, name, watch)
    watched["expand_slash_skill"] = AsyncMock(return_value=None)
    monkeypatch.setattr(loop_module, "expand_slash_skill", watched["expand_slash_skill"])
    return watched


def ran(watched: dict[str, AsyncMock]) -> list[str]:
    return [name for name, watch in watched.items() if watch.await_count]


async def test_a_wake_for_the_users_command_runs_it(asking, monkeypatch):
    # What the tests below watch for is what runs a command: a wake for the user's own, each time.
    for command, runs in (("/goal status", "_handle_goal_command"), ("/board-pack Q3", "expand_slash_skill")):
        chat = await asking.held()
        await asking.says(chat, command)
        harness, _handed = answering(asking, monkeypatch)
        watched = watching(harness, monkeypatch)
        await harness.wake(chat)
        assert ran(watched) == [runs]


@pytest.mark.parametrize("command", [*COMMANDS, SWITCHED_OFF])
async def test_a_hand_backs_turn_runs_no_command_of_the_users_again(asking, monkeypatch, command):
    chat = await asking.held()
    # The user's last message is a command, answered by the harness: it left the chat active.
    answer = await command_answered(asking, monkeypatch, chat, command)
    assert (await asking.session(chat)).status == "active"

    await asking.hands_back(chat)

    try:
        harness, handed = answering(asking, monkeypatch, command)
        watched = watching(harness, monkeypatch)
        await harness.wake(chat)
        assert ran(watched) == []
        [conversation] = handed
        assert conversation[-2:] == [{"role": "assistant", "content": answer}, HANDED_BACK]
        # A worker that dies in that turn, after its request, leaves it to be run again: still no command.
        await asking.store.emit_event(chat, EventType.LLM_REQUEST, {})
        harness, handed = answering(asking, monkeypatch, command)
        watched = watching(harness, monkeypatch)
        await harness.wake(chat)
        assert ran(watched) == []
        assert [conversation.count(HANDED_BACK) for conversation in handed] == [1]
    finally:
        await asking.unqueue(chat)


@pytest.mark.parametrize("command", [*COMMANDS, SWITCHED_OFF])
async def test_a_hand_back_given_its_turn_as_a_commands_wake_began_runs_no_command_again(asking, monkeypatch, command):
    chat = await asking.held()

    async def user_hands_back():
        async with asking.no_turn_seen_under_way(monkeypatch):
            await asking.hands_back(chat)

    # The command's own wake is under way, and moves the cursor past the hand back where it moves it.
    answer = await command_answered(asking, monkeypatch, chat, command, meanwhile=user_hands_back)

    try:
        harness, handed = answering(asking, monkeypatch, command)
        watched = watching(harness, monkeypatch)
        await harness.wake(chat)
        assert ran(watched) == []
        assert [conversation[-2:] for conversation in handed] == [[{"role": "assistant", "content": answer}, HANDED_BACK]]
    finally:
        await asking.unqueue(chat)


@pytest.mark.parametrize("command", [*COMMANDS, SWITCHED_OFF])
async def test_a_hand_back_made_before_a_commands_wake_came_is_given_its_turn_once_the_command_is_answered(
    asking, monkeypatch, command,
):
    chat = await asking.held()
    # Typed, and the browser handed back before a wake took the command: the queue holds one wake for both.
    await asking.says(chat, command)
    await asking.hands_back(chat)
    await asking.unqueue(chat)

    try:
        # That wake answers the command, and asks the model nothing. The hand back's turn is queued.
        harness, handed = answering(asking, monkeypatch, command)
        await harness.wake(chat)
        assert handed == []
        assert await asking.queued(chat)
        await asking.unqueue(chat)
        # Its wake runs no command again, reads the hand back once, and queues nothing more.
        harness, handed = answering(asking, monkeypatch, command)
        watched = watching(harness, monkeypatch)
        await harness.wake(chat)
        assert ran(watched) == []
        assert [conversation.count(HANDED_BACK) for conversation in handed] == [1]
        assert not await asking.queued(chat)
    finally:
        await asking.unqueue(chat)


async def test_a_hand_back_made_while_a_commands_wake_ran_is_given_its_turn_by_that_wakes_end(asking, monkeypatch):
    chat = await asking.held()

    async def user_hands_back():
        async with asking.no_turn_seen_under_way(monkeypatch):
            await asking.hands_back(chat)
        # The wake the hand back queued came at once, found the chat taken, and went.
        await asking.unqueue(chat)

    await command_answered(asking, monkeypatch, chat, "/goal status", meanwhile=user_hands_back)

    try:
        # The command's wake read what was written while it ran, and queued the hand back's turn.
        assert await asking.queued(chat)
    finally:
        await asking.unqueue(chat)


@pytest.mark.parametrize("command", [*COMMANDS, SWITCHED_OFF])
async def test_a_commands_wake_queues_nothing_more_where_no_hand_back_waits_for_a_turn(asking, monkeypatch, command):
    # Its user still holds the browser; and in another chat a hand back's turn was read before the command.
    held, read = await asking.held(), await asking.stopped_while_held()
    await asking.hands_back(read)
    await asking.store.emit_event(read, EventType.LLM_REQUEST, {})
    await asking.unqueue(read)

    try:
        for chat in (held, read):
            await command_answered(asking, monkeypatch, chat, command)
            assert not await asking.queued(chat)
    finally:
        await asking.unqueue(held, read)


async def test_a_hand_back_a_turn_ended_over_without_reading_is_read_at_the_wake_it_queued(asking, monkeypatch):
    chat = await asking.held()
    await command_answered(asking, monkeypatch, chat, "/goal status")
    # Handed back as a turn is ending: the route makes the chat active, and that turn's end is written
    # over it. The chat is finished again, with the hand back's resume read by no request.
    await asking.hands_back(chat)
    await asking.ends(chat)
    assert (await asking.session(chat)).status == "completed"

    try:
        harness, handed = answering(asking, monkeypatch)
        watched = watching(harness, monkeypatch)
        await harness.wake(chat)
        assert ran(watched) == []
        assert [conversation[-1] for conversation in handed] == [HANDED_BACK]
        # Revived for it, the chat's log says why; that is no second hand back, and is read as none.
        assert await asking.resumes(chat) == [{"source": "browser_hand_back"}, {"source": "unread_browser_hand_back"}]
        assert handed[0].count(HANDED_BACK) == 1
        assert (await asking.session(chat)).status == "active"
    finally:
        await asking.unqueue(chat)


async def test_a_finished_chat_is_not_revived_for_an_unread_hand_back_while_its_users_limit_is_spent_nor_failed_for_it(
    asking, monkeypatch,
):
    # A project's chat, whose turns the wake itself holds against its user's limit.
    metered(asking.api, {}, Ops())
    chat = await asking.stopped_while_held(**IN_A_PROJECT)
    # Handed back as a turn is ending: the chat is finished again, the hand back's resume unread.
    await asking.hands_back(chat)
    await asking.ends(chat)
    await asking.unqueue(chat)

    spent = Ops(allowance_left=False)
    harness, turns = worker(asking.api, monkeypatch, CAPPED, spent)
    await harness.wake(chat)

    # Asked once, at the revival, and refused: the hand back waits for their next message. Nobody typed
    # into the limit, so the chat is not failed for it.
    assert turns == []
    assert spent.held == [("allowance", str(asking.api.user_id), "web")]
    assert (await asking.session(chat)).status == "completed"
    assert await asking.resumes(chat) == [{"source": "browser_hand_back"}]
    assert await asking.store.get_events(chat, types=[EventType.SESSION_FAIL]) == []


async def test_a_hand_back_gives_one_turn_however_often_it_is_posted_and_the_chat_is_woken(asking, monkeypatch):
    chat = await asking.stopped_while_held()
    await asking.hands_back(chat)
    # The pane posts the hand back again, as a second window of the chat does: answered as the first.
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
    # The first worker took the chat, made its request, and died before the model answered.
    harness, handed = waking(asking.api, monkeypatch)
    await harness.wake(chat)
    await asking.store.emit_event(chat, EventType.LLM_REQUEST, {})

    try:
        # The sweeper's wake runs the turn again: the chat is active, and its request is in the log.
        sent = await woken(asking.api, monkeypatch, await asking.session(chat))
        assert sent[-2:] == [{"role": "assistant", "content": WAITING}, HANDED_BACK]
        assert sent.count(HANDED_BACK) == 1
        assert await asking.resumes(chat) == [{"source": "browser_hand_back"}]
    finally:
        await asking.unqueue(chat)


async def test_a_chat_that_failed_before_its_hand_backs_turn_ran_is_left_for_its_user_to_start_again(asking, monkeypatch):
    chat = await asking.stopped_while_held()
    await asking.hands_back(chat)
    # A turn that began as the browser was handed back failed, before the hand back's own wake came.
    await asking.store.update_session_status(chat, "failed")

    try:
        harness, handed = waking(asking.api, monkeypatch)
        await harness.wake(chat)
        # A failed chat is its user's to start again: the hand back revives a finished one alone.
        assert handed == []
        assert (await asking.session(chat)).status == "failed"
        assert await asking.resumes(chat) == [{"source": "browser_hand_back"}]
    finally:
        await asking.unqueue(chat)


@pytest.mark.parametrize("status", ["paused", "failed"])
async def test_a_hand_back_to_a_chat_its_user_stopped_or_that_failed_gives_no_turn_and_is_not_read_later(
    asking, monkeypatch, status,
):
    chat = await asking.stopped_while_held()
    await asking.store.update_session_status(chat, status)

    await asking.hands_back(chat, goes_on=False)

    harness, handed = waking(asking.api, monkeypatch)
    await harness.wake(chat)
    assert (asking.wakes, handed) == ([], [])
    assert (await asking.session(chat)).status == status
    # The chat is theirs to start again, and their message is read by itself: no note waited for it.
    await asking.says(chat, "Go on.")
    sent = await woken(asking.api, monkeypatch, await asking.session(chat))
    assert sent[-1] == {"role": "user", "content": "Go on."}
    assert HANDED_BACK not in sent


async def test_a_hand_back_while_a_turn_is_under_way_is_made_and_tells_neither_that_turn_nor_a_later_one(
    asking, monkeypatch,
):
    chat = await asking.stopped_while_held()
    await asking.says(chat, "Note down where we are.")

    async def user_hands_back():
        # Answered 200, and that the agent does not go on by itself: the pane tells them to write to it.
        await asking.hands_back(chat, goes_on=False)

    requests = await live_turn(
        asking.api, monkeypatch, await asking.session(chat), [TODO_CALL, _final_response("Noted down.")],
        during_tool=user_hands_back,
    )
    assert len(requests) == 2
    assert HANDED_BACK not in requests[1]
    # No turn of its own either, then or once that turn ends; and nothing for their next message to bring.
    assert asking.wakes == []
    await asking.ends(chat)
    harness, handed = waking(asking.api, monkeypatch)
    await harness.wake(chat)
    assert handed == []
    assert await asking.resumes(chat) == []
    assert HANDED_BACK not in await replayed(asking.api, await asking.session(chat))


async def test_a_hand_back_left_unread_is_no_news_once_its_user_has_taken_the_browser_over_again(asking, monkeypatch):
    chat = await asking.stopped_while_held()
    await asking.hands_back(chat)
    # Stopped before the wake came, and the browser taken over again.
    await asking.store.update_session_status(chat, "paused")
    await asking.unqueue(chat)
    assert (await asking.control(chat, "acquire", asking.window))[1]["outcome"] == "granted"

    try:
        # Their next message's turn is not told that the browser tools work again: they do not.
        await asking.says(chat, "Where were we?")
        sent = await woken(asking.api, monkeypatch, await asking.session(chat))
        assert sent[-1] == {"role": "user", "content": "Where were we?"}
        assert HANDED_BACK not in sent
        # Nor does replay say it of that request later.
        await asking.ends(chat)
        assert HANDED_BACK not in await replayed(asking.api, await asking.session(chat))
        # Handed back again, that hand back's turn reads its own, once.
        await asking.hands_back(chat)
        sent = await woken(asking.api, monkeypatch, await asking.session(chat))
        assert sent.count(HANDED_BACK) == 1
    finally:
        await asking.unqueue(chat)


async def test_a_hand_back_whose_user_took_the_browser_over_again_before_its_wake_ran_gives_no_turn(asking, monkeypatch):
    chat = await asking.stopped_while_held()
    await asking.hands_back(chat)
    assert (await asking.control(chat, "acquire", asking.window))[1]["outcome"] == "granted"

    try:
        # The wake the hand back queued finds nothing to do, and nothing revives the chat for it.
        harness, handed = waking(asking.api, monkeypatch)
        await harness.wake(chat)
        assert handed == []
        await asking.store.update_session_status(chat, "completed")
        harness, handed = waking(asking.api, monkeypatch)
        await harness.wake(chat)
        assert handed == []
        assert await asking.resumes(chat) == [{"source": "browser_hand_back"}]
    finally:
        await asking.unqueue(chat)


async def test_a_hand_back_read_in_a_turn_under_way_is_dropped_where_the_browser_is_taken_over_again_before_its_request(
    asking, monkeypatch,
):
    chat = await asking.stopped_while_held()
    await asking.says(chat, "Where are we?")

    async def user_hands_back():
        async with asking.no_turn_seen_under_way(monkeypatch):
            await asking.hands_back(chat)

    unread = loop_module.AgentHarness._has_unread_hand_back

    async def and_takes_over_again(self, session_id) -> bool:
        # Once the hand back is held for the turn's next request, and before that request is made.
        waits = await unread(self, session_id)
        if waits:
            assert (await asking.control(chat, "acquire", asking.window))[1]["outcome"] == "granted"
        return waits

    monkeypatch.setattr(loop_module.AgentHarness, "_has_unread_hand_back", and_takes_over_again)
    try:
        requests = await live_turn(
            asking.api, monkeypatch, await asking.session(chat),
            [_final_response("Waiting for the browser."), _final_response("Still waiting.")],
            during_reply=user_hands_back,
        )
        # The hand back kept the turn going; its request does not say the browser tools work again.
        assert len(requests) == 2
        assert HANDED_BACK not in requests[1]
        # Replay gives the conversation that request sent.
        await asking.ends(chat)
        replay = await replayed(asking.api, await asking.session(chat))
        assert HANDED_BACK not in replay
        [_system, *sent] = requests[1]
        assert replay[: len(sent)] == sent
    finally:
        await asking.unqueue(chat)


async def test_a_hand_back_given_its_turn_as_another_began_is_read_in_that_turn_and_gives_no_second(
    asking, monkeypatch,
):
    chat = await asking.stopped_while_held()
    await asking.says(chat, "Note down where we are.")

    async def user_hands_back():
        async with asking.no_turn_seen_under_way(monkeypatch):
            await asking.hands_back(chat)

    try:
        requests = await live_turn(
            asking.api, monkeypatch, await asking.session(chat), [TODO_CALL, _final_response("Going on.")],
            during_tool=user_hands_back,
        )
        *_, called, result, note = requests[1]
        assert [called["role"], result["role"]] == ["assistant", "tool"]
        assert note == HANDED_BACK
        # Read in that turn, the hand back gives no second one once it ends.
        assert asking.wakes == [str(chat)]
        await asking.ends(chat)
        harness, handed = waking(asking.api, monkeypatch)
        await harness.wake(chat)
        assert handed == []
        assert await asking.resumes(chat) == [{"source": "browser_hand_back"}]
    finally:
        await asking.unqueue(chat)


async def test_a_hand_back_given_its_turn_during_the_agents_reply_is_read_before_that_turn_ends(asking, monkeypatch):
    chat = await asking.stopped_while_held()
    await asking.says(chat, "Where are we?")

    async def user_hands_back():
        async with asking.no_turn_seen_under_way(monkeypatch):
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


async def test_a_hand_backs_turn_reads_the_skill_the_users_last_message_ran_and_runs_it_no_more(asking, monkeypatch):
    chat = await asking.held()
    # The user's last message ran a skill, at its own wake, and that turn ended waiting for the browser.
    emit = asking.store.emit_event
    await asking.says(chat, "/board-pack Q3")
    await emit(chat, EventType.SKILL_INVOKED, {"skill": "board-pack", "raw_message": "/board-pack Q3", "staged_at": None})
    await emit(chat, EventType.LLM_REQUEST, {})
    await emit(chat, EventType.LLM_RESPONSE, {"message": {"role": "assistant", "content": WAITING}})
    await asking.ends(chat)

    await asking.hands_back(chat)

    try:
        harness, handed = waking(asking.api, monkeypatch)
        harness._tools.dispatch = AsyncMock(return_value=json.dumps({"success": True, "content": BOARD_PACK}))
        # What runs a skill at its own wake, and consults an expert: neither again.
        run_again = AsyncMock(return_value=None)
        monkeypatch.setattr(loop_module, "expand_slash_skill", run_again)
        await harness.wake(chat)
        [conversation] = handed
        # As its own wake sent it, so the model still has the skill's instructions, and the prompt cache the conversation.
        expanded = build_expanded_message(name="board-pack", args="Q3", skill_body=BOARD_PACK)
        assert {"role": "user", "content": expanded} in conversation
        assert {"role": "user", "content": "/board-pack Q3"} not in conversation
        assert conversation[-1] == HANDED_BACK
        # Read again, not run again.
        assert run_again.await_count == 0
        assert len(await asking.store.get_events(chat, types=[EventType.SKILL_INVOKED])) == 1
    finally:
        await asking.unqueue(chat)


# -- A hand back's turn is counted as a typed message's is: held where it is given, spent when it ends --

IN_A_PROJECT = {"workspace_boundary": f"{PROJECT_BOUNDARY_PREFIX}{uuid4()}"}
CHATS = pytest.mark.parametrize("config", [{}, IN_A_PROJECT], ids=["a chat on a folder", "a project's chat on a folder"])


@CHATS
async def test_a_hand_backs_turn_is_held_against_the_users_allowance_where_it_is_given_and_spent_when_it_ends(
    asking, monkeypatch, config,
):
    chat = await asking.stopped_while_held(**config)
    ops = Ops()
    metered(asking.api, CAPPED, ops)

    await asking.hands_back(chat)

    try:
        # Held by the route, as the message route holds a typed message's turn.
        assert ops.held == [("allowance", str(asking.api.user_id), "web")]
        harness, turns = worker(asking.api, monkeypatch, CAPPED, ops)
        await harness.wake(chat)
        assert turns == [chat]
        # The wake holds nothing more, and the turn's end spends what the route held.
        assert (ops.held, ops.spent) == ([("allowance", str(asking.api.user_id), "web")], [("allowance", "hold-1", 1500)])
        # Posted again, with nothing left to hand back: no turn, so nothing is held for one.
        await asking.hands_back(chat, goes_on=False)
        assert len(ops.held) == 1
    finally:
        await asking.unqueue(chat)


@CHATS
@pytest.mark.parametrize("left", ["finished", "left active by a command"])
@pytest.mark.parametrize("ops", [lambda: Ops(allowance_left=False), Down], ids=["the limit spent", "ops unreachable"])
async def test_a_hand_back_whose_turn_the_users_limit_refuses_is_made_and_gives_none(
    asking, monkeypatch, config, left, ops,
):
    chat = await asking.stopped_while_held(**config)
    if left == "left active by a command":
        await command_answered(asking, monkeypatch, chat, "/clear")
    status = (await asking.session(chat)).status
    metered(asking.api, CAPPED, ops())

    # The release does not fail: the browser is the agent's again on the computer, and the pane is
    # answered that the agent does not go on by itself.
    await asking.hands_back(chat, goes_on=False)

    # No turn, and the chat is as it was: not resumed, not queued, and not failed for a limit nobody typed into.
    assert asking.wakes == []
    assert (await asking.session(chat)).status == status
    assert await asking.resumes(chat) == []
    assert await asking.store.get_events(chat, types=[EventType.SESSION_FAIL]) == []
    stale = [o.id for o in await asking.store.find_orphaned_sessions(stale_seconds=0, agent_id=AGENT_ID)]
    assert chat not in stale
    # And nothing waits for the user's next message, once their limit allows one.
    assert HANDED_BACK not in await replayed(asking.api, await asking.session(chat))
