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
