"""Integration tests for the Firebase email collision guard on admin user create."""

from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from surogates.db.models import User
from surogates.session.store import SessionStore
from surogates.storage.backend import create_backend
from surogates.tenant.auth.jwt import create_access_token
from surogates.tenant.credentials import CredentialVault

from .conftest import create_org, create_user


pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest_asyncio.fixture(loop_scope="session")
async def admin_app(session_factory, redis_client, pg_url, redis_url):
    os.environ["SUROGATES_DB_URL"] = pg_url
    os.environ["SUROGATES_REDIS_URL"] = redis_url
    os.environ.setdefault("SUROGATES_ENCRYPTION_KEY", Fernet.generate_key().decode())

    from surogates.api.app import create_app
    from surogates.config import Settings

    app = create_app()
    app.state.session_factory = session_factory
    app.state.redis = redis_client
    app.state.session_store = SessionStore(session_factory, redis=redis_client)
    app.state.settings = Settings()
    app.state.storage = create_backend(app.state.settings)
    app.state.credential_vault = CredentialVault(
        session_factory, Fernet.generate_key(),
    )
    return app


@pytest_asyncio.fixture(loop_scope="session")
async def admin_client(admin_app):
    async with AsyncClient(
        transport=ASGITransport(app=admin_app), base_url="http://test",
    ) as client:
        yield client


async def _admin_token(session_factory) -> tuple[uuid.UUID, dict[str, str]]:
    """Create an org + admin user and return (org_id, auth headers)."""
    org_id = await create_org(session_factory)
    user_id = uuid.uuid4()
    await create_user(session_factory, org_id, user_id=user_id, email=f"admin-{user_id}@test")
    token = create_access_token(
        org_id, user_id, {"sessions:read", "tools:read", "admin"},
    )
    return org_id, {"Authorization": f"Bearer {token}"}


async def test_manual_create_refuses_existing_firebase_email(
    admin_client, session_factory,
):
    org_id, headers = await _admin_token(session_factory)
    async with session_factory() as session:
        session.add(User(
            id=uuid.uuid4(),
            org_id=org_id,
            email="dup@example.com",
            display_name="Existing Firebase User",
            auth_provider="firebase:builder-firebase",
            external_id="uid-existing",
        ))
        await session.commit()

    response = await admin_client.post(
        f"/v1/admin/orgs/{org_id}/users",
        json={
            "email": "dup@example.com",
            "display_name": "Manual",
            "auth_provider": "database",
            "password": "hunter2",
        },
        headers=headers,
    )
    assert response.status_code == 409, response.text
    body = response.json()
    assert "already in use" in body["detail"].lower()

    # No escape hatch: ``uq_users_org_lower_email`` allows exactly one
    # account per (org, email), so the existing row stays alone.
    async with session_factory() as session:
        rows = (await session.execute(
            select(User).where(
                User.org_id == org_id, User.email == "dup@example.com",
            )
        )).scalars().all()
    assert len(rows) == 1


async def test_manual_create_succeeds_when_no_firebase_collision(
    admin_client, session_factory,
):
    """Default behaviour: when no Firebase user shares the email, manual
    creation must continue to work without ``force``."""
    org_id, headers = await _admin_token(session_factory)

    response = await admin_client.post(
        f"/v1/admin/orgs/{org_id}/users",
        json={
            "email": "fresh@example.com",
            "display_name": "Fresh",
            "auth_provider": "database",
            "password": "hunter2",
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text


async def test_manual_create_blocks_on_any_provider(
    admin_client, session_factory,
):
    """The collision guard is provider-agnostic: email is the org-scoped
    login key, so a second ``database`` row on the same address is refused
    with a 409 rather than 500-ing on the unique index."""
    org_id, headers = await _admin_token(session_factory)
    async with session_factory() as session:
        session.add(User(
            id=uuid.uuid4(),
            org_id=org_id,
            email="manual@example.com",
            display_name="Existing Manual",
            auth_provider="database",
            password_hash="hash",
        ))
        await session.commit()

    response = await admin_client.post(
        f"/v1/admin/orgs/{org_id}/users",
        json={
            "email": "manual@example.com",
            "display_name": "Another Manual",
            "auth_provider": "database",
            "password": "hunter2",
        },
        headers=headers,
    )
    assert response.status_code == 409, response.text
    assert "already in use" in response.json()["detail"].lower()
