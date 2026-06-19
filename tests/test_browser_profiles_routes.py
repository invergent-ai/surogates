import asyncio
import uuid
from dataclasses import replace
from datetime import datetime, timezone

from fastapi import FastAPI
from starlette.testclient import TestClient

import surogates.api.routes.browser_profiles as bp
from surogates.browser.profiles import BrowserProfileRow
from surogates.tenant.context import TenantContext


class _FakeStore:
    def __init__(self):
        self.rows = []

    async def list(self, org_id, *, user_id, service_account_id):
        return list(self.rows)

    async def create(self, org_id, *, user_id, service_account_id, name):
        if any(r.name == name for r in self.rows):
            from sqlalchemy.exc import IntegrityError

            raise IntegrityError("INSERT", {}, Exception("unique violation"))
        row = BrowserProfileRow(
            uuid.uuid4(), name, "manual_vnc", [],
            datetime.now(timezone.utc), None, False,
        )
        self.rows.append(row)
        return row

    async def rename(self, profile_id, org_id, *, user_id, service_account_id, name):
        for i, row in enumerate(self.rows):
            if row.id == profile_id:
                self.rows[i] = replace(row, name=name)
                return True
        return False

    async def delete(self, profile_id, org_id, *, user_id, service_account_id):
        before = len(self.rows)
        self.rows = [r for r in self.rows if r.id != profile_id]
        return len(self.rows) != before

    async def save_capture(
        self, profile_id, org_id, *, user_id, service_account_id, storage_state
    ):
        for i, row in enumerate(self.rows):
            if row.id == profile_id:
                updated = replace(row, has_state=True, cookie_domains=["google.com"])
                self.rows[i] = updated
                return updated
        raise KeyError("profile not found")


def _app(store, *, user_id=None, sa_id=None):
    sa_id = sa_id or uuid.uuid4()
    app = FastAPI()
    app.include_router(bp.router)
    app.state.browser_profile_store = store

    async def _tenant():
        return TenantContext(
            org_id=uuid.uuid4(),
            user_id=user_id,
            org_config={},
            user_preferences={},
            permissions=frozenset(),
            asset_root="/tmp",
            service_account_id=sa_id,
        )

    app.dependency_overrides[bp.get_current_tenant] = _tenant
    return app


def test_create_then_list_returns_metadata_only():
    store = _FakeStore()
    client = TestClient(_app(store))
    created = client.post("/browser-profiles", json={"name": "Personal"})
    assert created.status_code == 201
    body = created.json()
    assert body["name"] == "Personal"
    assert body["has_state"] is False
    assert "storage_state_enc" not in body
    listed = client.get("/browser-profiles").json()
    assert [p["name"] for p in listed] == ["Personal"]


def test_create_duplicate_name_returns_409():
    store = _FakeStore()
    client = TestClient(_app(store))
    assert client.post("/browser-profiles", json={"name": "Work"}).status_code == 201
    dup = client.post("/browser-profiles", json={"name": "Work"})
    assert dup.status_code == 409


def test_rename_and_delete():
    store = _FakeStore()
    client = TestClient(_app(store))
    pid = client.post("/browser-profiles", json={"name": "P"}).json()["id"]
    renamed = client.patch(
        f"/browser-profiles/{pid}", json={"name": "Renamed"}
    )
    assert renamed.status_code == 200
    assert client.get("/browser-profiles").json()[0]["name"] == "Renamed"
    deleted = client.delete(f"/browser-profiles/{pid}")
    assert deleted.status_code == 204
    assert client.get("/browser-profiles").json() == []


class _SettingsStub:
    storage = type("S", (), {"bucket": "test-bucket", "key_prefix": ""})()
    llm = type("L", (), {"model": "gpt-4o"})()


class _StorageStub:
    def __init__(self, sink):
        self._sink = sink

    async def create_bucket(self, bucket):
        self._sink["bucket"] = bucket

    def resolve_workspace_path(self, bucket, sid):
        return f"/tmp/{bucket}/{sid}"


def test_setup_session_creates_browser_setup_and_wakes():
    # Provisioning is worker-driven: the API creates the browser_setup session
    # (stamping the owner into config) and wakes it — the worker's loop does the
    # actual provision + control grant.
    store = _FakeStore()
    app = _app(store)
    created = {}
    waked = {"sid": None}

    row = asyncio.run(
        store.create(uuid.uuid4(), user_id=None, service_account_id=uuid.uuid4(), name="P")
    )

    class _SessionStore:
        async def create_session(self, **kw):
            created.update(kw)

            class _S:
                id = kw["session_id"]
                config = kw["config"]

            return _S()

    async def _wake(sid):
        waked["sid"] = sid

    app.state.session_store = _SessionStore()
    app.state.session_wake = _wake
    app.state.storage = _StorageStub(created)
    app.state.settings = _SettingsStub()

    client = TestClient(app)
    resp = client.post(
        f"/browser-profiles/{row.id}/setup-session",
        json={"owner_user_id": "ops-user", "agent_id": "agent-1"},
    )
    assert resp.status_code == 200
    assert created["channel"] == "browser_setup"
    assert created["config"]["browser"]["profile_id"] == str(row.id)
    assert created["config"]["browser"]["setup_owner_user_id"] == "ops-user"
    assert waked["sid"] == str(created["session_id"])
    assert "session_id" in resp.json()
    assert "expires_at" in resp.json()


def _seed_profile(store):
    return asyncio.run(
        store.create(uuid.uuid4(), user_id=None, service_account_id=uuid.uuid4(), name="P")
    )


def test_capture_rejects_non_setup_session():
    store = _FakeStore()
    app = _app(store)
    row = _seed_profile(store)

    async def _async(v):
        return v

    class _SessionStore:
        async def get_session(self, _sid):
            class _S:
                channel = "web"  # not browser_setup
                config = {"browser": {"profile_id": str(row.id)}}

            return _S()

    app.state.session_store = _SessionStore()
    app.state.browser_control = type(
        "C", (), {"held_by": staticmethod(lambda sid: _async("ops-user"))}
    )()
    app.state.browser_resolver = type(
        "R", (), {"resolve": staticmethod(lambda sid, expected_org_id=None: _async(object()))}
    )()

    client = TestClient(app)
    resp = client.post(
        f"/browser-profiles/{row.id}/capture",
        params={"session_id": str(uuid.uuid4())},
        json={"owner_user_id": "ops-user"},
    )
    assert resp.status_code == 409


def test_capture_saves_storage_state(monkeypatch):
    store = _FakeStore()
    app = _app(store)
    row = _seed_profile(store)
    sid = uuid.uuid4()
    teardown = {"status": None, "waked": None}

    class _SessionStore:
        async def get_session(self, _sid):
            class _S:
                channel = "browser_setup"
                config = {"browser": {"profile_id": str(row.id)}}

            return _S()

        async def update_session_status(self, _sid, status):
            teardown["status"] = status

    class _Control:
        async def held_by(self, _sid):
            return "ops-user"

    class _Resolver:
        async def resolve(self, _sid, expected_org_id=None):
            from surogates.browser.base import BrowserEndpoint
            from surogates.browser.resolver import ResolvedBrowser

            return ResolvedBrowser(
                session_id=str(sid),
                endpoint=BrowserEndpoint("http://browser", "ws://cdp", "ws://live"),
            )

    class _Client:
        def __init__(self, rest_url):
            assert rest_url == "http://browser"

        async def storage_state(self):
            return {
                "cookies": [{"name": "SID", "domain": ".google.com"}],
                "origins": [],
            }

        async def close(self):
            pass

    async def _wake(_sid):
        teardown["waked"] = _sid

    monkeypatch.setattr(bp, "KernelBrowserClient", _Client)
    app.state.session_store = _SessionStore()
    app.state.browser_control = _Control()
    app.state.browser_resolver = _Resolver()
    app.state.session_wake = _wake

    client = TestClient(app)
    resp = client.post(
        f"/browser-profiles/{row.id}/capture",
        params={"session_id": str(sid)},
        json={"owner_user_id": "ops-user"},
    )
    assert resp.status_code == 200
    assert resp.json()["has_state"] is True
    # The encrypted blob is never exposed in the response.
    assert "storage_state_enc" not in resp.json()
    # Teardown: the session is flipped terminal and re-woken so the worker
    # (which owns the pool) releases the browser.
    assert teardown["status"] == "completed"
    assert teardown["waked"] == str(sid)
