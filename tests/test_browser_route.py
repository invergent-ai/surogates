"""REST endpoints for browser state and control."""

from __future__ import annotations

from datetime import datetime, timezone
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


@pytest.fixture()
def app_factory():
    from surogates.api.routes import browser as browser_routes
    from surogates.tenant.auth.middleware import get_current_tenant

    resolver = StubResolver()
    control = StubControl()

    def build(*, org_id: UUID = ORG_1, user_id: UUID | None = USER_1) -> FastAPI:
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


class TestLiveViewHTTPProxy:
    async def test_vnc_html_is_proxied(self, app_factory, monkeypatch) -> None:
        from surogates.api.routes import browser as browser_routes

        build, resolver, _control = app_factory
        sid = str(uuid4())
        resolver.entries[sid] = _resolved(sid)
        seen: list[str] = []

        async def fake_request(method, url, **kwargs):
            seen.append(str(url))
            return httpx.Response(
                status_code=200,
                text="<html>neko</html>",
                headers={"content-type": "text/html"},
            )

        monkeypatch.setattr(
            browser_routes,
            "_proxy_live_view_request",
            fake_request,
            raising=False,
        )

        async with AsyncClient(
            transport=ASGITransport(app=build()),
            base_url="http://test",
        ) as client:
            response = await client.get(f"/v1/sessions/{sid}/browser/live/")

        assert response.status_code == 200
        assert response.text == "<html>neko</html>"
        assert seen == ["http://browser-x.svc:443/"]

    async def test_unknown_session_returns_404(self, app_factory) -> None:
        build, _resolver, _control = app_factory

        async with AsyncClient(
            transport=ASGITransport(app=build()),
            base_url="http://test",
        ) as client:
            response = await client.get(
                "/v1/sessions/00000000-0000-0000-0000-000000000001/browser/live/",
            )

        assert response.status_code == 404

    async def test_static_asset_strips_token_query_param(
        self,
        app_factory,
        monkeypatch,
    ) -> None:
        from surogates.api.routes import browser as browser_routes

        build, resolver, _control = app_factory
        sid = str(uuid4())
        resolver.entries[sid] = _resolved(sid)
        params_seen: list[dict[str, str]] = []

        async def fake_request(method, url, **kwargs):
            params_seen.append(dict(kwargs.get("params", {})))
            return httpx.Response(
                status_code=200,
                text="console.log('neko')",
                headers={"content-type": "application/javascript"},
            )

        monkeypatch.setattr(
            browser_routes,
            "_proxy_live_view_request",
            fake_request,
            raising=False,
        )

        async with AsyncClient(
            transport=ASGITransport(app=build()),
            base_url="http://test",
        ) as client:
            response = await client.get(
                f"/v1/sessions/{sid}/browser/live/app.js",
                params={"token": "secret", "cache": "1"},
            )

        assert response.status_code == 200
        assert params_seen == [{"cache": "1"}]


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
