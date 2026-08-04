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


AGENT_ID = "test-agent"
OTHER_AGENT_ID = "other-agent"


class FakeRuntimeCache:
    """Minimal stand-in for ``app.state.runtime_config_cache``.

    Returns a valid runtime-config payload for the agents it knows and
    raises ``LookupError`` for everything else, mirroring the real
    cache's contract — which is what lets ``agent_runtime_context_dep``
    resolve an agent without a live management plane.
    """

    def __init__(self, *agent_ids: str) -> None:
        self._agent_ids = frozenset(agent_ids)

    async def get(self, agent_id: str) -> dict:
        if agent_id not in self._agent_ids:
            raise LookupError(agent_id)
        return {
            "agent_id": agent_id,
            "org_id": "00000000-0000-0000-0000-000000000000",
            "project_id": "test-project",
            "enabled": True,
            "version": 1,
            "storage_key_prefix": "",
        }


def inbox_path(
    suffix: str = "",
    *,
    agent_id: str | None = AGENT_ID,
    query: str = "",
) -> str:
    """A ``/v1/inbox`` URL that names the agent whose inbox it is.

    Every real client names one: the SPA through its host subdomain in
    production, through an ``agent_id`` the dev proxy injects otherwise.
    ``agent_id=None`` builds the nameless URL the API rejects.
    """
    parts = [
        part
        for part in (query, f"agent_id={agent_id}" if agent_id else "")
        if part
    ]
    return f"/v1/inbox{suffix}" + (f"?{'&'.join(parts)}" if parts else "")


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
    application.state.runtime_config_cache = FakeRuntimeCache(
        AGENT_ID, OTHER_AGENT_ID,
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
