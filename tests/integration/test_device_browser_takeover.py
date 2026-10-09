"""A local-folder chat's browser taken over on its user's computer, and handed back, as the dispatcher sees it.

Against the real database and queue: the control route's own events, the orphan sweeper's query and
one of its passes, and what a wake would find past the chat's cursor.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from surogates.api.app import _install_browser_api_dependencies
from surogates.api.routes import browser as browser_routes
from surogates.browser.registry import BrowserEntry
from surogates.config import SHARED_WORK_QUEUE_KEY, encode_queue_member
from surogates.devices.browser import tell_pane
from surogates.harness.loop_messages import maybe_inject_browser_pause
from surogates.harness.loop_pending import _actionable_pending_events
from surogates.orchestrator.dispatcher import Orchestrator
from surogates.session.events import EventType
from surogates.tenant.auth.jwt import create_access_token, create_service_account_session_token
from surogates.tenant.auth.middleware import get_current_tenant
from surogates.tenant.context import TenantContext

from .conftest import create_org, create_user, issue_service_account_token
from .test_devices import AGENT_ID, add_user, api  # noqa: F401  (api is a fixture)

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
        app = self.api(session_scope_id=token_of)
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


async def test_a_hand_back_made_from_another_chat_is_told_to_the_chat_first_taken_over_and_its_agent_is_left_alone(computer):
    first, other = await computer.idle(), await computer.idle()
    await computer.control(first, "acquire")
    # The first chat gone from the computer, its user hands the browser back from another: that chat's
    # pane tells its own route of the take-over, then of the hand back.
    await computer.control(other, "acquire")
    await computer.control(other, "release")
    await computer.left(first)

    try:
        # The first chat no longer says its user holds the browser: it is told the hand back, and where it was made.
        told = (await computer.store.get_events(first))[-1]
        assert (told.type, told.data) == ("browser.control_returned", {
            "session_id": str(first), "released_by": str(computer.user_id), "computer": True,
            "handed_back_from": str(other),
        })
        assert await computer.log(first) == [
            "user.message", "llm.response", "browser.control_granted", "browser.control_returned",
        ]
        # For its pane: its agent is not woken, has no work, and the chat does not look crashed. The agent
        # goes on in the chat the browser was handed back from.
        assert computer.wakes == [str(other)]
        assert await computer.work(first) == []
        assert await computer.work(other) == ["browser.control_returned"]
        assert await computer.orphans() == {other}
        await computer.sweep()
        assert "harness.recovered" not in await computer.log(first)
        assert not await computer.queued(first)
    finally:
        await computer.unqueue(first, other)


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
        # Taken over anew, it is told anew; handed back from itself, its own agent is woken.
        assert (await computer.control(first, "acquire"))["outcome"] == "granted"
        await computer.control(first, "release")
        assert (await computer.log(first))[told:] == ["browser.control_granted", "browser.control_returned"]
        assert computer.wakes == [str(other), str(first)]
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

    try:
        for chat in standing:
            assert (await computer.log(chat))[-2:] == ["browser.control_granted", "browser.control_returned"]
        assert {chat: await computer.log(chat) for chat in untold} == before
        assert computer.wakes == [str(handed_back), str(here)]
    finally:
        await computer.unqueue(here, handed_back)


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


async def test_only_a_hand_back_is_passed_over_for_naming_the_chat_it_was_made_from(computer):
    chat = await computer.idle()

    # A message nobody answered is found, and is a wake's work, whatever it carries.
    await computer.store.emit_event(chat, EventType.USER_MESSAGE, {"content": "Go on.", "handed_back_from": str(uuid4())})
    await computer.left(chat)

    assert await computer.orphans() == {chat}
    assert await computer.work(chat) == ["user.message"]


# -- Who may take a chat's browser over, hand it back, or read its state: the app's own sign-in check --

NO_SUCH_CHAT = (404, {"detail": "No browser for session"})


class Asking:
    """The real app, each caller with a real token: a user's chats on a folder of their computer,
    their browser routes told through the API's own emitter, and each wake recorded."""

    def __init__(self, api, session_factory) -> None:
        self.api, self.factory = api, session_factory
        self.store = api.app.state.session_store
        self.device_id = uuid4()
        self.wakes: list[str] = []
        _install_browser_api_dependencies(api.app, SimpleNamespace(browser=SimpleNamespace(backend=None)))
        enqueue = api.app.state.session_wake

        async def wake(session_id: str) -> None:
            self.wakes.append(session_id)
            await enqueue(session_id)

        api.app.state.session_wake = wake

    async def chat(self, *, taken_over: bool = False) -> UUID:
        """A chat of the app's user on a folder of their computer, its agent's tab open there."""
        session = await self.store.create_session(
            user_id=self.api.user_id, org_id=self.api.org_id, agent_id=AGENT_ID,
            config={"execution": {"kind": "device", "device_id": str(self.device_id)}},
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
