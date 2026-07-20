"""Web-channel single-session enforcement.

With the agent's "multi session" capability off, ``POST /v1/sessions``
returns the user's newest reusable web session (HTTP 200) instead of
creating another; a user with no reusable session still gets a fresh
one.  The api channel and the capability-on default keep today's
always-create behavior.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import Response

from surogates.api.routes import sessions as sessions_route
from surogates.config import Settings
from surogates.tenant.context import TenantContext

pytestmark = pytest.mark.asyncio


class _Storage:
    def __init__(self) -> None:
        self.created_buckets: list[str] = []

    async def create_bucket(self, bucket: str) -> None:
        self.created_buckets.append(bucket)

    def resolve_workspace_path(self, bucket: str, session_id: UUID | str) -> str:
        return f"/bucket-root/{bucket}/{session_id}"


class _Store:
    def __init__(self, *, reusable=None) -> None:
        self.created: list[dict] = []
        self.reusable = reusable
        self.reusable_lookups: list[dict] = []

    async def get_reusable_channel_session(self, **kwargs):
        self.reusable_lookups.append(kwargs)
        return self.reusable

    async def create_session(self, **kwargs):
        self.created.append(kwargs)
        return SimpleNamespace(
            id=kwargs["session_id"],
            org_id=kwargs["org_id"],
            agent_id=kwargs["agent_id"],
            status="active",
            channel=kwargs["channel"],
            model=kwargs["model"],
            config=kwargs["config"],
        )

    async def emit_event(self, session_id, event_type, data):
        return 1

    async def update_session_status(self, session_id, status):
        return None


def _runtime(agent_id: str, org_id: UUID, *, multi_session: bool):
    from surogates.runtime import build_agent_runtime_context

    return build_agent_runtime_context(
        {
            "agent_id": agent_id,
            "org_id": str(org_id),
            "project_id": "test-project",
            "enabled": True,
            "version": 1,
            "storage_key_prefix": "",
            "multi_session": multi_session,
        }
    )


def _tenant(org_id: UUID, user_id: UUID | None) -> TenantContext:
    return TenantContext(
        org_id=org_id,
        user_id=user_id,
        org_config={},
        user_preferences={},
        permissions=frozenset({"sessions:read", "sessions:write"}),
        asset_root="/tmp/assets",
        service_account_id=None if user_id is not None else uuid4(),
    )


def _request(store: _Store, storage: _Storage):
    settings = Settings()
    settings.llm.model = "gpt-test"
    settings.storage.bucket = "ops-agent-bucket"
    return SimpleNamespace(
        url=SimpleNamespace(path="/v1/sessions"),
        app=SimpleNamespace(
            state=SimpleNamespace(
                settings=settings,
                session_store=store,
                storage=storage,
                redis=None,
            ),
        ),
    )


async def test_single_session_returns_existing_web_session_with_200():
    org_id, user_id = uuid4(), uuid4()
    existing = SimpleNamespace(
        id=uuid4(), org_id=org_id, agent_id="support-bot",
        status="active", channel="web", model="gpt-test", config={},
    )
    store = _Store(reusable=existing)
    response = Response()

    session = await sessions_route.create_session(
        sessions_route.CreateSessionRequest(),
        _request(store, _Storage()),
        response,
        _tenant(org_id, user_id),
        _runtime("support-bot", org_id, multi_session=False),
    )

    assert session.id == existing.id
    assert store.created == []
    assert response.status_code == 200
    assert store.reusable_lookups == [
        {
            "org_id": org_id,
            "user_id": user_id,
            "agent_id": "support-bot",
            "channel": "web",
        }
    ]


async def test_single_session_creates_when_none_reusable():
    org_id, user_id = uuid4(), uuid4()
    store = _Store(reusable=None)

    session = await sessions_route.create_session(
        sessions_route.CreateSessionRequest(),
        _request(store, _Storage()),
        Response(),
        _tenant(org_id, user_id),
        _runtime("support-bot", org_id, multi_session=False),
    )

    assert len(store.created) == 1
    assert store.created[0]["channel"] == "web"
    assert session.channel == "web"


async def test_multi_session_on_always_creates():
    org_id, user_id = uuid4(), uuid4()
    existing = SimpleNamespace(
        id=uuid4(), org_id=org_id, agent_id="support-bot",
        status="active", channel="web", model="gpt-test", config={},
    )
    store = _Store(reusable=existing)

    session = await sessions_route.create_session(
        sessions_route.CreateSessionRequest(),
        _request(store, _Storage()),
        Response(),
        _tenant(org_id, user_id),
        _runtime("support-bot", org_id, multi_session=True),
    )

    assert store.reusable_lookups == []
    assert len(store.created) == 1
    assert session.id != existing.id
