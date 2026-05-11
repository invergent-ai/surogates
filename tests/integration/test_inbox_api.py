"""API tests for /v1/inbox."""

from __future__ import annotations

import os
import uuid
from uuid import UUID

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient

from surogates.session.events import EventType
from surogates.session.store import SessionStore
from surogates.tenant.auth.jwt import create_access_token
from surogates.tenant.credentials import CredentialVault

from .conftest import create_org, create_user, issue_service_account_token

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest_asyncio.fixture(loop_scope="session")
async def app(session_factory, redis_client, pg_url, redis_url):
    os.environ["SUROGATES_DB_URL"] = pg_url
    os.environ["SUROGATES_REDIS_URL"] = redis_url

    from surogates.api.app import create_app
    from surogates.config import Settings
    from surogates.storage.backend import create_backend

    application = create_app()
    application.state.session_factory = session_factory
    application.state.redis = redis_client
    application.state.session_store = SessionStore(
        session_factory,
        redis=redis_client,
    )
    application.state.settings = Settings()
    application.state.storage = create_backend(application.state.settings)
    application.state.credential_vault = CredentialVault(
        session_factory,
        Fernet.generate_key(),
    )
    return application


@pytest_asyncio.fixture(loop_scope="session")
async def client(app):
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as c:
        yield c


async def _create_user_token_session(session_factory, session_store):
    org_id = await create_org(session_factory)
    user_id = uuid.uuid4()
    await create_user(session_factory, org_id, user_id=user_id)
    token = create_access_token(
        org_id,
        user_id,
        {"sessions:read", "sessions:write"},
    )
    session = await session_store.create_session(
        user_id=user_id,
        org_id=org_id,
        agent_id="test-agent",
    )
    return org_id, user_id, token, session


async def _create_service_account_token(session_factory) -> tuple[UUID, str]:
    org_id = await create_org(session_factory)
    issued = await issue_service_account_token(session_factory, org_id)
    return org_id, issued.token


async def test_list_inbox_returns_only_callers_items(
    client,
    session_factory,
    session_store,
):
    _, _, token, session = await _create_user_token_session(
        session_factory,
        session_store,
    )
    await session_store.emit_event(
        session.id,
        EventType.INBOX_TASK_COMPLETE,
        {
            "outcome": "success",
            "duration_seconds": 1,
            "summary": "All done.",
            "session_title": "Task complete",
        },
    )

    response = await client.get(
        "/v1/inbox",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["kind"] == "task_complete"
    assert body["items"][0]["session_id"] == str(session.id)


async def test_list_inbox_rejects_service_account(
    client,
    session_factory,
):
    _, token = await _create_service_account_token(session_factory)

    response = await client.get(
        "/v1/inbox",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 403
