"""No generic route reaches a Keel case's sessions.

A case session holds a client's case data.  Only Keel's link, which loads
sessions directly, and the session's own worker-minted token reach it: an
agent's API key on ``/v1/api/*``, a user of the org on ``/v1/*`` and every
session listing answer as for a session that does not exist.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import os
import uuid
from types import SimpleNamespace
from uuid import UUID

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from surogates.api.routes.events import poll_events
from surogates.channels.constants import API_CHANNEL
from surogates.session.events import EventType
from surogates.session.provisioning import create_agent_session, create_child_session
from surogates.session.store import SessionStore
from surogates.tenant.auth.jwt import (
    create_access_token,
    create_service_account_session_token,
)
from surogates.tenant.auth.service_account import KIND_API_KEY, ServiceAccountStore
from surogates.tenant.context import TenantContext
from surogates.tenant.credentials import CredentialVault
from surogates.tools.builtin.session_search import _session_search_handler

from .conftest import create_org, create_user

pytestmark = pytest.mark.asyncio(loop_scope="session")

# The agent the app below resolves every request to, as test_prompts_api.py's does.
AGENT = "default"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(loop_scope="session")
async def app(session_factory, redis_client, pg_url, redis_url):
    """The API app on the test containers, as test_prompts_api.py builds it."""
    os.environ["SUROGATES_DB_URL"] = pg_url
    os.environ["SUROGATES_REDIS_URL"] = redis_url

    from surogates.api.app import create_app
    from surogates.config import Settings
    from surogates.runtime import (
        agent_runtime_context_dep,
        build_agent_runtime_context,
    )
    from surogates.storage.backend import create_backend

    application = create_app()
    application.state.session_factory = session_factory
    application.state.redis = redis_client
    application.state.session_store = SessionStore(session_factory, redis=redis_client)
    application.state.settings = Settings()
    application.state.settings.storage.bucket = f"test-agent-{uuid.uuid4()}"
    application.state.storage = create_backend(application.state.settings)
    application.state.credential_vault = CredentialVault(
        session_factory, Fernet.generate_key()
    )

    def _fixed_runtime_context():
        return build_agent_runtime_context({
            "agent_id": AGENT,
            "org_id": "00000000-0000-0000-0000-000000000000",
            "project_id": "test-project",
            "enabled": True,
            "version": 1,
            "storage_key_prefix": "test-project/default",
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


def _contract(case_id: UUID) -> dict:
    return {
        "case_id": str(case_id),
        "case_number": "LN-000001",
        "case_type": "loan",
        "type_version": 1,
        "token": "t1",
        "node": "check",
        "step": "Check completeness",
        "mode": "complete",
        "inputs": {"applicant": "Ioana", "amount": "182000.00"},
        "form": {
            "type": "object",
            "properties": {"docs_complete": {"type": "boolean"}},
            "additionalProperties": False,
        },
        "outcomes": {"complete": {"required": ["docs_complete"]}},
    }


@pytest_asyncio.fixture(loop_scope="session")
async def case(app, session_factory):
    """A step session of a loan case, an agent-bound API key and a user of its org."""
    org_id = await create_org(session_factory)
    case_id = uuid.uuid4()
    accounts = ServiceAccountStore(session_factory)
    case_type = await accounts.create(org_id=org_id, name="case-type:loan")
    session = await create_agent_session(
        store=app.state.session_store,
        storage=app.state.storage,
        settings=app.state.settings,
        org_id=org_id,
        user_id=None,
        agent_id=AGENT,
        channel=API_CHANNEL,
        service_account_id=case_type.id,
        case_step=_contract(case_id),
        case_scope={"tools": None, "knowledge": []},
        config={
            "memory_boundary": f"case:{case_id}",
            "workspace_boundary": f"case:{case_id}",
        },
    )
    key = await accounts.create(
        org_id=org_id, name="customer key", agent_id=AGENT, kind=KIND_API_KEY,
    )
    user_id = await create_user(session_factory, org_id)
    return SimpleNamespace(
        org_id=org_id,
        session=session,
        case_type=case_type,
        api_key={"Authorization": f"Bearer {key.token}"},
        user={
            "Authorization": "Bearer "
            + create_access_token(org_id, user_id, {"sessions:read", "sessions:write"})
        },
    )


async def _answer(client: AsyncClient, method: str, url: str, headers: dict, body: dict | None) -> int | str:
    """The status *url* answers, or ``"no answer"`` when it streams on.

    The event stream of a live session stays open: a stream that never
    closes is a route that reached the session.
    """
    try:
        response = await asyncio.wait_for(
            client.request(method, url, headers=headers, json=body), timeout=10,
        )
    except TimeoutError:
        return "no answer"
    return response.status_code


# Every generic route a stranger would reach a session through, the
# destructive one last.
_OUTCOME = {
    "events": [{
        "type": "user.define_outcome",
        "description": "Close the case",
        "rubric": {"type": "text", "content": "The case is closed."},
    }],
}
_ROUTES = (
    ("GET", "/sessions/{sid}", None),
    ("GET", "/sessions/{sid}/events", None),
    ("POST", "/sessions/{sid}/messages", {"content": "Tell me about this case."}),
    ("POST", "/sessions/{sid}/pause", None),
    ("POST", "/sessions/{sid}/retry", None),
    ("POST", "/sessions/{sid}/events", _OUTCOME),
    ("GET", "/sessions/{sid}/workspace/tree", None),
    ("DELETE", "/sessions/{sid}", None),
)
# Routes on /v1 alone: a key is refused there before any session is read.
_USER_ROUTES = (
    ("GET", "/sessions/{sid}/tree", None),
    ("GET", "/sessions/{sid}/children", None),
    ("GET", "/sessions/{sid}/events/poll", None),
    ("GET", "/memory?session_id={sid}", None),
)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("target", ["step", "child"])
async def test_generic_routes_never_reach_a_case_session(client, case, session_store, target):
    # A child the step makes, as delegation makes one, inherits the case.
    session = case.session
    if target == "child":
        session = await create_child_session(
            store=session_store, parent=case.session, channel="delegation",
        )
    sid = session.id

    listed = await client.get("/v1/sessions?include_descendants=true", headers=case.user)
    assert listed.status_code == 200, listed.text
    assert {s["id"] for s in listed.json()["sessions"]} & {str(case.session.id), str(sid)} == set()

    calls = [
        (method, prefix + path.format(sid=sid), headers, body)
        for prefix, headers, routes in (
            ("/v1/api", case.api_key, _ROUTES),
            ("/v1", case.user, _USER_ROUTES + _ROUTES),
        )
        for method, path, body in routes
    ]
    # The destructive ones last, so a refusal that fails cannot hide the others.
    calls.sort(key=lambda call: call[0] == "DELETE")
    answers = {
        f"{method} {url}": await _answer(client, method, url, headers, body)
        for method, url, headers, body in calls
    }

    assert answers == {route: 404 for route in answers}
    # Nothing reached it: no message, no outcome, still live.
    for untouched in {case.session.id, sid}:
        after = await session_store.get_session(untouched)
        assert after.status == "active"
        assert await session_store.get_events(untouched) == []


async def test_a_route_called_without_a_request_still_refuses_a_case_session(app, case):
    """A route handed its tenant by an in-process caller, with no request's tenant set, refuses all the same."""
    user = TenantContext(
        org_id=case.org_id,
        user_id=uuid.uuid4(),
        org_config={},
        user_preferences={},
        permissions=frozenset({"sessions:read"}),
        asset_root="",
    )
    poll = poll_events(case.session.id, SimpleNamespace(app=app), user, after=0, limit=50)

    with pytest.raises(HTTPException) as refused:
        # A fresh context: no request has set a tenant in it.
        await asyncio.create_task(poll, context=contextvars.Context())
    assert refused.value.status_code == 404


async def test_the_sessions_own_token_still_works(client, case):
    own = {
        "Authorization": "Bearer " + create_service_account_session_token(
            org_id=case.org_id,
            service_account_id=case.case_type.id,
            session_id=case.session.id,
        ),
    }

    tree = await client.get(f"/v1/api/sessions/{case.session.id}/workspace/tree", headers=own)
    assert tree.status_code == 200, tree.text
    memory = await client.get(f"/v1/memory?session_id={case.session.id}", headers=own)
    assert memory.status_code == 200, memory.text


async def test_an_ordinary_session_is_unchanged(client, case):
    started = await client.post(
        "/v1/api/prompts", json={"prompt": "Summarise the loan policy."}, headers=case.api_key,
    )
    assert started.status_code == 202, started.text
    sid = started.json()["session_id"]

    read = await client.get(f"/v1/api/sessions/{sid}", headers=case.api_key)
    assert read.status_code == 200, read.text
    assert read.json()["id"] == sid
    tree = await client.get(f"/v1/api/sessions/{sid}/workspace/tree", headers=case.api_key)
    assert tree.status_code == 200, tree.text


async def test_a_prompt_cannot_replay_into_a_case_session(client, case, session_store):
    reserved = await client.post(
        "/v1/api/prompts",
        json={"prompt": "hi", "idempotency_key": "keel:anything"},
        headers=case.api_key,
    )
    assert reserved.status_code == 422, reserved.text

    # Keel's link keys every step session ``keel:<effect key>``, so only a
    # corrupted row holds a case session under any other key.
    held = await session_store.create_session(
        user_id=None,
        org_id=case.org_id,
        agent_id=AGENT,
        channel=API_CHANNEL,
        service_account_id=case.case_type.id,
        config={"memory_boundary": "case:held", "workspace_boundary": "case:held"},
        idempotency_key="guessed",
    )

    replay = await client.post(
        "/v1/api/prompts",
        json={"prompt": "hi", "idempotency_key": "guessed"},
        headers=case.api_key,
    )
    assert replay.status_code == 409, replay.text
    assert replay.json()["detail"]["code"] == "key-in-use"
    assert str(held.id) not in replay.text
    assert await session_store.get_events(held.id) == []


async def test_an_operators_session_listing_never_shows_a_case_session(
    session_store, session_factory,
):
    """The ops owner console searches every principal's sessions of the agent, case sessions excepted."""
    org_id = await create_org(session_factory)
    operator = await ServiceAccountStore(session_factory).create(
        org_id=org_id, name=f"ops-chat-{org_id}-owner",
    )
    case_type = await ServiceAccountStore(session_factory).create(
        org_id=org_id, name="case-type:loan",
    )
    user_id = await create_user(session_factory, org_id)
    ordinary = await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id="agent-a", channel="web",
    )
    in_a_case = await session_store.create_session(
        user_id=None,
        org_id=org_id,
        agent_id="agent-a",
        channel=API_CHANNEL,
        service_account_id=case_type.id,
        config={"memory_boundary": "case:c1", "workspace_boundary": "case:c1"},
    )
    for session in (ordinary, in_a_case):
        await session_store.emit_event(
            session.id, EventType.USER_MESSAGE, {"content": "the applicant's magnesium invoice"},
        )

    async def search(query: str) -> dict:
        return json.loads(
            await _session_search_handler(
                {"query": query},
                session_store=session_store,
                tenant=SimpleNamespace(
                    org_id=org_id, user_id=None, service_account_id=operator.id,
                ),
                agent_id="agent-a",
                session_config={"ops": {"user_id": str(uuid.uuid4())}},
            )
        )

    found = await search("magnesium")
    assert found["success"] is True
    assert [r["session_id"] for r in found["results"]] == [str(ordinary.id)]

    recent = await search("")
    assert recent["mode"] == "recent"
    assert [r["session_id"] for r in recent["results"]] == [str(ordinary.id)]
