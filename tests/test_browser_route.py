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
    """The session store in a test: each session as its row has it, and the types of its browser events in order."""

    def __init__(self) -> None:
        self.sessions: dict[UUID, Any] = {}
        self.events: dict[UUID, list[str]] = {}

    async def get_session(self, session_id: UUID) -> Any:
        if session_id not in self.sessions:
            raise LookupError(session_id)
        return self.sessions[session_id]

    async def get_events(self, session_id: UUID, *, types: list[Any] | None = None, **_: Any) -> list[Any]:
        wanted = {kind.value for kind in types or []}
        return [SimpleNamespace(type=kind) for kind in self.events.get(session_id, []) if kind in wanted]

    def emitter(self, events: list[tuple[str, str, dict]]):
        """The app's emitter in a test: what it emits is recorded, and is in its session's log for the routes to read back."""
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
            )

        app.dependency_overrides[get_current_tenant] = fake_tenant
        return app

    return build, resolver, control


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

    async def test_a_local_folder_chats_take_over_and_hand_back_are_told_to_the_chat_and_the_hand_back_wakes_it(
        self, app_factory,
    ) -> None:
        build, _resolver, _control = app_factory
        sid = uuid4()
        store = StubSessions()
        store.sessions[sid] = SimpleNamespace(
            org_id=ORG_1, agent_id="agent", config={"execution": {"kind": "device", "device_id": str(uuid4())}},
        )
        events: list[tuple[str, str, dict]] = []
        wakes: list[str] = []

        def built(org_id: UUID = ORG_1) -> FastAPI:
            app = build(org_id=org_id)
            app.state.session_store = store
            app.state.session_event_emitter = store.emitter(events)
            app.state.session_wake = _wake_recorder(wakes)
            return app

        async with AsyncClient(transport=ASGITransport(app=built()), base_url="http://test") as client:
            async def control(action: str) -> httpx.Response:
                return await client.post(f"/v1/sessions/{sid}/browser/control", json={"action": action})

            # The pause is the user's computer's: no browser here and no lease, so each is told as it comes.
            assert (await control("acquire")).json() == {"outcome": "granted", "owner_user_id": str(USER_1)}
            assert wakes == []
            assert (await control("release")).json() == {"outcome": "released"}

        assert events == [
            (str(sid), "browser.control_granted", {"session_id": str(sid), "owner_user_id": str(USER_1), "computer": True}),
            (str(sid), "browser.control_returned", {"session_id": str(sid), "released_by": str(USER_1), "computer": True}),
        ]
        # Handed back: its agent goes on, as it does once a user hands the cloud's browser back.
        assert wakes == [str(sid)]

        # Another organisation's chat is not known here.
        async with AsyncClient(transport=ASGITransport(app=built(ORG_2)), base_url="http://test") as client:
            response = await client.post(f"/v1/sessions/{sid}/browser/control", json={"action": "release"})
        assert response.status_code == 404
        assert len(events) == 2

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

    async def test_a_local_folder_chats_hand_back_is_told_once_and_only_after_a_take_over(self, app_factory) -> None:
        build, _resolver, _control = app_factory
        sid = uuid4()
        store = StubSessions()
        store.sessions[sid] = SimpleNamespace(
            org_id=ORG_1, agent_id="agent", config={"execution": {"kind": "device", "device_id": str(uuid4())}},
        )
        events: list[tuple[str, str, dict]] = []
        wakes: list[str] = []
        app = build()
        app.state.session_store = store
        app.state.session_event_emitter = store.emitter(events)
        app.state.session_wake = _wake_recorder(wakes)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            async def control(action: str) -> httpx.Response:
                return await client.post(f"/v1/sessions/{sid}/browser/control", json={"action": action})

            # Never taken over: nothing to hand back, so the chat is told nothing and its agent is not woken.
            unheld = await control("release")
            assert (unheld.status_code, unheld.json()) == (200, {"outcome": "released"})
            assert (events, wakes) == ([], [])

            await control("acquire")
            await control("release")
            assert [kind for _, kind, _ in events] == ["browser.control_granted", "browser.control_returned"]
            assert wakes == [str(sid)]

            # Handed back already: a repeat answers the same, and tells and wakes no more.
            again = await control("release")
            assert (again.status_code, again.json()) == (200, {"outcome": "released"})

        assert [kind for _, kind, _ in events] == ["browser.control_granted", "browser.control_returned"]
        assert wakes == [str(sid)]

    async def test_a_local_folder_chats_take_over_is_told_once_while_it_stands(self, app_factory) -> None:
        build, _resolver, _control = app_factory
        sid = uuid4()
        store = StubSessions()
        store.sessions[sid] = SimpleNamespace(
            org_id=ORG_1, agent_id="agent", config={"execution": {"kind": "device", "device_id": str(uuid4())}},
        )
        events: list[tuple[str, str, dict]] = []
        wakes: list[str] = []
        app = build()
        app.state.session_store = store
        app.state.session_event_emitter = store.emitter(events)
        app.state.session_wake = _wake_recorder(wakes)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            async def control(action: str) -> httpx.Response:
                return await client.post(f"/v1/sessions/{sid}/browser/control", json={"action": action})

            assert (await control("acquire")).json() == {"outcome": "granted", "owner_user_id": str(USER_1)}
            # Told already: the cloud's own answer to an acquire that changes nothing.
            again = await control("acquire")
            assert (again.status_code, again.json()) == (200, {"outcome": "refreshed", "owner_user_id": str(USER_1)})
            assert [kind for _, kind, _ in events] == ["browser.control_granted"]

            # Handed back, a new take-over is told anew.
            await control("release")
            assert (await control("acquire")).json() == {"outcome": "granted", "owner_user_id": str(USER_1)}

        assert [kind for _, kind, _ in events] == [
            "browser.control_granted", "browser.control_returned", "browser.control_granted",
        ]
        assert wakes == [str(sid)]


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
