"""REST endpoints for browser state and control."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from surogates.browser.base import BrowserEndpoint
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
