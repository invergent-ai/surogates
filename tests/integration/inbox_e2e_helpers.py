"""Shared helpers for inbox end-to-end integration tests."""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from cryptography.fernet import Fernet

from surogates.session.store import SessionStore
from surogates.tenant.auth.jwt import create_access_token
from surogates.tenant.credentials import CredentialVault


@dataclass(frozen=True)
class UserSession:
    org_id: UUID
    user_id: UUID
    token: str
    session: Any

    @property
    def auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}


@dataclass(frozen=True)
class StubTenant:
    org_id: UUID
    user_id: UUID


def build_inbox_test_app(session_factory, redis_client, pg_url, redis_url):
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


async def create_user_token_session(
    session_factory,
    session_store,
    *,
    agent_id: str = "test-agent",
    config: dict[str, Any] | None = None,
) -> UserSession:
    from .conftest import create_org, create_user

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
        agent_id=agent_id,
        config=config,
    )
    return UserSession(
        org_id=org_id,
        user_id=user_id,
        token=token,
        session=session,
    )
