from __future__ import annotations

import os
import uuid
from uuid import UUID

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient

from surogates.scheduled.schedule import parse_dynamic_loop_schedule, parse_schedule
from surogates.scheduled.store import ScheduledSessionStore
from surogates.session.store import SessionStore
from surogates.storage.backend import LocalBackend
from surogates.tenant.auth.jwt import create_access_token
from surogates.tenant.credentials import CredentialVault

from .conftest import create_org, create_user

pytestmark = pytest.mark.asyncio(loop_scope="session")

_AGENT_ID = "test-agent"


@pytest_asyncio.fixture(loop_scope="session")
async def app(session_factory, redis_client, pg_url, redis_url, tmp_path_factory):
    os.environ["SUROGATES_DB_URL"] = pg_url
    os.environ["SUROGATES_REDIS_URL"] = redis_url

    from surogates.api.app import create_app
    from surogates.config import Settings
    from surogates.runtime import (
        agent_runtime_context_dep,
        build_agent_runtime_context,
    )

    application = create_app()
    application.state.session_factory = session_factory
    application.state.redis = redis_client
    application.state.session_store = SessionStore(session_factory)
    application.state.settings = Settings()
    application.state.storage = LocalBackend(
        base_path=str(tmp_path_factory.mktemp("scheduled-work-api-storage"))
    )
    application.state.credential_vault = CredentialVault(
        session_factory, Fernet.generate_key()
    )

    # Scheduled-work routes scope schedules to the per-request agent
    # context (shared-runtime); ``agent_id`` no longer lives on
    # ``Settings``.  Pin it to the value our schedules are created with.
    def _fixed_runtime_context():
        return build_agent_runtime_context({
            "agent_id": _AGENT_ID,
            "org_id": "00000000-0000-0000-0000-000000000000",
            "project_id": "test-project",
            "enabled": True,
            "version": 1,
            "storage_key_prefix": "",
        })

    application.dependency_overrides[agent_runtime_context_dep] = (
        _fixed_runtime_context
    )
    return application


@pytest_asyncio.fixture(loop_scope="session")
async def client(app):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


async def _tenant(session_factory) -> tuple[UUID, UUID, str]:
    org_id = await create_org(session_factory)
    user_id = uuid.uuid4()
    await create_user(session_factory, org_id, user_id=user_id)
    token = create_access_token(
        org_id, user_id, {"sessions:read", "sessions:write", "tools:read"}
    )
    return org_id, user_id, token


async def test_list_scheduled_work_returns_user_owned_agent_schedules(
    client: AsyncClient, session_factory, session_store
):
    org_id, user_id, token = await _tenant(session_factory)
    other_user_id = await create_user(session_factory, org_id)
    origin = await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id=_AGENT_ID
    )
    last_run = await session_store.create_session(
        user_id=user_id,
        org_id=org_id,
        agent_id=_AGENT_ID,
        parent_id=origin.id,
        channel="scheduled",
    )
    store = ScheduledSessionStore(session_factory)
    fixed = await store.create_loop(
        org_id=org_id,
        user_id=user_id,
        agent_id=_AGENT_ID,
        prompt="check deploy",
        schedule=parse_schedule("5m"),
        created_from_session_id=origin.id,
    )
    dynamic = await store.create_loop(
        org_id=org_id,
        user_id=user_id,
        agent_id=_AGENT_ID,
        prompt="watch CI",
        schedule=parse_dynamic_loop_schedule(),
        created_from_session_id=origin.id,
    )
    await store.mark_run_created(dynamic, session_id=last_run.id)
    await store.create_loop(
        org_id=org_id,
        user_id=other_user_id,
        agent_id=_AGENT_ID,
        prompt="other user",
        schedule=parse_schedule("5m"),
        created_from_session_id=None,
    )
    await store.create_loop(
        org_id=org_id,
        user_id=user_id,
        agent_id="other-agent",
        prompt="other agent",
        schedule=parse_schedule("5m"),
        created_from_session_id=None,
    )

    response = await client.get(
        "/v1/scheduled-work",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 2
    by_id = {item["id"]: item for item in data["items"]}
    assert set(by_id) == {str(fixed.id), str(dynamic.id)}
    assert by_id[str(fixed.id)]["kind"] == "interval"
    assert by_id[str(fixed.id)]["created_from_session_id"] == str(origin.id)
    assert by_id[str(dynamic.id)]["kind"] == "dynamic"
    assert by_id[str(dynamic.id)]["last_session_id"] == str(last_run.id)


async def test_run_now_and_cancel_scheduled_work_are_user_scoped(
    client: AsyncClient, session_factory
):
    org_id, user_id, token = await _tenant(session_factory)
    store = ScheduledSessionStore(session_factory)
    schedule = await store.create_loop(
        org_id=org_id,
        user_id=user_id,
        agent_id=_AGENT_ID,
        prompt="check price",
        schedule=parse_schedule("10m"),
        created_from_session_id=None,
    )

    run_response = await client.post(
        f"/v1/scheduled-work/{schedule.id}/run-now",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert run_response.status_code == 200
    assert run_response.json() == {"id": str(schedule.id), "queued": True}
    updated = await store.get(schedule.id)
    assert updated.next_run_at is not None
    assert updated.next_run_at <= updated.updated_at

    cancel_response = await client.delete(
        f"/v1/scheduled-work/{schedule.id}",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert cancel_response.status_code == 204
    cancelled = await store.get(schedule.id)
    assert (cancelled.status, cancelled.ended_reason, cancelled.ended_by) == (
        "cancelled", "cancelled", f"user:{user_id}",
    )
    assert cancelled.next_run_at is None

    again = await client.delete(
        f"/v1/scheduled-work/{schedule.id}", headers={"Authorization": f"Bearer {token}"},
    )
    assert again.status_code == 409
    assert "cancelled" in again.json()["detail"]
    assert (await client.delete(
        f"/v1/scheduled-work/{uuid.uuid4()}", headers={"Authorization": f"Bearer {token}"},
    )).status_code == 404


async def test_owner_me_narrows_an_ops_chat_account_to_its_own(client: AsyncClient, session_factory):
    org_id, user_id, _ = await _tenant(session_factory)
    sa_id, auth = await _ops_chat(session_factory, org_id)
    store = ScheduledSessionStore(session_factory)
    mine = await store.create_loop(
        org_id=org_id, service_account_id=sa_id, agent_id=_AGENT_ID, prompt="mine",
        schedule=parse_schedule("10m"), created_from_session_id=None,
    )
    theirs = await store.create_loop(
        org_id=org_id, user_id=user_id, agent_id=_AGENT_ID, prompt="theirs",
        schedule=parse_schedule("10m"), created_from_session_id=None,
    )

    every = {i["id"] for i in (await client.get("/v1/scheduled-work", headers=auth)).json()["items"]}
    assert {str(mine.id), str(theirs.id)} <= every
    own = {i["id"] for i in (await client.get("/v1/scheduled-work?owner=me", headers=auth)).json()["items"]}
    assert own == {str(mine.id)}

    other = f"/v1/scheduled-work/{theirs.id}?owner=me"
    assert (await client.get(other, headers=auth)).status_code == 404
    assert (await client.post(f"/v1/scheduled-work/{theirs.id}/pause?owner=me", headers=auth)).status_code == 404
    assert (await client.delete(other, headers=auth)).status_code == 404
    assert (await store.get(theirs.id)).status == "active"
    assert (await client.post(f"/v1/scheduled-work/{mine.id}/pause?owner=me", headers=auth)).status_code == 200


async def test_scheduled_work_can_be_read_changed_paused_and_resumed(
    client: AsyncClient, session_factory
):
    org_id, user_id, token = await _tenant(session_factory)
    auth = {"Authorization": f"Bearer {token}"}
    store = ScheduledSessionStore(session_factory)
    schedule = await store.create_loop(
        org_id=org_id, user_id=user_id, agent_id=_AGENT_ID, prompt="check price",
        schedule=parse_schedule("10m"), created_from_session_id=None,
    )
    base = f"/v1/scheduled-work/{schedule.id}"

    got = await client.get(base, headers=auth)
    assert got.status_code == 200
    assert (got.json()["kind"], got.json()["consecutive_failures"]) == ("interval", 0)

    changed = await client.patch(
        base, headers=auth,
        json={"prompt": "check the price twice", "cadence": {
            "kind": "calendar", "days": ["mon", "fri"], "times": ["09:00"], "tz": "Europe/Bucharest",
        }},
    )
    assert changed.status_code == 200, changed.text
    body = changed.json()
    assert body["prompt"] == "check the price twice"
    assert body["kind"] == "calendar"
    assert body["timezone"] == "Europe/Bucharest"

    assert (await client.patch(base, headers=auth, json={"cadence": {"kind": "cron", "cron": "daily"}})).status_code == 422
    assert (await client.patch(base, headers=auth, json={"cadence": {"kind": "interval", "seconds": 30}})).status_code == 422

    assert (await client.post(f"{base}/pause", headers=auth)).status_code == 200
    assert (await client.post(f"{base}/pause", headers=auth)).status_code == 409
    paused = await store.get(schedule.id)
    assert (paused.status, paused.next_run_at) == ("paused", None)

    assert (await client.post(f"{base}/resume", headers=auth)).status_code == 200
    resumed = await store.get(schedule.id)
    assert resumed.status == "active"
    assert resumed.next_run_at is not None

    listed = await client.get("/v1/scheduled-work?status=all", headers=auth)
    assert listed.status_code == 200
    assert str(schedule.id) in {item["id"] for item in listed.json()["items"]}


async def test_another_users_scheduled_work_is_not_found(client: AsyncClient, session_factory):
    org_id, owner_id, _ = await _tenant(session_factory)
    other_id = uuid.uuid4()
    await create_user(session_factory, org_id, user_id=other_id)
    other = create_access_token(org_id, other_id, {"sessions:read", "sessions:write", "tools:read"})
    auth = {"Authorization": f"Bearer {other}"}
    schedule = await ScheduledSessionStore(session_factory).create_loop(
        org_id=org_id, user_id=owner_id, agent_id=_AGENT_ID, prompt="check price",
        schedule=parse_schedule("10m"), created_from_session_id=None,
    )
    base = f"/v1/scheduled-work/{schedule.id}"
    assert (await client.get(base, headers=auth)).status_code == 404
    assert (await client.patch(base, headers=auth, json={"prompt": "x"})).status_code == 404
    assert (await client.post(f"{base}/pause", headers=auth)).status_code == 404
    assert (await client.delete(base, headers=auth)).status_code == 404


async def _ops_chat(session_factory, org_id: UUID):
    from surogates.tenant.auth.jwt import create_service_account_session_token
    from surogates.tenant.auth.service_account import ServiceAccountStore

    account = await ServiceAccountStore(session_factory).create(org_id=org_id, name=f"ops-chat-{uuid.uuid4().hex[:8]}")
    token = create_service_account_session_token(org_id, account.id, uuid.uuid4())
    return account.id, {"Authorization": f"Bearer {token}"}


async def _profiles(app, session_factory):
    from surogates.browser.profiles import BrowserProfileStore

    app.state.browser_profile_store = BrowserProfileStore(session_factory, Fernet.generate_key())
    return app.state.browser_profile_store


async def test_create_preview_and_list_runs(client: AsyncClient, session_factory, session_store):
    org_id, user_id, token = await _tenant(session_factory)
    auth = {"Authorization": f"Bearer {token}"}
    cadence = {"kind": "calendar", "days": ["mon", 2], "times": ["09:00"], "tz": "Europe/Bucharest"}

    preview = await client.post("/v1/scheduled-work/preview", headers=auth, json={"cadence": cadence})
    assert preview.status_code == 200, preview.text
    assert preview.json()["display"].startswith("Mon, Wed at 09:00")
    assert len(preview.json()["next_fires"]) == 3
    bad = await client.post("/v1/scheduled-work/preview", headers=auth,
                            json={"cadence": {"kind": "cron", "cron": "nope"}})
    assert bad.status_code == 422

    made = await client.post("/v1/scheduled-work", headers=auth, json={
        "prompt": "post the weekly digest", "cadence": cadence,
        # Only an operator's account may say whom the work is for.
        "context": {"ops": {"user_id": 1}},
    })
    assert made.status_code == 201, made.text
    item = made.json()
    assert (item["kind"], item["source"], item["owner_user_id"]) == ("calendar", "api", str(user_id))
    assert item["display"] == item["schedule_display"]
    assert len(item["next_fires"]) == 3
    assert item["runs_logged_in_as"] is None
    row = await ScheduledSessionStore(session_factory).get(UUID(item["id"]))
    assert row.context == {}

    run = await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id=_AGENT_ID, channel="scheduled",
        config={"scheduled_session_id": item["id"]},
    )
    runs = await client.get(f"/v1/scheduled-work/{item['id']}/runs", headers=auth)
    assert runs.status_code == 200
    assert [r["id"] for r in runs.json()["items"]] == [str(run.id)]

    live = await client.get("/v1/scheduled-work?status=live", headers=auth)
    assert item["id"] in {i["id"] for i in live.json()["items"]}


async def test_operator_creates_with_context_and_the_agents_login(app, client: AsyncClient, session_factory):
    from surogates.tenant.auth.service_account import KIND_AGENT_PRINCIPAL, ServiceAccountStore

    org_id, user_id, token = await _tenant(session_factory)
    sa_id, auth = await _ops_chat(session_factory, org_id)
    agent = await ServiceAccountStore(session_factory).create(
        org_id=org_id, name=f"agent-{uuid.uuid4().hex[:6]}", agent_id=_AGENT_ID, kind=KIND_AGENT_PRINCIPAL,
    )
    profiles = await _profiles(app, session_factory)
    agents_login = await profiles.create(org_id, user_id=None, service_account_id=agent.id, name="LinkedIn")
    someone_elses = await profiles.create(org_id, user_id=user_id, service_account_id=None, name="Mine")

    body = {
        "prompt": "check the inbox", "name": "Inbox",
        "cadence": {"kind": "interval", "seconds": 3600},
        "context": {"ops": {"user_id": 7, "project_id": "p"}, "operator": {"name": "Ana"}, "browser": {"profile_id": "x"}},
    }
    refused = await client.post("/v1/scheduled-work", headers=auth,
                                json={**body, "browser_profile_id": str(someone_elses.id)})
    assert refused.status_code == 422

    made = await client.post("/v1/scheduled-work", headers=auth,
                             json={**body, "browser_profile_id": str(agents_login.id)})
    assert made.status_code == 201, made.text
    item = made.json()
    assert (item["source"], item["owner_service_account_id"]) == ("studio", str(sa_id))
    assert item["owner_service_account_name"].startswith("ops-chat-")
    assert item["browser_profile_id"] == str(agents_login.id)
    assert item["runs_logged_in_as"] == {
        "profile_id": str(agents_login.id), "name": "LinkedIn", "owner": "agent", "missing": True,
    }
    row = await ScheduledSessionStore(session_factory).get(UUID(item["id"]))
    assert row.context == {
        "ops": {"user_id": 7, "project_id": "p"}, "operator": {"name": "Ana"},
        "browser": {"profile_id": str(agents_login.id)},
    }

    logged_out = await client.patch(f"/v1/scheduled-work/{item['id']}", headers=auth,
                                    json={"browser_profile_id": None, "max_run_seconds": 0})
    assert logged_out.status_code == 200, logged_out.text
    assert (logged_out.json()["runs_logged_in_as"], logged_out.json()["max_run_seconds"]) == (None, None)
    assert (await client.patch(f"/v1/scheduled-work/{item['id']}", headers=auth,
                               json={"max_run_seconds": 5})).status_code == 422

    cancelled = await client.delete(f"/v1/scheduled-work/{item['id']}?ended_by=ops_user:ana", headers=auth)
    assert cancelled.status_code == 204
    assert (await ScheduledSessionStore(session_factory).get(UUID(item["id"]))).ended_by == "ops_user:ana"


async def test_cancel_stops_the_running_run_at_once(client: AsyncClient, session_factory, session_store):
    org_id, user_id, token = await _tenant(session_factory)
    auth = {"Authorization": f"Bearer {token}"}
    store = ScheduledSessionStore(session_factory)
    schedule = await store.create_loop(
        org_id=org_id, user_id=user_id, agent_id=_AGENT_ID, prompt="watch",
        schedule=parse_schedule("10m"), created_from_session_id=None,
    )
    run = await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id=_AGENT_ID, channel="scheduled",
        config={"scheduled_session_id": str(schedule.id)},
    )
    await store.mark_run_created(schedule, session_id=run.id)
    # Someone else's name is not the caller's to give.
    assert (await client.delete(f"/v1/scheduled-work/{schedule.id}?ended_by=ops_user:ana", headers=auth)).status_code == 204
    assert (await store.get(schedule.id)).ended_by == f"user:{user_id}"
    assert (await session_store.get_session(run.id)).status == "paused"


async def test_a_keel_cases_schedules_are_hidden_from_all_but_its_own_session(
    client: AsyncClient, session_factory, session_store,
):
    from surogates.tenant.auth.jwt import create_service_account_session_token
    from surogates.tenant.auth.service_account import ServiceAccountStore

    org_id = await create_org(session_factory)
    _, operator = await _ops_chat(session_factory, org_id)
    keel = await ServiceAccountStore(session_factory).create(org_id=org_id, name=f"keel-{uuid.uuid4().hex[:6]}")
    case_session = await session_store.create_session(
        user_id=None, service_account_id=keel.id, org_id=org_id, agent_id=_AGENT_ID,
        config={"memory_boundary": "case:42"},
    )
    own = {"Authorization": "Bearer " + create_service_account_session_token(org_id, keel.id, case_session.id)}
    store = ScheduledSessionStore(session_factory)
    case_schedule = await store.create_loop(
        org_id=org_id, service_account_id=keel.id, agent_id=_AGENT_ID, prompt="chase the documents",
        schedule=parse_schedule("1h"), created_from_session_id=case_session.id,
    )
    plain = await store.create_loop(
        org_id=org_id, service_account_id=keel.id, agent_id=_AGENT_ID, prompt="weekly digest",
        schedule=parse_schedule("1h"), created_from_session_id=None,
    )

    listed = await client.get("/v1/scheduled-work?status=all", headers=operator)
    assert {i["id"] for i in listed.json()["items"]} == {str(plain.id)}
    base = f"/v1/scheduled-work/{case_schedule.id}"
    for method, path, body in [
        ("GET", base, None), ("GET", f"{base}/runs", None), ("PATCH", base, {"prompt": "x"}),
        ("POST", f"{base}/pause", None), ("POST", f"{base}/resume", None),
        ("POST", f"{base}/run-now", None), ("DELETE", base, None),
    ]:
        answer = await client.request(method, path, headers=operator, json=body)
        assert answer.status_code == 404, (method, path, answer.text)
    assert (await store.get(case_schedule.id)).status == "active"

    assert (await client.get(base, headers=own)).status_code == 200
    mine = await client.get("/v1/scheduled-work", headers=own)
    assert str(case_schedule.id) in {i["id"] for i in mine.json()["items"]}
