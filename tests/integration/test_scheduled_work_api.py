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
    os.environ["SUROGATES_AGENT_ID"] = _AGENT_ID

    from surogates.api.app import create_app
    from surogates.config import Settings

    application = create_app()
    application.state.session_factory = session_factory
    application.state.redis = redis_client
    application.state.session_store = SessionStore(session_factory)
    application.state.settings = Settings()
    application.state.settings.agent_id = _AGENT_ID
    application.state.storage = LocalBackend(
        base_path=str(tmp_path_factory.mktemp("scheduled-work-api-storage"))
    )
    application.state.credential_vault = CredentialVault(
        session_factory, Fernet.generate_key()
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
    dynamic = await store.create_dynamic_loop(
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
    assert by_id[str(fixed.id)]["kind"] == "cron"
    assert by_id[str(fixed.id)]["created_from_session_id"] == str(origin.id)
    assert by_id[str(dynamic.id)]["kind"] == "dynamic_loop"
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
    with pytest.raises(KeyError):
        await store.get(schedule.id)
