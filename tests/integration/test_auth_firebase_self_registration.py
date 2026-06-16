"""Integration tests for the BYO Firebase auth config + exchange endpoints.

Each test uses the real PostgreSQL container (the User table relies on
``JSONB`` columns) plus the real ``create_app`` factory. The Firebase
token verifier is monkeypatched so we don't need to mint real signed
tokens — every assertion focuses on the runtime auth rules, not on
verifier correctness which lives in `tests/test_firebase_auth_runtime.py`.
"""

from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from surogates.api import routes as routes_pkg  # noqa: F401  (registers routers)
from surogates.api.routes import auth as auth_routes
from surogates.db.models import User
from surogates.session.store import SessionStore
from surogates.storage.backend import create_backend
from surogates.tenant.credentials import CredentialVault

from .conftest import create_org


pytestmark = pytest.mark.asyncio(loop_scope="session")


TEST_PROJECT_ID = "test-project"


@pytest_asyncio.fixture(loop_scope="session")
async def auth_app(session_factory, redis_client, pg_url, redis_url):
    """FastAPI app wired against the real Postgres + Redis containers.

    The auth routes now resolve the tenant (org_id, project_id) per-request
    via ``agent_runtime_context_dep`` and resolve the per-project Firebase
    config via ``app.state.firebase_config_cache`` (which fronts ops's
    ``GET /api/projects/{id}/firebase-config``).  We wire both with test
    doubles driven by a mutable ``_runtime`` holder so each test can point
    the override at its freshly-created org and toggle Firebase on/off.
    """
    os.environ["SUROGATES_DB_URL"] = pg_url
    os.environ["SUROGATES_REDIS_URL"] = redis_url
    os.environ.setdefault("SUROGATES_ENCRYPTION_KEY", Fernet.generate_key().decode())

    from surogates.api.app import create_app
    from surogates.config import Settings
    from surogates.runtime import (
        FirebaseConfig,
        FirebaseConfigCache,
        agent_runtime_context_dep,
        build_agent_runtime_context,
    )

    application = create_app()
    application.state.session_factory = session_factory
    application.state.redis = redis_client
    application.state.session_store = SessionStore(
        session_factory, redis=redis_client,
    )
    application.state.settings = Settings()
    application.state.storage = create_backend(application.state.settings)
    application.state.credential_vault = CredentialVault(
        session_factory, Fernet.generate_key(),
    )

    # Mutable per-test holder consulted by both the runtime-context
    # override and the Firebase loader.  ``firebase`` is the dataclass
    # to serve for ``TEST_PROJECT_ID`` (or None to simulate a project
    # with no Firebase configured → the cache raises LookupError).
    holder: dict = {
        "org_id": "00000000-0000-0000-0000-000000000000",
        "project_id": TEST_PROJECT_ID,
        "firebase": None,
    }
    application.state._auth_test_holder = holder

    def _fixed_runtime_context():
        return build_agent_runtime_context({
            "agent_id": "default",
            "org_id": holder["org_id"],
            "project_id": holder["project_id"],
            "enabled": True,
            "version": 1,
            "storage_key_prefix": "test-project/default",
        })

    application.dependency_overrides[agent_runtime_context_dep] = (
        _fixed_runtime_context
    )

    async def _load_firebase(project_id: str) -> FirebaseConfig:
        fb = holder["firebase"]
        if fb is None or project_id != holder["project_id"]:
            raise LookupError(project_id)
        return fb

    application.state.firebase_config_cache = FirebaseConfigCache(
        loader=_load_firebase, ttl_seconds=0.0,
    )
    return application


@pytest_asyncio.fixture(loop_scope="session")
async def auth_client(auth_app):
    async with AsyncClient(
        transport=ASGITransport(app=auth_app), base_url="http://test",
    ) as client:
        yield client


def _set_firebase(
    auth_app, *, enabled: bool, project_id: str = "builder-firebase",
):
    """Configure the per-project Firebase config served by the cache.

    Self-registration is no longer a standalone toggle: a project either
    has a Firebase config row (self-registration enabled) or it doesn't
    (the cache raises ``LookupError`` and the route reports it disabled).
    ``enabled=False`` therefore clears the configured row.
    """
    from surogates.runtime import FirebaseConfig

    holder = auth_app.state._auth_test_holder
    if not enabled:
        holder["firebase"] = None
        return
    holder["firebase"] = FirebaseConfig(
        project_id=holder["project_id"],
        firebase_project_id=project_id,
        api_key="public-key",
        auth_domain="builder.firebaseapp.com",
        enabled_providers=("google", "password"),
    )


def _set_org(auth_app, org_id) -> None:
    """Point the runtime-context override at ``org_id`` for this test."""
    auth_app.state._auth_test_holder["org_id"] = str(org_id)


async def test_auth_config_hides_firebase_when_self_registration_disabled(
    auth_client, auth_app,
):
    _set_firebase(auth_app, enabled=False)

    response = await auth_client.get("/v1/auth/config")

    assert response.status_code == 200
    assert response.json() == {
        "self_registration_enabled": False,
        "firebase": None,
    }


async def test_auth_config_exposes_firebase_when_enabled(auth_client, auth_app):
    _set_firebase(auth_app, enabled=True)

    response = await auth_client.get("/v1/auth/config")

    assert response.status_code == 200
    body = response.json()
    assert body["self_registration_enabled"] is True
    firebase = body["firebase"]
    assert firebase["project_id"] == "builder-firebase"
    assert firebase["api_key"] == "public-key"
    assert firebase["auth_domain"] == "builder.firebaseapp.com"
    assert firebase["enabled_providers"] == ["google", "password"]


async def test_firebase_exchange_creates_user_when_enabled(
    auth_client, auth_app, session_factory, monkeypatch,
):
    org_id = await create_org(session_factory)
    _set_org(auth_app, org_id)
    _set_firebase(auth_app, enabled=True)

    async def fake_verify(token: str, project_id: str) -> dict:
        assert token == "firebase-token"
        assert project_id == "builder-firebase"
        return {
            "sub": "uid-123",
            "email": "new-user@example.com",
            "email_verified": True,
        }

    monkeypatch.setattr(auth_routes, "verify_firebase_id_token", fake_verify)

    response = await auth_client.post(
        "/v1/auth/firebase/exchange",
        json={"id_token": "firebase-token"},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["access_token"]
    assert payload["refresh_token"]

    async with session_factory() as session:
        user = await session.scalar(
            select(User).where(
                User.org_id == org_id, User.email == "new-user@example.com",
            )
        )
    assert user is not None
    assert user.auth_provider == "firebase:builder-firebase"
    assert user.external_id == "uid-123"


async def test_firebase_exchange_404_when_project_has_no_firebase(
    auth_client, auth_app, session_factory, monkeypatch,
):
    """A project with no Firebase config row rejects the exchange with 404.

    Self-registration is now gated solely by the per-project Firebase
    config existing: when it doesn't, the endpoint is simply unusable and
    no user is provisioned (token verification never even runs).
    """
    org_id = await create_org(session_factory)
    _set_org(auth_app, org_id)
    _set_firebase(auth_app, enabled=False)

    verify_called = False

    async def fake_verify(token: str, project_id: str) -> dict:
        nonlocal verify_called
        verify_called = True
        return {
            "sub": "uid-new",
            "email": "blocked@example.com",
            "email_verified": True,
        }

    monkeypatch.setattr(auth_routes, "verify_firebase_id_token", fake_verify)

    response = await auth_client.post(
        "/v1/auth/firebase/exchange",
        json={"id_token": "firebase-token"},
    )

    assert response.status_code == 404
    assert verify_called is False
    async with session_factory() as session:
        user = await session.scalar(
            select(User).where(
                User.org_id == org_id, User.email == "blocked@example.com",
            )
        )
    assert user is None


async def test_firebase_exchange_does_not_link_manual_database_user(
    auth_client, auth_app, session_factory, monkeypatch,
):
    """A manual ``database`` user must not be silently linked to Firebase
    just because their email happens to match an incoming token — even when
    Firebase self-registration is enabled for the project."""
    org_id = await create_org(session_factory)
    _set_org(auth_app, org_id)
    _set_firebase(auth_app, enabled=True)

    async with session_factory() as session:
        session.add(User(
            id=uuid.uuid4(),
            org_id=org_id,
            email="manual@example.com",
            display_name="Manual User",
            auth_provider="database",
            password_hash="hash",
        ))
        await session.commit()

    async def fake_verify(token: str, project_id: str) -> dict:
        return {
            "sub": "uid-manual",
            "email": "manual@example.com",
            "email_verified": True,
        }

    monkeypatch.setattr(auth_routes, "verify_firebase_id_token", fake_verify)

    response = await auth_client.post(
        "/v1/auth/firebase/exchange",
        json={"id_token": "firebase-token"},
    )

    # A brand-new firebase-provider row is provisioned (self-registration),
    # but the pre-existing manual ``database`` row is left untouched: no
    # silent link of a password account to Firebase.
    assert response.status_code == 200, response.text
    async with session_factory() as session:
        manual = await session.scalar(
            select(User).where(
                User.org_id == org_id,
                User.email == "manual@example.com",
                User.auth_provider == "database",
            )
        )
    assert manual is not None
    assert manual.external_id is None


async def test_cross_project_uid_collision_resolves_to_different_users(
    auth_client, auth_app, session_factory, monkeypatch,
):
    """Two BYO Firebase projects mint the same UID — must NOT collide.

    Demonstrates the value of namespacing ``auth_provider`` with the
    Firebase project id: Alice's row uses ``firebase:project-a`` and
    Bob's exchange against ``firebase:project-b`` creates a separate row
    even though both share ``external_id="uid-shared"``.
    """
    org_id = await create_org(session_factory)
    _set_org(auth_app, org_id)

    # Pre-seed Alice — pretends she self-registered against project A.
    async with session_factory() as session:
        session.add(User(
            id=uuid.uuid4(),
            org_id=org_id,
            email="alice@example.com",
            display_name="Alice from A",
            auth_provider="firebase:project-a",
            external_id="uid-shared",
        ))
        await session.commit()

    # Now exchange a token from project B with the same UID + different email.
    _set_firebase(auth_app, enabled=True, project_id="project-b")

    async def fake_verify(token: str, project_id: str) -> dict:
        assert project_id == "project-b"
        return {
            "sub": "uid-shared",
            "email": "bob@example.com",
            "email_verified": True,
        }

    monkeypatch.setattr(auth_routes, "verify_firebase_id_token", fake_verify)

    response = await auth_client.post(
        "/v1/auth/firebase/exchange",
        json={"id_token": "firebase-token"},
    )
    assert response.status_code == 200, response.text

    async with session_factory() as session:
        rows = (await session.execute(
            select(User).where(
                User.org_id == org_id, User.external_id == "uid-shared",
            )
        )).scalars().all()
    providers = sorted(u.auth_provider for u in rows)
    assert providers == ["firebase:project-a", "firebase:project-b"], providers
    emails = sorted(u.email for u in rows)
    assert emails == ["alice@example.com", "bob@example.com"]
