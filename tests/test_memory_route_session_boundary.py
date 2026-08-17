"""An agent's memory write lands in the partition its session reads from.

``POST /v1/memory`` composed its object key from ``storage_key_prefix`` and
the tenant's ``user_id`` alone.  Every harness memory call goes through this
route (``use_api_for_harness_tools`` is on by default and the memory tool
prefers the API client whenever one exists), so a boundary-scoped session —
any managed-channel thread, and every evaluation row — wrote into the agent's
real per-user memory, or, for a service account whose ``user_id`` is ``None``,
straight into org-shared memory, while its worker read
``boundaries/<boundary>/memory.json``.  An evaluation therefore read an empty
partition and wrote into the live agent: the exact contamination the
evaluation isolation exists to prevent.
"""
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from surogates.api.routes import memory as memory_route
from surogates.config import Settings
from surogates.runtime import build_agent_runtime_context
from surogates.session.store import SessionNotFoundError
from surogates.tenant.context import TenantContext

_EVAL_BOUNDARY = "eval:run-1-a1b2"


class _RecordingBackend:
    """Storage backend that records every key written."""

    def __init__(self) -> None:
        self.written: list[str] = []

    async def read(self, bucket, key):
        raise KeyError(key)

    async def write(self, bucket, key, data):
        self.written.append(key)


class _SessionStore:
    def __init__(self, session=None) -> None:
        self._session = session

    async def get_session(self, session_id):
        if self._session is None or self._session.id != session_id:
            raise SessionNotFoundError(str(session_id))
        return self._session


def _fixtures(session=None, *, org_id=None, service_account=True):
    org_id = org_id or uuid4()
    settings = Settings()
    settings.storage.bucket = "test-bucket"
    backend = _RecordingBackend()
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                settings=settings,
                storage=backend,
                session_store=_SessionStore(session),
            ),
        ),
    )
    tenant = TenantContext(
        org_id=org_id,
        user_id=None if service_account else uuid4(),
        org_config={},
        user_preferences={},
        permissions=frozenset(),
        asset_root="/tmp/assets",
        service_account_id=uuid4() if service_account else None,
    )
    agent_runtime = build_agent_runtime_context({
        "agent_id": "support-bot",
        "org_id": str(org_id),
        "project_id": "test-project",
        "enabled": True,
        "version": 1,
        "storage_key_prefix": "agents/support-bot",
        "multi_session": True,
    })
    return backend, request, tenant, agent_runtime


def _session(org_id, config, *, agent_id="support-bot"):
    return SimpleNamespace(
        id=uuid4(), org_id=org_id, agent_id=agent_id,
        channel=config.get("channel", "api"), config=config,
    )


async def _mutate(request, tenant, agent_runtime, session_id):
    return await memory_route.mutate_memory(
        memory_route.MemoryMutateRequest(
            action="add", target="memory", content="the sky is green",
        ),
        request,
        session_id,
        tenant,
        agent_runtime,
    )


async def test_eval_session_write_does_not_land_in_shared_memory():
    org_id = uuid4()
    session = _session(org_id, {"memory_boundary": _EVAL_BOUNDARY})
    backend, request, tenant, agent_runtime = _fixtures(session, org_id=org_id)

    result = await _mutate(request, tenant, agent_runtime, session.id)

    assert result.success is True
    assert backend.written == [
        f"agents/support-bot/boundaries/{_EVAL_BOUNDARY}/memory.json",
    ]
    # The bug's signature: a service account has no user_id, so the
    # unscoped key is the agent's own org-shared memory.
    assert "agents/support-bot/shared/memory.json" not in backend.written


async def test_eval_session_read_serves_the_boundary_partition():
    org_id = uuid4()
    session = _session(org_id, {"memory_boundary": _EVAL_BOUNDARY})
    _, request, tenant, agent_runtime = _fixtures(session, org_id=org_id)

    store = await memory_route._build_store(
        request, tenant, agent_runtime, session.id,
    )

    assert store._keys == {
        "memory": f"agents/support-bot/boundaries/{_EVAL_BOUNDARY}/memory.json",
        "user": f"agents/support-bot/boundaries/{_EVAL_BOUNDARY}/user.json",
    }


async def test_a_managed_channel_thread_is_scoped_too():
    # The root cause predates the evaluation work: a Slack thread's memory
    # went to the acting user's personal store while the worker read the
    # thread's boundary. Fixing it at the route fixes both.
    org_id = uuid4()
    session = _session(
        org_id,
        {"channel": "slack", "memory_boundary": "slack:c:C123"},
    )
    session.channel = "slack"
    backend, request, tenant, agent_runtime = _fixtures(
        session, org_id=org_id, service_account=False,
    )

    await _mutate(request, tenant, agent_runtime, session.id)

    assert backend.written == [
        "agents/support-bot/boundaries/slack:c:C123/memory.json",
    ]


async def test_a_plain_web_session_keeps_the_per_user_layout():
    org_id = uuid4()
    session = _session(org_id, {})
    session.channel = "web"
    backend, request, tenant, agent_runtime = _fixtures(
        session, org_id=org_id, service_account=False,
    )

    await _mutate(request, tenant, agent_runtime, session.id)

    assert backend.written == [
        f"agents/support-bot/users/{tenant.user_id}/memory.json",
    ]


async def test_no_session_id_keeps_todays_layout():
    # The Studio memory panel talks about a user's memory, not any one
    # session, and must keep working unchanged. A user JWT never carries
    # session_scope_id, so the new fallback has nothing to catch here.
    _, request, tenant, agent_runtime = _fixtures(service_account=False)
    assert tenant.session_scope_id is None

    store = await memory_route._build_store(request, tenant, agent_runtime)

    assert store._keys["memory"] == (
        f"agents/support-bot/users/{tenant.user_id}/memory.json"
    )


async def test_a_session_scoped_token_with_no_session_id_still_resolves_the_boundary():
    # The worker mints a service-account session token scoped to the
    # session id (create_service_account_session_token). A client built
    # without session_id -- HarnessAPIClient defaults it to None -- sends no
    # session_id query param, but the boundary must still resolve from the
    # token itself: the query param cannot be the only thing standing
    # between an eval session and shared memory.
    org_id = uuid4()
    session = _session(org_id, {"memory_boundary": _EVAL_BOUNDARY})
    backend, request, tenant, agent_runtime = _fixtures(session, org_id=org_id)
    tenant = replace(tenant, session_scope_id=session.id)

    result = await _mutate(request, tenant, agent_runtime, None)

    assert result.success is True
    assert backend.written == [
        f"agents/support-bot/boundaries/{_EVAL_BOUNDARY}/memory.json",
    ]
    assert "agents/support-bot/shared/memory.json" not in backend.written


async def test_a_session_from_another_org_is_a_404():
    # Otherwise ``session_id`` would be a way to write into another tenant's
    # boundary partition, or to probe which session ids exist.
    session = _session(uuid4(), {"memory_boundary": _EVAL_BOUNDARY})
    _, request, tenant, agent_runtime = _fixtures(session)

    with pytest.raises(HTTPException) as exc:
        await _mutate(request, tenant, agent_runtime, session.id)
    assert exc.value.status_code == 404


async def test_a_session_belonging_to_another_agent_is_a_404():
    org_id = uuid4()
    session = _session(
        org_id, {"memory_boundary": _EVAL_BOUNDARY}, agent_id="other-bot",
    )
    _, request, tenant, agent_runtime = _fixtures(session, org_id=org_id)

    with pytest.raises(HTTPException) as exc:
        await _mutate(request, tenant, agent_runtime, session.id)
    assert exc.value.status_code == 404


async def test_an_unknown_session_is_a_404():
    _, request, tenant, agent_runtime = _fixtures()

    with pytest.raises(HTTPException) as exc:
        await _mutate(request, tenant, agent_runtime, uuid4())
    assert exc.value.status_code == 404


def test_the_harness_client_sends_its_session_id():
    # The route can only scope what it is told about; the client is the only
    # thing that knows which session a memory call belongs to.
    from surogates.harness.api_client import HarnessAPIClient

    sid = str(uuid4())
    client = HarnessAPIClient(
        base_url="http://api", token="t", session_id=sid, agent_id="a1",
    )
    assert client._memory_params() == {"session_id": sid}
    assert client._merge_params(client._memory_params()) == {
        "agent_id": "a1", "session_id": sid,
    }

    # A client with no session (legacy wiring) sends nothing extra and gets
    # today's unscoped behaviour rather than an error.
    assert HarnessAPIClient(
        base_url="http://api", token="t",
    )._memory_params() is None
