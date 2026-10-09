"""REST endpoints for browser state and control."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
import httpx
from httpx import ASGITransport, AsyncClient

from surogates.browser.base import BrowserEndpoint
from surogates.browser.control import AcquireOutcome, ControlEntry
from surogates.browser.resolver import ResolvedBrowser
from surogates.tenant.context import TenantContext


ORG_1 = UUID("00000000-0000-0000-0000-000000000001")
ORG_2 = UUID("00000000-0000-0000-0000-000000000002")
USER_1 = UUID("10000000-0000-0000-0000-000000000001")


class StubResolver:
    def __init__(self) -> None:
        self.entries: dict[str, ResolvedBrowser] = {}
        self.forgotten: list[str] = []

    async def forget_unreachable(self, session_id: str) -> None:
        self.forgotten.append(session_id)
        self.entries.pop(session_id, None)

    async def resolve(
        self,
        session_id: str,
        *,
        expected_org_id: str | None,
    ) -> ResolvedBrowser | None:
        entry = self.entries.get(session_id)
        if entry is None:
            return None
        if expected_org_id is not None and entry.org_id != expected_org_id:
            return None
        return entry


class StubControl:
    def __init__(self) -> None:
        self.flag: dict[str, str] = {}

    async def held_by(self, session_id: str) -> str | None:
        return self.flag.get(session_id)

    async def acquire(
        self,
        session_id: str,
        user_id: str,
    ) -> tuple[AcquireOutcome, ControlEntry]:
        existing = self.flag.get(session_id)
        if existing is not None:
            outcome = (
                AcquireOutcome.REFRESHED
                if existing == user_id
                else AcquireOutcome.CONFLICT
            )
            return outcome, ControlEntry(
                owner_user_id=existing,
                acquired_at=datetime.now(timezone.utc),
            )
        self.flag[session_id] = user_id
        return AcquireOutcome.GRANTED, ControlEntry(
            owner_user_id=user_id,
            acquired_at=datetime.now(timezone.utc),
        )

    async def release(self, session_id: str, user_id: str) -> bool:
        if self.flag.get(session_id) != user_id:
            return False
        self.flag.pop(session_id, None)
        return True


class StubSessions:
    """The session store in a test: each session as its row has it, and its browser events in order.

    An event is its type alone, for the session's own, or its type and the sub-agent whose it is.
    """

    def __init__(self) -> None:
        self.sessions: dict[UUID, Any] = {}
        self.events: dict[UUID, list[str | tuple[str, UUID]]] = {}
        # The chats with a turn under way: a worker holds their lease.
        self.busy: set[UUID] = set()
        # What the chats were told, as (session, type, data): the list a test's emitter records in.
        self.told: list[tuple[str, str, dict]] = []

    async def tell_browser_control(self, session_id: UUID, event_type: Any, data: dict) -> int | None:
        """As the store tells it: a take-over only while none stands, a hand back only while one does."""
        log = self.events.setdefault(session_id, [])
        said = [kind for kind in (entry if isinstance(entry, str) else entry[0] for entry in log)
                if kind in ("browser.control_granted", "browser.control_returned")]
        if (said[-1:] == ["browser.control_granted"]) is not (event_type.value == "browser.control_returned"):
            return None
        log.append(event_type.value)
        self.told.append((str(session_id), event_type.value, data))
        return len(log)

    async def has_live_lease(self, session_id: UUID) -> bool:
        return session_id in self.busy

    async def resume_session(self, session_id: UUID, *, source: str = "") -> None:
        """As the store resumes one: active again, and its log says why."""
        self.sessions[session_id].status = "active"
        self.events.setdefault(session_id, []).append("session.resume")
        self.told.append((str(session_id), "session.resume", {"source": source}))

    async def get_session(self, session_id: UUID) -> Any:
        if session_id not in self.sessions:
            raise LookupError(session_id)
        session = self.sessions[session_id]
        # A chat's row names itself and its user: the one the tests' tokens are for, where a test names none.
        session.id = session_id
        session.user_id = getattr(session, "user_id", USER_1)
        session.status = getattr(session, "status", "completed")
        return session

    async def chats_told_taken_over(
        self, *, device_id: UUID, org_id: UUID, agent_id: str, user_id: UUID | None,
    ) -> list[UUID]:
        """As the store answers it: that user's chats with the agent on the computer whose last word of
        their browser's control is a take-over."""
        told = []
        for sid, session in self.sessions.items():
            said = [kind for kind in (entry if isinstance(entry, str) else entry[0] for entry in self.events.get(sid, []))
                    if kind in ("browser.control_granted", "browser.control_returned")]
            theirs = (session.org_id, session.agent_id, getattr(session, "user_id", USER_1)) == (org_id, agent_id, user_id)
            if theirs and session.config["execution"]["device_id"] == str(device_id) and said[-1:] == ["browser.control_granted"]:
                told.append(sid)
        return told

    async def get_events(self, session_id: UUID, *, types: list[Any] | None = None, **_: Any) -> list[Any]:
        wanted = {kind.value for kind in types or []}
        events = [entry if isinstance(entry, tuple) else (entry, session_id) for entry in self.events.get(session_id, [])]
        # An event's id is its place in the log, counted from one.
        return [
            SimpleNamespace(id=at, type=kind, data={"session_id": str(of), "computer": True})
            for at, (kind, of) in enumerate(events, start=1) if kind in wanted
        ]

    def emitter(self, events: list[tuple[str, str, dict]]):
        """The app's emitter in a test: what it emits is recorded, and is in its session's log for the routes to read back.
        What the store itself tells a chat of its browser's control is recorded in the same list."""
        self.told = events
        record = _event_recorder(events)

        async def emit(session_id: str, event_type: Any, data: dict) -> None:
            await record(session_id, event_type, data)
            self.events.setdefault(UUID(session_id), []).append(events[-1][1])

        return emit


@pytest.fixture()
def app_factory():
    from surogates.api.routes import browser as browser_routes
    from surogates.tenant.auth.middleware import get_current_tenant

    resolver = StubResolver()
    control = StubControl()

    def build(
        *, org_id: UUID = ORG_1, user_id: UUID | None = USER_1, session_scope_id: UUID | None = None,
        sign_in: UUID | None = None,
    ) -> FastAPI:
        app = FastAPI()
        app.include_router(browser_routes.router, prefix="/v1")
        app.state.browser_resolver = resolver
        app.state.browser_control = control

        async def fake_tenant() -> TenantContext:
            return TenantContext(
                org_id=org_id,
                user_id=user_id,
                org_config={},
                user_preferences={},
                permissions=frozenset(),
                asset_root="/tmp/surogates-test",
                session_scope_id=session_scope_id,
                oauth_family_id=sign_in,
            )

        app.dependency_overrides[get_current_tenant] = fake_tenant
        return app

    return build, resolver, control


# A user's computer, and the sign-in of Surogate Desktop on it, whose window's web client posts with it.
COMPUTER = UUID("30000000-0000-0000-0000-000000000001")
DESKTOP = UUID("20000000-0000-0000-0000-000000000001")
# The same user's sign-in of the desktop on another computer of theirs.
ANOTHER_DESKTOP = UUID("20000000-0000-0000-0000-000000000002")


@pytest.fixture()
def desk(app_factory, monkeypatch):
    """The control route of chats on a folder of a user's computer: each post made as the web client
    in that computer's desktop window makes it, unless a test says who else posts."""
    from surogates.api.routes import browser as browser_routes

    build, _resolver, _control = app_factory
    store = StubSessions()
    events: list[tuple[str, str, dict]] = []
    wakes: list[str] = []
    computers = {DESKTOP: COMPUTER, ANOTHER_DESKTOP: uuid4()}

    class SignIns:
        """The sign-ins' store in a test: the computer each is bound to."""

        def __init__(self, _session_factory: Any) -> None:
            pass

        async def computer(self, family_id: UUID) -> UUID | None:
            return computers.get(family_id)

    monkeypatch.setattr(browser_routes, "OAuthTokens", SignIns)

    def chat(**row: Any) -> UUID:
        sid = uuid4()
        config = {"execution": {"kind": "device", "device_id": str(COMPUTER)}, **row.pop("config", {})}
        store.sessions[sid] = SimpleNamespace(org_id=ORG_1, agent_id="agent", config=config, **row)
        return sid

    async def control(sid: UUID, action: str, *, handed_back: bool | None = None, **who: Any) -> tuple[int, dict]:
        app = build(**{"sign_in": DESKTOP, **who})
        app.state.session_store = store
        app.state.session_factory = object()
        app.state.session_event_emitter = store.emitter(events)
        app.state.session_wake = _wake_recorder(wakes)
        said = {"action": action, **({} if handed_back is None else {"handed_back": handed_back})}
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(f"/v1/sessions/{sid}/browser/control", json=said)
        return response.status_code, response.json()

    return SimpleNamespace(store=store, events=events, wakes=wakes, chat=chat, control=control)


def _taken_over(sid: UUID) -> tuple[str, str, dict]:
    return str(sid), "browser.control_granted", {"session_id": str(sid), "owner_user_id": str(USER_1), "computer": True}


def _handed_back(sid: UUID, **said: Any) -> tuple[str, str, dict]:
    return str(sid), "browser.control_returned", {"session_id": str(sid), "released_by": str(USER_1), "computer": True, **said}


def _resumed(sid: UUID) -> tuple[str, str, dict]:
    return str(sid), "session.resume", {"source": "browser_hand_back"}


RELEASED = {"outcome": "released"}


def _resolved(session_id: str, *, org_id: UUID = ORG_1) -> ResolvedBrowser:
    return ResolvedBrowser(
        session_id=session_id,
        endpoint=BrowserEndpoint(
            rest_url="http://browser-x.svc:10001",
            cdp_url="ws://browser-x.svc:9222",
            live_view_url="ws://browser-x.svc:443",
        ),
        org_id=str(org_id),
        user_id=str(USER_1),
        source="registry",
    )


class TestStateEndpoint:
    async def test_returns_404_when_no_browser(self, app_factory) -> None:
        build, _resolver, _control = app_factory
        sid = str(uuid4())

        async with AsyncClient(
            transport=ASGITransport(app=build()),
            base_url="http://test",
        ) as client:
            response = await client.get(f"/v1/sessions/{sid}/browser/state")

        assert response.status_code == 404

    async def test_returns_state_when_browser_live(self, app_factory) -> None:
        build, resolver, _control = app_factory
        sid = str(uuid4())
        resolver.entries[sid] = _resolved(sid)

        async with AsyncClient(
            transport=ASGITransport(app=build()),
            base_url="http://test",
        ) as client:
            response = await client.get(f"/v1/sessions/{sid}/browser/state")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "live"
        assert body["control_owner"] is None
        assert body["live_view_path"] == f"/v1/sessions/{sid}/browser/live/"

    async def test_state_reports_user_control(self, app_factory) -> None:
        build, resolver, control = app_factory
        sid = str(uuid4())
        resolver.entries[sid] = _resolved(sid)
        control.flag[sid] = str(USER_1)

        async with AsyncClient(
            transport=ASGITransport(app=build()),
            base_url="http://test",
        ) as client:
            response = await client.get(f"/v1/sessions/{sid}/browser/state")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "user-control"
        assert body["control_owner"] == str(USER_1)

    async def test_other_org_gets_404(self, app_factory) -> None:
        build, resolver, _control = app_factory
        sid = str(uuid4())
        resolver.entries[sid] = _resolved(sid, org_id=ORG_1)

        async with AsyncClient(
            transport=ASGITransport(app=build(org_id=ORG_2)),
            base_url="http://test",
        ) as client:
            response = await client.get(f"/v1/sessions/{sid}/browser/state")

        assert response.status_code == 404

    async def test_a_local_folder_chats_browser_is_as_its_last_browser_event_says(self, app_factory) -> None:
        build, _resolver, _control = app_factory
        sid = uuid4()
        store = StubSessions()
        store.sessions[sid] = SimpleNamespace(
            org_id=ORG_1, agent_id="agent", config={"execution": {"kind": "device", "device_id": str(uuid4())}},
        )
        app = build()
        app.state.session_store = store
        on_computer = {"control_owner": None, "live_view_path": "", "computer": True}

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            async def state() -> httpx.Response:
                return await client.get(f"/v1/sessions/{sid}/browser/state")

            # Its agent has not opened a page on the computer yet.
            assert (await state()).status_code == 404
            store.events[sid] = ["browser.provisioned"]
            assert (await state()).json() == {"status": "live", **on_computer}
            store.events[sid].append("browser.unavailable")
            assert (await state()).json() == {"status": "unavailable", **on_computer}
            store.events[sid] += ["browser.provisioned", "browser.destroyed"]
            assert (await state()).status_code == 404

        # Another organisation's chat is not known here.
        store.events[sid] = ["browser.provisioned"]
        other = build(org_id=ORG_2)
        other.state.session_store = store
        async with AsyncClient(transport=ASGITransport(app=other), base_url="http://test") as client:
            assert (await client.get(f"/v1/sessions/{sid}/browser/state")).status_code == 404

    async def test_a_local_folder_chats_browser_is_open_while_a_tab_of_its_own_or_of_a_sub_agents_is(self, app_factory) -> None:
        build, _resolver, _control = app_factory
        sid, child, another = uuid4(), uuid4(), uuid4()
        store = StubSessions()
        store.sessions[sid] = SimpleNamespace(
            org_id=ORG_1, agent_id="agent", config={"execution": {"kind": "device", "device_id": str(uuid4())}},
        )
        app = build()
        app.state.session_store = store

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            async def status() -> str | int:
                response = await client.get(f"/v1/sessions/{sid}/browser/state")
                return response.json()["status"] if response.status_code == 200 else response.status_code

            # A sub-agent's tab alone, as the worker writes it to its root's log: the chat's browser is open there.
            store.events[sid] = [("browser.provisioned", child)]
            assert await status() == "live"
            # The chat's own tab too, then the sub-agent's closed: the chat's own is open still.
            store.events[sid] += ["browser.provisioned", ("browser.destroyed", child)]
            assert await status() == "live"
            # Another sub-agent's, then the chat's own closed: that sub-agent's is open still.
            store.events[sid] += [("browser.provisioned", another), "browser.destroyed"]
            assert await status() == "live"
            # The last of them closed: no browser for the chat.
            store.events[sid].append(("browser.destroyed", another))
            assert await status() == 404

    async def test_a_call_that_found_no_browser_takes_away_its_own_sessions_tab_and_no_other(self, app_factory) -> None:
        build, _resolver, _control = app_factory
        sid, child, another = uuid4(), uuid4(), uuid4()
        store = StubSessions()
        store.sessions[sid] = SimpleNamespace(
            org_id=ORG_1, agent_id="agent", config={"execution": {"kind": "device", "device_id": str(uuid4())}},
        )
        app = build()
        app.state.session_store = store

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            async def status() -> str | int:
                response = await client.get(f"/v1/sessions/{sid}/browser/state")
                return response.json()["status"] if response.status_code == 200 else response.status_code

            # A sub-agent's call found no supported browser while the chat's own tab is open: the chat's is as it was.
            store.events[sid] = ["browser.provisioned", ("browser.unavailable", child)]
            assert await status() == "live"
            # The sub-agent's own tab goes with its call; another sub-agent's stays.
            store.events[sid] = [("browser.provisioned", child), ("browser.provisioned", another), ("browser.unavailable", child)]
            assert await status() == "live"
            # That one's call finds none too: no tab is left, and the chat's last word of its browser is that there is none.
            store.events[sid].append(("browser.unavailable", another))
            assert await status() == "unavailable"
            # The chat's own call takes its own tab the same way, and leaves a sub-agent's.
            store.events[sid] = [("browser.provisioned", child), "browser.provisioned", "browser.unavailable"]
            assert await status() == "live"

    async def test_a_token_that_does_not_cover_a_root_chat_reads_nothing_of_its_sub_agents_browser(self, app_factory) -> None:
        build, _resolver, _control = app_factory
        sid, child = uuid4(), uuid4()
        on_computer = {"execution": {"kind": "device", "device_id": str(uuid4())}}
        store = StubSessions()
        store.sessions[sid] = SimpleNamespace(org_id=ORG_1, agent_id="agent", config=on_computer)
        store.sessions[child] = SimpleNamespace(
            org_id=ORG_1, agent_id="agent", config={**on_computer, "sandbox_root_session_id": str(sid)},
        )
        # The sub-agent's tab, in its own log and in its root's.
        store.events[child] = ["browser.provisioned"]
        store.events[sid] = [("browser.provisioned", child)]

        async def statuses(scope: UUID, of: UUID) -> list[int]:
            app = build(user_id=None, session_scope_id=scope)
            app.state.session_store = store
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                return [
                    (await client.get(f"{prefix}/sessions/{of}/browser/state")).status_code
                    for prefix in ("/v1", "/v1/api")
                ]

        # Another session's token reads the root's state no more for the sub-agent's tab in it.
        assert await statuses(uuid4(), sid) == [404, 404]
        # The sub-agent's own token reads its own, never its root's.
        assert await statuses(child, child) == [200, 200]
        assert await statuses(child, sid) == [404, 404]
        # The root's own token reads the chat's browser, its sub-agent's tab in it.
        assert await statuses(sid, sid) == [200, 200]

    async def test_a_token_for_another_session_does_not_read_a_local_folder_chats_browser(self, app_factory) -> None:
        build, _resolver, _control = app_factory
        sid = uuid4()
        store = StubSessions()
        store.sessions[sid] = SimpleNamespace(
            org_id=ORG_1, agent_id="agent", config={"execution": {"kind": "device", "device_id": str(uuid4())}},
        )
        store.events[sid] = ["browser.provisioned"]

        async def statuses(scope: UUID) -> list[int]:
            # As a worker's token for one session has it: no user, and that session alone.
            app = build(user_id=None, session_scope_id=scope)
            app.state.session_store = store
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                return [
                    (await client.get(f"{prefix}/sessions/{sid}/browser/state")).status_code
                    for prefix in ("/v1", "/v1/api")
                ]

        # Of the same organisation, and still not this chat's: as every route of a session answers it.
        assert await statuses(uuid4()) == [404, 404]
        # The chat's own token reads it.
        assert await statuses(sid) == [200, 200]

    async def test_a_chat_in_the_cloud_is_answered_from_its_browser_as_before(self, app_factory) -> None:
        build, resolver, _control = app_factory
        sid = uuid4()
        store = StubSessions()
        store.sessions[sid] = SimpleNamespace(org_id=ORG_1, agent_id="agent", config={})
        store.events[sid] = ["browser.provisioned"]
        resolver.entries[str(sid)] = _resolved(str(sid))
        app = build()
        app.state.session_store = store

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            body = (await client.get(f"/v1/sessions/{sid}/browser/state")).json()

        assert body == {"status": "live", "control_owner": None, "live_view_path": f"/v1/sessions/{sid}/browser/live/", "computer": False}


class TestControlEndpoint:
    async def test_acquire_when_unheld_emits_event(
        self,
        app_factory,
    ) -> None:
        build, resolver, _control = app_factory
        sid = str(uuid4())
        resolver.entries[sid] = _resolved(sid)
        events: list[tuple[str, str, dict]] = []
        app = build()
        app.state.session_event_emitter = _event_recorder(events)
        app.state.session_wake = _wake_noop

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.post(
                f"/v1/sessions/{sid}/browser/control",
                json={"action": "acquire"},
            )

        assert response.status_code == 200
        assert response.json() == {
            "outcome": "granted",
            "owner_user_id": str(USER_1),
        }
        assert events == [
            (
                sid,
                "browser.control_granted",
                {"session_id": sid, "owner_user_id": str(USER_1)},
            )
        ]

    async def test_acquire_same_user_refreshes_without_event(
        self,
        app_factory,
    ) -> None:
        build, resolver, control = app_factory
        sid = str(uuid4())
        resolver.entries[sid] = _resolved(sid)
        control.flag[sid] = str(USER_1)
        events: list[tuple[str, str, dict]] = []
        app = build()
        app.state.session_event_emitter = _event_recorder(events)
        app.state.session_wake = _wake_noop

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.post(
                f"/v1/sessions/{sid}/browser/control",
                json={"action": "acquire"},
            )

        assert response.status_code == 200
        assert response.json()["outcome"] == "refreshed"
        assert events == []

    async def test_acquire_different_user_returns_409(
        self,
        app_factory,
    ) -> None:
        build, resolver, control = app_factory
        sid = str(uuid4())
        resolver.entries[sid] = _resolved(sid)
        holder = "20000000-0000-0000-0000-000000000001"
        control.flag[sid] = holder
        app = build()
        app.state.session_event_emitter = _event_recorder([])
        app.state.session_wake = _wake_noop

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.post(
                f"/v1/sessions/{sid}/browser/control",
                json={"action": "acquire"},
            )

        assert response.status_code == 409
        assert response.json()["detail"]["holder_user_id"] == holder

    async def test_release_owner_succeeds_emits_event_and_wakes(
        self,
        app_factory,
    ) -> None:
        build, resolver, control = app_factory
        sid = str(uuid4())
        resolver.entries[sid] = _resolved(sid)
        control.flag[sid] = str(USER_1)
        events: list[tuple[str, str, dict]] = []
        wakes: list[str] = []
        app = build()
        app.state.session_event_emitter = _event_recorder(events)
        app.state.session_wake = _wake_recorder(wakes)

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.post(
                f"/v1/sessions/{sid}/browser/control",
                json={"action": "release"},
            )

        assert response.status_code == 200
        assert response.json() == {"outcome": "released"}
        assert events == [
            (
                sid,
                "browser.control_returned",
                {"session_id": sid, "released_by": str(USER_1)},
            )
        ]
        assert wakes == [sid]

    async def test_release_non_owner_returns_403(
        self,
        app_factory,
    ) -> None:
        build, resolver, control = app_factory
        sid = str(uuid4())
        resolver.entries[sid] = _resolved(sid)
        control.flag[sid] = "20000000-0000-0000-0000-000000000001"
        events: list[tuple[str, str, dict]] = []
        wakes: list[str] = []
        app = build()
        app.state.session_event_emitter = _event_recorder(events)
        app.state.session_wake = _wake_recorder(wakes)

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.post(
                f"/v1/sessions/{sid}/browser/control",
                json={"action": "release"},
            )

        assert response.status_code == 403
        assert events == []
        assert wakes == []

    async def test_invalid_action_returns_400(self, app_factory) -> None:
        build, resolver, _control = app_factory
        sid = str(uuid4())
        resolver.entries[sid] = _resolved(sid)

        async with AsyncClient(
            transport=ASGITransport(app=build()),
            base_url="http://test",
        ) as client:
            response = await client.post(
                f"/v1/sessions/{sid}/browser/control",
                json={"action": "steal"},
            )

        assert response.status_code == 400

    async def test_a_local_folder_chats_take_over_is_told_to_it_and_a_hand_back_its_user_confirmed_gives_its_agent_a_turn(
        self, desk,
    ) -> None:
        sid = desk.chat()

        # The pause is the user's computer's: no browser here and no lease, so each is told as it comes.
        assert await desk.control(sid, "acquire") == (200, {"outcome": "granted", "owner_user_id": str(USER_1)})
        assert desk.wakes == []
        assert await desk.control(sid, "release", handed_back=True) == (200, {**RELEASED, "resumes": True})

        # Handed back: the chat is told, made active again as by a message, and queued. Its resume is the
        # turn, and what its agent reads the hand back from.
        assert desk.events == [_taken_over(sid), _handed_back(sid, resumes=True), _resumed(sid)]
        assert desk.wakes == [str(sid)]
        assert desk.store.sessions[sid].status == "active"

        # Another organisation's chat is not known here.
        assert (await desk.control(sid, "release", org_id=ORG_2))[0] == 404
        assert len(desk.events) == 3

    async def test_a_release_that_is_no_confirmed_hand_back_is_told_for_the_chats_pane_alone(self, desk) -> None:
        # As the pane posts one at a chat's opening, where the app ended while its user held the browser,
        # and for a chat the browser is not held from: nobody handed anything back.
        for said in ({}, {"handed_back": False}):
            sid = desk.chat()
            await desk.control(sid, "acquire")

            assert await desk.control(sid, "release", **said) == (200, {**RELEASED, "resumes": False})

            assert desk.events[-2:] == [_taken_over(sid), _handed_back(sid)]
            assert desk.store.sessions[sid].status == "completed"
        assert desk.wakes == []

    @pytest.mark.parametrize("who", [
        {"sign_in": None}, {"sign_in": ANOTHER_DESKTOP}, {"sign_in": UUID(int=7)},
    ], ids=["the-web-client-in-a-browser", "the-desktop-on-another-computer", "a-sign-in-that-ended"])
    async def test_a_hand_back_is_taken_as_confirmed_only_from_the_desktops_window_on_the_chats_computer(
        self, desk, who,
    ) -> None:
        sid = desk.chat()
        await desk.control(sid, "acquire")

        # The confirmation is drawn on the chat's computer: from anywhere else nothing was confirmed there.
        assert await desk.control(sid, "release", handed_back=True, **who) == (200, {**RELEASED, "resumes": False})

        assert desk.events == [_taken_over(sid), _handed_back(sid)]
        assert desk.wakes == []

    @pytest.mark.parametrize("chat", [
        {"busy": True}, {"status": "paused"}, {"status": "failed"}, {"status": "archived"},
    ], ids=["a-turn-under-way", "stopped-by-its-user", "failed", "deleted"])
    async def test_a_hand_back_to_a_chat_that_cannot_take_a_turn_is_made_and_gives_none(self, desk, chat) -> None:
        busy = chat.pop("busy", False)
        sid = desk.chat(**chat)
        if busy:
            desk.store.busy.add(sid)
        await desk.control(sid, "acquire")

        # The browser is the agent's again on the computer whatever the server answers: it answers 200,
        # says the agent does not go on by itself, and keeps nothing for a later turn to read.
        assert await desk.control(sid, "release", handed_back=True) == (200, {**RELEASED, "resumes": False})

        assert desk.events == [_taken_over(sid), _handed_back(sid)]
        assert desk.wakes == []
        assert desk.store.sessions[sid].status == chat.get("status", "completed")

    async def test_a_take_over_and_a_hand_back_made_from_a_sub_agents_view_are_its_chats(self, desk) -> None:
        root = desk.chat()
        child = desk.chat(config={"sandbox_root_session_id": str(root)})

        assert (await desk.control(child, "acquire"))[1]["outcome"] == "granted"
        assert await desk.control(child, "release", handed_back=True) == (200, {**RELEASED, "resumes": True})

        # Told to the chat, and its agent given the turn: the sub-agent's own log says nothing of either.
        assert desk.events == [_taken_over(root), _handed_back(root, resumes=True), _resumed(root)]
        assert desk.wakes == [str(root)]
        assert desk.store.events.get(child, []) == []

    @pytest.mark.parametrize("under", ["a chat that is not there", "another user's chat", "no chat's id"])
    async def test_a_session_under_a_chat_its_caller_may_not_ask_of_has_no_browser_to_take_over(self, desk, under) -> None:
        theirs = desk.chat(user_id=uuid4())
        named = {"a chat that is not there": str(uuid4()), "another user's chat": str(theirs), "no chat's id": "the-chat"}
        child = desk.chat(config={"sandbox_root_session_id": named[under]})

        for action in ("acquire", "release"):
            assert await desk.control(child, action, handed_back=action == "release") == (
                404, {"detail": "No browser for session"},
            )

        assert (desk.events, desk.wakes) == ([], [])

    async def test_a_token_for_another_session_tells_a_local_folder_chat_nothing(self, app_factory) -> None:
        build, _resolver, _control = app_factory
        sid = uuid4()
        store = StubSessions()
        store.sessions[sid] = SimpleNamespace(
            org_id=ORG_1, agent_id="agent", config={"execution": {"kind": "device", "device_id": str(uuid4())}},
        )
        events: list[tuple[str, str, dict]] = []
        wakes: list[str] = []
        # As a worker's token for another session of the same organisation has it: no user, and that
        # session alone. On the service path it names the user it speaks for.
        app = build(user_id=None, session_scope_id=uuid4())
        app.state.session_store = store
        app.state.session_event_emitter = _event_recorder(events)
        app.state.session_wake = _wake_recorder(wakes)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            answered = {
                (action, prefix): (await client.post(
                    f"{prefix}/sessions/{sid}/browser/control",
                    json={"action": action, "owner_user_id": str(USER_1)},
                )).status_code
                for action in ("acquire", "release")
                for prefix in ("/v1/api", "/v1")
            }

        # As every route of a session answers it.
        assert set(answered.values()) == {404}, answered
        assert events == []
        assert wakes == []

    async def test_a_local_folder_chats_hand_back_is_told_once_and_only_after_a_take_over(self, desk) -> None:
        sid = desk.chat()

        # Never taken over: nothing to hand back, so the chat is told nothing and its agent is not woken.
        assert await desk.control(sid, "release", handed_back=True) == (200, {**RELEASED, "resumes": False})
        assert (desk.events, desk.wakes) == ([], [])

        await desk.control(sid, "acquire")
        await desk.control(sid, "release", handed_back=True)
        told = [_taken_over(sid), _handed_back(sid, resumes=True), _resumed(sid)]
        assert (desk.events, desk.wakes) == (told, [str(sid)])

        # Handed back already: a repeat is answered as done, gives no second turn, and tells and wakes no more.
        assert await desk.control(sid, "release", handed_back=True) == (200, {**RELEASED, "resumes": False})
        assert (desk.events, desk.wakes) == (told, [str(sid)])

    async def test_a_local_folder_chats_take_over_is_told_once_while_it_stands(self, desk) -> None:
        sid = desk.chat()

        assert await desk.control(sid, "acquire") == (200, {"outcome": "granted", "owner_user_id": str(USER_1)})
        # Told already: the cloud's own answer to an acquire that changes nothing.
        assert await desk.control(sid, "acquire") == (200, {"outcome": "refreshed", "owner_user_id": str(USER_1)})
        assert desk.events == [_taken_over(sid)]

        # Handed back, a new take-over is told anew.
        await desk.control(sid, "release", handed_back=True)
        assert await desk.control(sid, "acquire") == (200, {"outcome": "granted", "owner_user_id": str(USER_1)})

        assert [kind for _, kind, _ in desk.events] == [
            "browser.control_granted", "browser.control_returned", "session.resume", "browser.control_granted",
        ]
        assert desk.wakes == [str(sid)]

    async def test_a_local_folder_chats_hand_back_is_made_whether_or_not_the_agents_other_chats_can_be_told(
        self, desk,
    ) -> None:
        sid, away, told = desk.chat(), desk.chat(), desk.chat()
        # Two more chats of the agent on the computer still say their user holds its browser.
        desk.store.events[away] = ["browser.control_granted"]
        desk.store.events[told] = ["browser.control_granted"]
        emit = desk.store.emitter(desk.events)

        async def one_log_away(session_id: str, event_type: Any, data: dict) -> None:
            if session_id == str(away):
                raise RuntimeError("the chat's log is away")
            await emit(session_id, event_type, data)

        desk.store.emitter = lambda _events: one_log_away

        await desk.control(sid, "acquire")
        # One could not be told: the hand back is made all the same, and the next chat is told.
        assert await desk.control(sid, "release", handed_back=True) == (200, {**RELEASED, "resumes": True})
        assert desk.events[1:] == [
            _handed_back(sid, resumes=True), _resumed(sid), _handed_back(told, handed_back_from=str(sid)),
        ]
        # Only the chat it was handed back from is woken.
        assert desk.wakes == [str(sid)]

        async def no_answer(**_: Any) -> list[UUID]:
            raise RuntimeError("the database is away")

        # The other chats cannot even be looked for: the hand back is made, told to its own chat, and its agent woken.
        desk.store.chats_told_taken_over = no_answer
        desk.store.sessions[sid].status = "completed"
        await desk.control(sid, "acquire")
        assert await desk.control(sid, "release", handed_back=True) == (200, {**RELEASED, "resumes": True})
        assert desk.events[4:] == [_taken_over(sid), _handed_back(sid, resumes=True), _resumed(sid)]
        assert desk.wakes == [str(sid), str(sid)]


class _StubBrowserPool:
    def __init__(self) -> None:
        self.destroyed: list[str] = []

    async def destroy_for_session(self, session_id: str) -> None:
        self.destroyed.append(session_id)


class _StubBackend:
    def __init__(self) -> None:
        self.destroyed: list[str] = []

    async def destroy_for_session(self, session_id: str) -> None:
        self.destroyed.append(session_id)


class _StubRegistry:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    async def delete(self, session_id: str) -> None:
        self.deleted.append(session_id)


class TestDeleteEndpoint:
    async def test_destroys_pool_backend_and_registry(self, app_factory) -> None:
        build, resolver, _control = app_factory
        sid = str(uuid4())
        resolver.entries[sid] = _resolved(sid)
        pool = _StubBrowserPool()
        backend = _StubBackend()
        registry = _StubRegistry()
        app = build()
        app.state.browser_pool = pool
        app.state.browser_backend = backend
        app.state.browser_registry = registry

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.delete(f"/v1/sessions/{sid}/browser")

        assert response.status_code == 204
        assert pool.destroyed == [sid]
        assert backend.destroyed == [sid]
        assert registry.deleted == [sid]

    async def test_idempotent_when_no_browser(self, app_factory) -> None:
        build, _resolver, _control = app_factory
        sid = str(uuid4())
        pool = _StubBrowserPool()
        registry = _StubRegistry()
        app = build()
        app.state.browser_pool = pool
        app.state.browser_registry = registry

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.delete(f"/v1/sessions/{sid}/browser")

        # 204 with no destroy / no delete calls — the session simply
        # has no browser to close.
        assert response.status_code == 204
        assert pool.destroyed == []
        assert registry.deleted == []

    async def test_cross_tenant_browser_invisible(self, app_factory) -> None:
        """A session in a different org must be unaddressable: the
        resolver returns None, the response is 204, and no destruction
        happens."""
        build, resolver, _control = app_factory
        sid = str(uuid4())
        foreign_org = uuid4()
        resolver.entries[sid] = _resolved(sid, org_id=foreign_org)
        pool = _StubBrowserPool()
        registry = _StubRegistry()
        app = build()  # tenant defaults to ORG_1
        app.state.browser_pool = pool
        app.state.browser_registry = registry

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.delete(f"/v1/sessions/{sid}/browser")

        assert response.status_code == 204
        assert pool.destroyed == []
        assert registry.deleted == []

    async def test_swallows_destroy_errors(self, app_factory) -> None:
        """Individual cleanup failures don't fail the request — the
        client gets a 204 regardless so retries don't compound the
        problem. Errors are logged for ops triage."""
        build, resolver, _control = app_factory
        sid = str(uuid4())
        resolver.entries[sid] = _resolved(sid)

        class _ExplodingPool:
            async def destroy_for_session(self, _session_id: str) -> None:
                raise RuntimeError("simulated pool failure")

        registry = _StubRegistry()
        app = build()
        app.state.browser_pool = _ExplodingPool()
        app.state.browser_registry = registry

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.delete(f"/v1/sessions/{sid}/browser")

        # Pool errored, but registry still got cleaned and response is 204.
        assert response.status_code == 204
        assert registry.deleted == [sid]


class TestPreviewEndpoint:
    async def test_preview_returns_screenshot_png(
        self,
        app_factory,
        monkeypatch,
    ) -> None:
        from surogates.api.routes import browser as browser_routes

        build, resolver, _control = app_factory
        sid = str(uuid4())
        resolver.entries[sid] = _resolved(sid)
        seen: list[str] = []
        screenshot_kwargs: list[dict[str, object]] = []

        class FakePreviewClient:
            def __init__(self, rest_url: str) -> None:
                seen.append(rest_url)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args) -> None:
                return None

            async def screenshot(self, **kwargs) -> dict[str, bytes]:
                screenshot_kwargs.append(kwargs)
                return {"png_bytes": b"\x89PNG\r\n\x1a\npreview"}

        monkeypatch.setattr(
            browser_routes,
            "_browser_preview_client",
            FakePreviewClient,
            raising=False,
        )

        async with AsyncClient(
            transport=ASGITransport(app=build()),
            base_url="http://test",
        ) as client:
            response = await client.get(f"/v1/sessions/{sid}/browser/preview.png")

        assert response.status_code == 200
        assert response.headers["content-type"] == "image/png"
        assert response.headers["cache-control"] == "no-store"
        assert response.content == b"\x89PNG\r\n\x1a\npreview"
        assert seen == ["http://browser-x.svc:10001"]
        assert screenshot_kwargs == [{}]

    async def test_preview_unreachable_browser_404s_and_forgets_it(
        self,
        app_factory,
        monkeypatch,
    ) -> None:
        """An entry nothing answers is absent, not a bad gateway.

        502 reads as "retry later", so a polling client loops on it forever
        and fills the proxy log. 404 is a verdict the caller can act on, and
        the stale entry goes with it so the next resolve answers honestly.
        """

        from surogates.api.routes import browser as browser_routes

        build, resolver, _control = app_factory
        sid = str(uuid4())
        resolver.entries[sid] = _resolved(sid)

        class DeadPreviewClient:
            def __init__(self, rest_url: str) -> None:
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args) -> None:
                return None

            async def screenshot(self, **_kwargs):
                raise httpx.ConnectError("All connection attempts failed")

        monkeypatch.setattr(
            browser_routes,
            "_browser_preview_client",
            DeadPreviewClient,
            raising=False,
        )

        async with AsyncClient(
            transport=ASGITransport(app=build()),
            base_url="http://test",
        ) as client:
            response = await client.get(f"/v1/sessions/{sid}/browser/preview.png")

        assert response.status_code == 404
        assert sid in resolver.forgotten

    async def test_preview_slow_screenshot_502s_and_keeps_the_entry(
        self,
        app_factory,
        monkeypatch,
    ) -> None:
        """A read timeout is a browser that answered slowly, not one that is gone.

        ReadTimeout subclasses TransportError, and classifying it as
        "unreachable" deleted live browsers' registry entries whenever a
        screenshot of a page mid-load ran long -- a working session then said
        "Browser disconnected". Only a refused connection may prune.
        """

        from surogates.api.routes import browser as browser_routes

        build, resolver, _control = app_factory
        sid = str(uuid4())
        resolver.entries[sid] = _resolved(sid)

        class SlowPreviewClient:
            def __init__(self, rest_url: str) -> None:
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args) -> None:
                return None

            async def screenshot(self, **_kwargs):
                raise httpx.ReadTimeout("screenshot ran long")

        monkeypatch.setattr(
            browser_routes,
            "_browser_preview_client",
            SlowPreviewClient,
            raising=False,
        )

        async with AsyncClient(
            transport=ASGITransport(app=build()),
            base_url="http://test",
        ) as client:
            response = await client.get(f"/v1/sessions/{sid}/browser/preview.png")

        assert response.status_code == 502
        assert resolver.forgotten == []
        assert sid in resolver.entries

    async def test_preview_unknown_session_returns_404(self, app_factory) -> None:
        build, _resolver, _control = app_factory

        async with AsyncClient(
            transport=ASGITransport(app=build()),
            base_url="http://test",
        ) as client:
            response = await client.get(
                "/v1/sessions/00000000-0000-0000-0000-000000000001/browser/preview.png",
            )

        assert response.status_code == 404

    async def test_preview_other_org_returns_404(self, app_factory) -> None:
        build, resolver, _control = app_factory
        sid = str(uuid4())
        resolver.entries[sid] = _resolved(sid, org_id=ORG_1)

        async with AsyncClient(
            transport=ASGITransport(app=build(org_id=ORG_2)),
            base_url="http://test",
        ) as client:
            response = await client.get(f"/v1/sessions/{sid}/browser/preview.png")

        assert response.status_code == 404


def _event_recorder(events: list[tuple[str, str, dict]]):
    async def emit(session_id: str, event_type, data: dict) -> None:
        event_value = getattr(event_type, "value", event_type)
        events.append((session_id, event_value, data))

    return emit


def _wake_recorder(wakes: list[str]):
    async def wake(session_id: str) -> None:
        wakes.append(session_id)

    return wake


async def _wake_noop(_session_id: str) -> None:
    return None
