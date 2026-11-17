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

    async def update(self, profile_id, org_id, *, user_id, service_account_id,
                     name=None, use_in_channels=None, shared_with_agents=None):
        owner = dict(user_id=user_id, service_account_id=service_account_id)
        ok = True
        if use_in_channels is not None:
            ok = await self.set_use_in_channels(
                profile_id, org_id, service_account_id=service_account_id, channels=use_in_channels,
            )
        if ok and shared_with_agents is not None:
            ok = await self.set_shared_with_agents(profile_id, org_id, agent_ids=shared_with_agents, **owner)
        if ok and name is not None:
            ok = await self.rename(profile_id, org_id, name=name, **owner)
        return ok

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


def _app(store, *, user_id=None, sa_id=None, org_id=None):
    sa_id = sa_id or uuid.uuid4()
    org_id = org_id or uuid.uuid4()
    app = FastAPI()
    app.include_router(bp.router)
    app.state.browser_profile_store = store

    async def _tenant():
        return TenantContext(
            org_id=org_id,
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


def test_setup_session_402_when_out_of_minutes(monkeypatch):
    from surogates.browser.base import BrowserCreditsExhaustedError

    store = _FakeStore()
    app = _app(store)
    row = asyncio.run(
        store.create(uuid.uuid4(), user_id=None, service_account_id=uuid.uuid4(), name="P")
    )

    async def _exhausted(_org_id):
        raise BrowserCreditsExhaustedError("This project is out of browsing minutes.")

    monkeypatch.setattr(bp, "assert_browser_minutes_available", _exhausted)

    client = TestClient(app)
    resp = client.post(
        f"/browser-profiles/{row.id}/setup-session",
        json={"owner_user_id": "ops-user"},
    )
    assert resp.status_code == 402


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
    _capture(monkeypatch, _FakeStore(), owner=None)


def test_a_login_captured_for_the_agent_is_saved_under_the_agent(monkeypatch):
    saved_under = []

    class _Store(_OwnerStore):
        async def save_capture(self, profile_id, org_id, *, user_id, service_account_id, storage_state):
            saved_under.append(service_account_id)
            return await _FakeStore.save_capture(
                self, profile_id, org_id, user_id=user_id,
                service_account_id=service_account_id, storage_state=storage_state,
            )

    store = _Store()
    agent_sa = _capture(monkeypatch, store, owner="agent")
    assert saved_under == [agent_sa]


def _capture(monkeypatch, store, *, owner):
    if owner == "agent":
        app, agent_sa = _agent_app(monkeypatch, store)
    else:
        app, agent_sa = _app(store), None
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
    params = {"session_id": str(sid)}
    if owner:
        params.update(owner=owner, agent_id="agent-1")
    resp = client.post(
        f"/browser-profiles/{row.id}/capture",
        params=params,
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
    return agent_sa


def test_a_setup_browser_is_captured_by_its_own_user_and_by_no_other_member(monkeypatch):
    org, yours, theirs = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    store = _FakeStore()
    row = _seed_profile(store)
    sid = uuid.uuid4()
    read: list[str] = []
    torn_down: list[str] = []

    class _SessionStore:
        async def get_session(self, _sid):
            # The setup session its user made, whose browser they took over to log in.
            return type("S", (), {
                "id": sid, "org_id": org, "user_id": yours, "agent_id": "browser-setup",
                "channel": "browser_setup", "config": {"browser": {"profile_id": str(row.id)}},
            })()

        async def update_session_status(self, _sid, status):
            torn_down.append(status)

    class _Control:
        async def held_by(self, _sid):
            return str(yours)

    class _Resolver:
        async def resolve(self, _sid, expected_org_id=None):
            from surogates.browser.base import BrowserEndpoint
            from surogates.browser.resolver import ResolvedBrowser

            return ResolvedBrowser(session_id=str(sid), endpoint=BrowserEndpoint("http://browser", "ws://cdp", "ws://live"))

    class _Client:
        def __init__(self, rest_url):
            pass

        async def storage_state(self):
            read.append(str(sid))
            return {"cookies": [{"name": "SID", "domain": ".google.com"}], "origins": []}

        async def close(self):
            pass

    async def _wake(_sid):
        pass

    monkeypatch.setattr(bp, "KernelBrowserClient", _Client)

    def capture(user_id):
        app = _app(store, user_id=user_id, org_id=org)
        app.state.session_store = _SessionStore()
        app.state.browser_control = _Control()
        app.state.browser_resolver = _Resolver()
        app.state.session_wake = _wake
        # Naming the user who holds the browser, as the service path does.
        return TestClient(app).post(
            f"/browser-profiles/{row.id}/capture", params={"session_id": str(sid)}, json={"owner_user_id": str(yours)},
        )

    # Another member of the organisation: as for no session, and nothing of the browser is read or torn down.
    refused = capture(theirs)
    assert (refused.status_code, refused.json()) == (404, {"detail": "Session not found"})
    assert (read, torn_down) == ([], [])

    assert capture(yours).status_code == 200
    assert (read, torn_down) == ([str(sid)], ["completed"])


def test_a_key_of_another_organisation_bound_to_an_agent_of_that_name_captures_nothing(monkeypatch):
    org, elsewhere, holder = uuid.uuid4(), uuid.uuid4(), str(uuid.uuid4())
    store = _FakeStore()
    row = _seed_profile(store)
    sid = uuid.uuid4()
    asked: list[str] = []

    class _SessionStore:
        async def get_session(self, _sid):
            return type("S", (), {
                "id": sid, "org_id": org, "user_id": None, "agent_id": "browser-setup",
                "channel": "browser_setup", "config": {"browser": {"profile_id": str(row.id)}},
            })()

    class _Control:
        async def held_by(self, _sid):
            return holder

    class _Resolver:
        async def resolve(self, _sid, expected_org_id=None):
            # The browser is the session's org's: one of another org is none.
            asked.append(expected_org_id)
            return None

    def _never(_rest_url):
        raise AssertionError("the browser was reached")

    monkeypatch.setattr(bp, "KernelBrowserClient", _never)
    app = _app(store, org_id=elsewhere)
    app.dependency_overrides[bp.get_current_tenant] = lambda: TenantContext(
        org_id=elsewhere, user_id=None, org_config={}, user_preferences={}, permissions=frozenset(), asset_root="/tmp",
        service_account_id=uuid.uuid4(), service_account_agent_id="browser-setup",
    )
    app.state.session_store = _SessionStore()
    app.state.browser_control = _Control()
    app.state.browser_resolver = _Resolver()

    refused = TestClient(app).post(
        f"/browser-profiles/{row.id}/capture", params={"session_id": str(sid)}, json={"owner_user_id": holder},
    )
    # Stopped at the session, as for no session: its browser is not even looked for.
    assert (refused.status_code, refused.json(), asked) == (404, {"detail": "Session not found"}, [])


class _OwnerStore(_FakeStore):
    """Rows per owning principal, as the real store keeps them."""

    def __init__(self):
        super().__init__()
        self.by_owner = {}
        self.channels = {}

    async def list(self, org_id, *, user_id, service_account_id):
        return list(self.by_owner.get(service_account_id or user_id, []))

    async def create(self, org_id, *, user_id, service_account_id, name):
        row = BrowserProfileRow(
            uuid.uuid4(), name, "manual_vnc", [], datetime.now(timezone.utc), None, False,
        )
        self.by_owner.setdefault(service_account_id or user_id, []).append(row)
        self.rows.append(row)
        return row

    async def set_use_in_channels(self, profile_id, org_id, *, service_account_id, channels):
        if not any(r.id == profile_id for r in self.by_owner.get(service_account_id, [])):
            return False
        self.channels[profile_id] = sorted(set(channels))
        return True


def _agent_app(monkeypatch, store, *, operator=True, principal=True):
    agent_sa = uuid.uuid4()
    app = _app(store)
    app.state.session_store = object()
    app.state.session_factory = object()

    async def is_operator(session_store, sa_id):
        return operator

    async def find(session_factory, org_id, agent_id):
        assert agent_id == "agent-1"
        return type("P", (), {"id": agent_sa})() if principal else None

    monkeypatch.setattr(bp, "is_ops_chat_service_account", is_operator)
    monkeypatch.setattr(bp, "find_agent_principal", find)
    return app, agent_sa


def test_an_operator_keeps_a_login_for_the_agent_apart_from_their_own(monkeypatch):
    store = _OwnerStore()
    app, agent_sa = _agent_app(monkeypatch, store)
    client = TestClient(app)

    made = client.post("/browser-profiles?owner=agent&agent_id=agent-1", json={"name": "X account"})
    assert made.status_code == 201 and made.json()["owner"] == "agent"
    assert [r.name for r in store.by_owner[agent_sa]] == ["X account"]
    assert client.get("/browser-profiles").json() == []
    listed = client.get("/browser-profiles?owner=agent&agent_id=agent-1").json()
    assert [(p["name"], p["owner"]) for p in listed] == [("X account", "agent")]

    pid = made.json()["id"]
    granted = client.patch(
        f"/browser-profiles/{pid}?owner=agent&agent_id=agent-1",
        json={"use_in_channels": ["slack", "scheduled"]},
    )
    assert granted.status_code == 200
    assert store.channels[uuid.UUID(pid)] == ["scheduled", "slack"]


def test_only_an_operator_manages_the_agents_logins(monkeypatch):
    app, _ = _agent_app(monkeypatch, _OwnerStore(), operator=False)
    assert TestClient(app).get("/browser-profiles?owner=agent&agent_id=agent-1").status_code == 403


def test_an_agent_without_an_identity_of_its_own_keeps_no_login(monkeypatch):
    app, _ = _agent_app(monkeypatch, _OwnerStore(), principal=False)
    assert TestClient(app).post(
        "/browser-profiles?owner=agent&agent_id=agent-1", json={"name": "X"},
    ).status_code == 404


def test_a_personal_login_is_never_granted_to_channels(monkeypatch):
    store = _OwnerStore()
    app, _ = _agent_app(monkeypatch, store)
    client = TestClient(app)
    pid = client.post("/browser-profiles", json={"name": "Mine"}).json()["id"]
    refused = client.patch(f"/browser-profiles/{pid}", json={"use_in_channels": ["slack"]})
    assert refused.status_code == 422
    assert store.channels == {}


class _SharingStore(_OwnerStore):
    """Adds sharing, as the real store keeps it: only the owner sets it, never on an agent's own."""

    def __init__(self, agent_owned=()):
        super().__init__()
        self.shared = {}
        self.agent_owned = set(agent_owned)
        self.usable = {}
        self.in_org = []

    async def set_shared_with_agents(self, profile_id, org_id, *, user_id, service_account_id, agent_ids):
        owner = service_account_id or user_id
        if owner in self.agent_owned or not any(r.id == profile_id for r in self.by_owner.get(owner, [])):
            return False
        self.shared[profile_id] = sorted(set(agent_ids))
        return True

    async def usable_by_agent(self, org_id, *, agent_sa, agent_id):
        return self.usable.get((agent_sa, agent_id), [])

    async def shared_in_org(self, org_id):
        return self.in_org


def test_an_owner_shares_their_login_with_chosen_agents(monkeypatch):
    store = _SharingStore()
    app, _ = _agent_app(monkeypatch, store)
    client = TestClient(app)
    pid = client.post("/browser-profiles", json={"name": "Mine"}).json()["id"]

    shared = client.patch(f"/browser-profiles/{pid}", json={"shared_with_agents": ["agent-1", "sales_bot"]})
    assert shared.status_code == 200
    assert store.shared[uuid.UUID(pid)] == ["agent-1", "sales_bot"]
    # Unsharing is the empty list.
    assert client.patch(f"/browser-profiles/{pid}", json={"shared_with_agents": []}).status_code == 200
    assert store.shared[uuid.UUID(pid)] == []


def test_sharing_takes_only_agent_ids_and_at_most_64(monkeypatch):
    store = _SharingStore()
    app, _ = _agent_app(monkeypatch, store)
    client = TestClient(app)
    pid = client.post("/browser-profiles", json={"name": "Mine"}).json()["id"]
    for bad in (["*"], ["a b"], [""], [f"a{i}" for i in range(65)]):
        assert client.patch(f"/browser-profiles/{pid}", json={"shared_with_agents": bad}).status_code == 422
    assert store.shared == {}


def test_an_agents_own_login_is_never_shared(monkeypatch):
    store = _SharingStore()
    app, agent_sa = _agent_app(monkeypatch, store)
    store.agent_owned.add(agent_sa)
    client = TestClient(app)
    pid = client.post("/browser-profiles?owner=agent&agent_id=agent-1", json={"name": "X"}).json()["id"]
    refused = client.patch(
        f"/browser-profiles/{pid}?owner=agent&agent_id=agent-1", json={"shared_with_agents": ["agent-2"]},
    )
    assert refused.status_code == 422
    assert store.shared == {}


def test_another_member_cannot_share_my_login():
    store = _SharingStore()
    org = uuid.uuid4()
    mine = TestClient(_app(store, user_id=uuid.uuid4(), sa_id=None, org_id=org))
    pid = mine.post("/browser-profiles", json={"name": "Mine"}).json()["id"]
    theirs = TestClient(_app(store, user_id=uuid.uuid4(), sa_id=None, org_id=org))

    refused = theirs.patch(f"/browser-profiles/{pid}", json={"shared_with_agents": ["agent-1"]})
    assert refused.status_code == 404
    assert store.shared == {}


def _row(name):
    return BrowserProfileRow(uuid.uuid4(), name, "manual_vnc", ["x.com"], datetime.now(timezone.utc), None, True)


def _keyed_app(store, monkeypatch, *, bound_agent=None, operator=False, principals=None):
    app = _app(store)
    app.dependency_overrides[bp.get_current_tenant] = lambda: TenantContext(
        org_id=uuid.uuid4(), user_id=None, org_config={}, user_preferences={}, permissions=frozenset(),
        asset_root="/tmp", service_account_id=uuid.uuid4(), service_account_agent_id=bound_agent,
    )
    app.state.session_store = object()
    app.state.session_factory = object()

    async def is_operator(session_store, sa_id):
        return operator

    async def find(session_factory, org_id, agent_id):
        sa = (principals or {}).get(agent_id)
        return type("P", (), {"id": sa})() if sa else None

    monkeypatch.setattr(bp, "is_ops_chat_service_account", is_operator)
    monkeypatch.setattr(bp, "find_agent_principal", find)
    return TestClient(app)


def test_an_agent_key_sees_its_own_and_shared_logins_and_nothing_of_another_agent(monkeypatch):
    store = _SharingStore()
    sa_1, sa_2 = uuid.uuid4(), uuid.uuid4()
    own, lent, others = _row("Own"), _row("Lent"), _row("Other's")
    store.usable = {(sa_1, "agent-1"): [(own, "agent"), (lent, "shared")], (sa_2, "agent-2"): [(others, "agent")]}
    client = _keyed_app(store, monkeypatch, bound_agent="agent-1", principals={"agent-1": sa_1, "agent-2": sa_2})

    usable = client.get("/browser-profiles/usable")
    assert usable.status_code == 200
    assert [(p["name"], p["kind"]) for p in usable.json()] == [("Own", "agent"), ("Lent", "shared")]
    assert set(usable.json()[0]) == {"id", "name", "cookie_domains", "has_state", "kind", "last_used_at"}
    # A key bound to one agent never names another.
    assert [p["name"] for p in client.get("/browser-profiles/usable?agent_id=agent-2").json()] == ["Own", "Lent"]


def test_a_studio_chat_lists_what_the_agent_it_names_can_use(monkeypatch):
    store = _SharingStore()
    sa_2 = uuid.uuid4()
    store.usable = {(sa_2, "agent-2"): [(_row("Other's"), "agent")]}
    client = _keyed_app(store, monkeypatch, operator=True, principals={"agent-2": sa_2})
    assert [p["name"] for p in client.get("/browser-profiles/usable?agent_id=agent-2").json()] == ["Other's"]
    assert client.get("/browser-profiles/usable").status_code == 400


def test_anyone_else_is_refused_the_usable_list(monkeypatch):
    client = _keyed_app(_SharingStore(), monkeypatch)
    assert client.get("/browser-profiles/usable?agent_id=agent-1").status_code == 403


def test_only_an_org_admin_lists_the_shared_logins():
    store = _SharingStore()
    row = replace(_row("Lent"), shared_with_agents=["agent-1"], last_used_by="agent-1")
    owner = uuid.uuid4()
    store.in_org = [(row, owner, None)]

    def client(permissions):
        app = _app(store, user_id=uuid.uuid4(), sa_id=None)
        tenant = app.dependency_overrides[bp.get_current_tenant]

        async def _tenant():
            return replace(await tenant(), service_account_id=None, permissions=frozenset(permissions))

        app.dependency_overrides[bp.get_current_tenant] = _tenant
        return TestClient(app)

    assert client([]).get("/browser-profiles/shared").status_code == 403
    listed = client(["admin"]).get("/browser-profiles/shared")
    assert listed.status_code == 200
    [item] = listed.json()
    assert item == {
        "id": str(row.id), "name": "Lent", "cookie_domains": ["x.com"], "owner_user_id": str(owner),
        "owner_service_account_id": None, "shared_with_agents": ["agent-1"], "last_used_at": None,
        "last_used_by": "agent-1",
    }


def test_a_studio_chat_names_the_agent_as_every_other_route_resolves_it(monkeypatch):
    store = _SharingStore()
    sa_2 = uuid.uuid4()
    store.usable = {(sa_2, "agent-2"): [(_row("Other's"), "agent")]}
    client = _keyed_app(store, monkeypatch, operator=True, principals={"agent-2": sa_2})

    async def by_host(request):
        return "agent-2"

    monkeypatch.setattr(bp, "resolve_agent_id_soft", by_host)
    assert [p["name"] for p in client.get("/browser-profiles/usable").json()] == ["Other's"]


def test_a_rename_to_a_taken_name_is_409_and_shares_nothing(monkeypatch):
    from sqlalchemy.exc import IntegrityError

    class _Store(_SharingStore):
        async def update(self, profile_id, org_id, *, user_id, service_account_id, **changes):
            raise IntegrityError("UPDATE", {}, Exception("unique violation"))

    store = _Store()
    client = TestClient(_app(store))
    pid = client.post("/browser-profiles", json={"name": "Mine"}).json()["id"]
    refused = client.patch(f"/browser-profiles/{pid}", json={"name": "Taken", "shared_with_agents": ["agent-1"]})
    assert refused.status_code == 409
    assert store.shared == {}
