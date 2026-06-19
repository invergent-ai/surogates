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
    created = client.post("/api/browser-profiles", json={"name": "Personal"})
    assert created.status_code == 201
    body = created.json()
    assert body["name"] == "Personal"
    assert body["has_state"] is False
    assert "storage_state_enc" not in body
    listed = client.get("/api/browser-profiles").json()
    assert [p["name"] for p in listed] == ["Personal"]


def test_rename_and_delete():
    store = _FakeStore()
    client = TestClient(_app(store))
    pid = client.post("/api/browser-profiles", json={"name": "P"}).json()["id"]
    renamed = client.patch(
        f"/api/browser-profiles/{pid}", json={"name": "Renamed"}
    )
    assert renamed.status_code == 200
    assert client.get("/api/browser-profiles").json()[0]["name"] == "Renamed"
    deleted = client.delete(f"/api/browser-profiles/{pid}")
    assert deleted.status_code == 204
    assert client.get("/api/browser-profiles").json() == []


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


def test_setup_session_creates_browser_setup_without_wake():
    store = _FakeStore()
    app = _app(store)
    created = {}
    waked = {"called": False}

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

    async def _ensure(**kw):
        return None

    class _Control:
        async def acquire(self, sid, uid):
            from surogates.browser.control import AcquireOutcome, ControlEntry

            return AcquireOutcome.GRANTED, ControlEntry(
                uid, datetime.now(timezone.utc)
            )

    app.state.session_store = _SessionStore()
    app.state.browser_pool = type("P", (), {"ensure": staticmethod(_ensure)})()
    app.state.browser_control = _Control()
    app.state.session_wake = lambda sid: waked.update(called=True)
    app.state.storage = _StorageStub(created)
    app.state.settings = _SettingsStub()

    client = TestClient(app)
    resp = client.post(
        f"/api/browser-profiles/{row.id}/setup-session",
        json={"owner_user_id": "ops-user", "agent_id": "agent-1"},
    )
    assert resp.status_code == 200
    assert created["channel"] == "browser_setup"
    assert created["config"]["browser"]["profile_id"] == str(row.id)
    assert waked["called"] is False
    assert "session_id" in resp.json()
    assert "expires_at" in resp.json()
