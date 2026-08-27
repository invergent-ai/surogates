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
        self.list_calls: list[dict] = []

    async def get_reusable_channel_session(self, **kwargs):
        self.reusable_lookups.append(kwargs)
        return self.reusable

    async def list_sessions(self, **kwargs):
        self.list_calls.append(kwargs)
        return []

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
            # Plain chat: no surface requested, so only a surface-less
            # session qualifies. Reuse never crosses surfaces.
            "surface": None,
        }
    ]


async def test_single_session_creates_canonical_marked_session():
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
    # The fresh session is stamped as the canonical single-session
    # conversation so every later create/list/access resolves to it.
    assert store.created[0]["config"]["single_session"] is True
    assert session.channel == "web"


async def test_client_supplied_marker_is_stripped():
    org_id, user_id = uuid4(), uuid4()
    store = _Store(reusable=None)

    await sessions_route.create_session(
        sessions_route.CreateSessionRequest(config={"single_session": True}),
        _request(store, _Storage()),
        Response(),
        _tenant(org_id, user_id),
        _runtime("support-bot", org_id, multi_session=True),
    )

    # The canonical stamp is server-owned: a pre-stamped session created
    # while multi-session is on would bypass a later lockdown.
    assert "single_session" not in store.created[0]["config"]


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
    # No marker on multi-session creates.
    assert "single_session" not in store.created[0]["config"]
    assert session.id != existing.id


async def test_list_hides_multi_era_sessions_when_capability_off():
    org_id, user_id = uuid4(), uuid4()
    store = _Store()

    await sessions_route.list_sessions(
        _request(store, _Storage()),
        _tenant(org_id, user_id),
        _runtime("support-bot", org_id, multi_session=False),
    )
    await sessions_route.list_sessions(
        _request(store, _Storage()),
        _tenant(org_id, user_id),
        _runtime("support-bot", org_id, multi_session=True),
    )

    assert store.list_calls[0]["single_session_only"] is True
    assert store.list_calls[1]["single_session_only"] is False


def _session(channel="web", *, parent_id=None, config=None):
    return SimpleNamespace(
        id=uuid4(),
        org_id=uuid4(),
        agent_id="support-bot",
        status="active",
        channel=channel,
        parent_id=parent_id,
        config=config or {},
    )


class _GetStore:
    def __init__(self, session):
        self._session = session

    async def get_session(self, session_id):
        return self._session


def _owning_tenant(session):
    tenant = SimpleNamespace(org_id=session.org_id)
    tenant.owns_session = lambda org_id, session_id: True
    return tenant


def _get_request(store):
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(session_store=store)),
    )


def _fake_runtime(multi_session):
    return SimpleNamespace(agent_id="support-bot", multi_session=multi_session)


async def test_access_block_hides_unmarked_web_session_when_off():
    from fastapi import HTTPException

    session = _session("web")
    with pytest.raises(HTTPException) as exc:
        await sessions_route._get_session_for_tenant(
            _get_request(_GetStore(session)), session.id,
            _owning_tenant(session), _fake_runtime(multi_session=False),
        )
    assert exc.value.status_code == 404


async def test_access_allows_canonical_and_children_and_other_channels():
    canonical = _session("web", config={"single_session": True})
    child = _session("web", parent_id=uuid4())
    api_session = _session("api")

    for session in (canonical, child, api_session):
        got = await sessions_route._get_session_for_tenant(
            _get_request(_GetStore(session)), session.id,
            _owning_tenant(session), _fake_runtime(multi_session=False),
        )
        assert got is session


async def test_access_unrestricted_when_capability_on():
    session = _session("web")  # unmarked multi-era session
    got = await sessions_route._get_session_for_tenant(
        _get_request(_GetStore(session)), session.id,
        _owning_tenant(session), _fake_runtime(multi_session=True),
    )
    assert got is session
