"""Integration tests for the fire-and-forget prompt-submission API.

Covers the service-account auth path (``surg_sk_…`` tokens), the
``POST /v1/api/prompts`` and ``POST /v1/api/prompts:batch`` endpoints,
idempotency semantics, and the admin CRUD for issuing service-account
tokens.
"""

from __future__ import annotations

import os
import uuid
from uuid import UUID

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient

from surogates.session.store import SessionStore
from surogates.tenant.auth.jwt import (
    create_access_token,
    create_service_account_session_token,
)
from surogates.tenant.auth.service_account import ServiceAccountStore
from surogates.tenant.credentials import CredentialVault

from .conftest import create_org, create_user

pytestmark = pytest.mark.asyncio(loop_scope="session")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(loop_scope="session")
async def app(session_factory, redis_client, pg_url, redis_url):
    """FastAPI app wired to the test containers, mirroring test_api.py."""
    os.environ["SUROGATES_DB_URL"] = pg_url
    os.environ["SUROGATES_REDIS_URL"] = redis_url

    from surogates.api.app import create_app
    from surogates.config import Settings
    from surogates.storage.backend import create_backend

    application = create_app()
    application.state.session_factory = session_factory
    application.state.redis = redis_client
    application.state.session_store = SessionStore(session_factory, redis=redis_client)
    application.state.settings = Settings()
    application.state.storage = create_backend(application.state.settings)
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


async def _admin_tenant(session_factory) -> tuple[UUID, str]:
    """Create an org + admin user; return (org_id, jwt)."""
    org_id = await create_org(session_factory)
    user_id = uuid.uuid4()
    await create_user(session_factory, org_id, user_id=user_id)
    token = create_access_token(org_id, user_id, {"admin"})
    return org_id, token


async def _non_admin_tenant(
    session_factory, org_id: UUID | None = None
) -> tuple[UUID, str]:
    """Create an org + non-admin user in it; return (org_id, jwt)."""
    if org_id is None:
        org_id = await create_org(session_factory)
    user_id = uuid.uuid4()
    await create_user(session_factory, org_id, user_id=user_id)
    token = create_access_token(org_id, user_id, {"sessions:read", "sessions:write"})
    return org_id, token


async def _issue_token(session_factory, org_id: UUID, name: str = "pipeline") -> str:
    """Issue a service-account token directly via the store."""
    from .conftest import issue_service_account_token

    issued = await issue_service_account_token(session_factory, org_id, name)
    return issued.token


# ---------------------------------------------------------------------------
# Service-account admin CRUD
# ---------------------------------------------------------------------------


async def test_admin_create_service_account_returns_token_once(
    client: AsyncClient, session_factory
):
    """POST /v1/admin/service-accounts returns the raw token exactly once."""
    org_id, jwt_token = await _admin_tenant(session_factory)

    resp = await client.post(
        "/v1/admin/service-accounts",
        json={"org_id": str(org_id), "name": "dataset-gen"},
        headers={"Authorization": f"Bearer {jwt_token}"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "dataset-gen"
    assert body["org_id"] == str(org_id)
    assert body["token"].startswith("surg_sk_")
    # Display prefix is the start of the raw token.
    assert body["token"].startswith(body["token_prefix"])

    # Listing never echoes the secret back.
    list_resp = await client.get(
        f"/v1/admin/service-accounts?org_id={org_id}",
        headers={"Authorization": f"Bearer {jwt_token}"},
    )
    assert list_resp.status_code == 200
    listed = list_resp.json()["service_accounts"]
    assert len(listed) == 1
    assert "token" not in listed[0]
    assert listed[0]["token_prefix"] == body["token_prefix"]


async def test_create_service_account_rejects_non_admin(
    client: AsyncClient, session_factory
):
    """Non-admin users — even in the target org — cannot mint tokens."""
    org_id, user_jwt = await _non_admin_tenant(session_factory)

    resp = await client.post(
        "/v1/admin/service-accounts",
        json={"org_id": str(org_id), "name": "pipeline"},
        headers={"Authorization": f"Bearer {user_jwt}"},
    )
    assert resp.status_code == 403
    assert "admin" in resp.json()["detail"].lower()


async def test_list_and_revoke_service_account_reject_non_admin(
    client: AsyncClient, session_factory
):
    """Listing and revoking require the admin permission too."""
    org_id, _ = await _non_admin_tenant(session_factory)
    _, user_jwt = await _non_admin_tenant(session_factory, org_id=org_id)

    issued = await ServiceAccountStore(session_factory).create(
        org_id=org_id, name="pipeline"
    )

    list_resp = await client.get(
        f"/v1/admin/service-accounts?org_id={org_id}",
        headers={"Authorization": f"Bearer {user_jwt}"},
    )
    assert list_resp.status_code == 403

    del_resp = await client.delete(
        f"/v1/admin/service-accounts/{issued.id}",
        headers={"Authorization": f"Bearer {user_jwt}"},
    )
    assert del_resp.status_code == 403


async def test_admin_revoke_service_account_rejects_further_use(
    client: AsyncClient, session_factory
):
    """Revoked tokens are immediately unusable on /v1/api/*."""
    org_id, jwt_token = await _admin_tenant(session_factory)

    token = await _issue_token(session_factory, org_id)
    # The token's service_account row id is the revoke handle.
    listed = await client.get(
        f"/v1/admin/service-accounts?org_id={org_id}",
        headers={"Authorization": f"Bearer {jwt_token}"},
    )
    sa_id = listed.json()["service_accounts"][0]["id"]

    rv = await client.delete(
        f"/v1/admin/service-accounts/{sa_id}",
        headers={"Authorization": f"Bearer {jwt_token}"},
    )
    assert rv.status_code == 204

    # Second delete returns 404 — revocation is not an idempotent no-op.
    rv_again = await client.delete(
        f"/v1/admin/service-accounts/{sa_id}",
        headers={"Authorization": f"Bearer {jwt_token}"},
    )
    assert rv_again.status_code == 404

    # The SA token no longer authenticates.
    denied = await client.post(
        "/v1/api/prompts",
        json={"prompt": "hi"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert denied.status_code == 401


# ---------------------------------------------------------------------------
# Token-type enforcement
# ---------------------------------------------------------------------------


async def test_service_account_token_rejected_outside_api_prefix(
    client: AsyncClient, session_factory
):
    """A SA token on a non-``/v1/api/`` route yields 403."""
    org_id = await create_org(session_factory)
    token = await _issue_token(session_factory, org_id)

    resp = await client.get(
        "/v1/sessions",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


async def test_jwt_rejected_on_prompts_endpoint(
    client: AsyncClient, session_factory
):
    """Interactive JWTs cannot submit prompts via /v1/api/prompts."""
    _, jwt_token = await _admin_tenant(session_factory)

    resp = await client.post(
        "/v1/api/prompts",
        json={"prompt": "hi"},
        headers={"Authorization": f"Bearer {jwt_token}"},
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Prompt submission
# ---------------------------------------------------------------------------


async def test_submit_prompt_creates_api_channel_session(
    client: AsyncClient, session_factory, session_store
):
    """A single submission creates a session, emits user.message, queues work."""
    org_id = await create_org(session_factory)
    token = await _issue_token(session_factory, org_id)

    resp = await client.post(
        "/v1/api/prompts",
        json={
            "prompt": "generate a training example about Pythagoras",
            "metadata": {"dataset_id": "ds_123", "row_index": 42},
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["session_id"] is not None
    assert body["event_id"] is not None
    assert body["deduplicated"] is False

    # Session persisted with api channel + no user, and passthrough metadata.
    sid = UUID(body["session_id"])
    session = await session_store.get_session(sid)
    assert session.channel == "api"
    assert session.user_id is None
    assert session.service_account_id is not None
    assert session.config["pipeline_metadata"] == {
        "dataset_id": "ds_123", "row_index": 42,
    }
    assert session.config["service_account_id"] == str(session.service_account_id)

    # The user.message event landed on the session.
    events = await session_store.get_events(sid)
    user_msgs = [e for e in events if e.type == "user.message"]
    assert len(user_msgs) == 1
    assert user_msgs[0].data["content"].startswith("generate a training")


async def test_idempotency_key_returns_existing_session(
    client: AsyncClient, session_factory
):
    """Two submissions with the same idempotency key yield the same session."""
    org_id = await create_org(session_factory)
    token = await _issue_token(session_factory, org_id)

    payload = {"prompt": "p", "idempotency_key": "batch-7/row-42"}
    first = await client.post(
        "/v1/api/prompts",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert first.status_code == 202
    first_body = first.json()
    assert first_body["deduplicated"] is False

    second = await client.post(
        "/v1/api/prompts",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert second.status_code == 202
    second_body = second.json()
    assert second_body["session_id"] == first_body["session_id"]
    assert second_body["deduplicated"] is True
    assert second_body["event_id"] is None


async def test_idempotency_key_scoped_per_org(
    client: AsyncClient, session_factory
):
    """Two orgs using the same idempotency key get independent sessions."""
    org_a = await create_org(session_factory)
    org_b = await create_org(session_factory)
    token_a = await _issue_token(session_factory, org_a)
    token_b = await _issue_token(session_factory, org_b)

    payload = {"prompt": "same key", "idempotency_key": "shared-key"}
    a = await client.post(
        "/v1/api/prompts", json=payload,
        headers={"Authorization": f"Bearer {token_a}"},
    )
    b = await client.post(
        "/v1/api/prompts", json=payload,
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert a.status_code == 202 and b.status_code == 202
    assert a.json()["session_id"] != b.json()["session_id"]
    assert b.json()["deduplicated"] is False


async def test_prompt_size_cap_rejects_oversize_body(
    client: AsyncClient, session_factory
):
    """Prompts larger than the cap are rejected by Pydantic validation."""
    from surogates.api.routes.prompts import MAX_PROMPT_LENGTH

    org_id = await create_org(session_factory)
    token = await _issue_token(session_factory, org_id)

    resp = await client.post(
        "/v1/api/prompts",
        json={"prompt": "x" * (MAX_PROMPT_LENGTH + 1)},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Service-account session JWT — worker → API calls on SA-owned sessions
# ---------------------------------------------------------------------------


async def test_sa_session_jwt_reads_shared_memory(
    client: AsyncClient, session_factory, session_store
):
    """Worker-minted SA session JWT can read /v1/memory (shared scope)."""
    org_id = await create_org(session_factory)
    issued = await ServiceAccountStore(session_factory).create(
        org_id=org_id, name="pipeline"
    )

    sa_session = await session_store.create_session(
        user_id=None,
        org_id=org_id,
        agent_id="",
        channel="api",
        service_account_id=issued.id,
    )
    jwt_token = create_service_account_session_token(
        org_id=org_id,
        service_account_id=issued.id,
        session_id=sa_session.id,
    )

    resp = await client.get(
        "/v1/memory",
        headers={"Authorization": f"Bearer {jwt_token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    # Empty to start, but the request reached the route and resolved.
    assert body["memory"] == []
    assert body["user"] == []


async def test_sa_session_jwt_scoped_to_its_own_session(
    client: AsyncClient, session_factory, session_store
):
    """A session JWT minted for session A cannot read/write session B.

    Covers every session-scoped route that takes a ``session_id`` path
    parameter: session metadata, event stream, and workspace browse.
    Each must 404 when the scope doesn't match, even though the org_id
    is the same.
    """
    org_id = await create_org(session_factory)
    issued = await ServiceAccountStore(session_factory).create(
        org_id=org_id, name="pipeline"
    )
    # Two sessions in the same org, same service account.
    sess_a = await session_store.create_session(
        user_id=None,
        org_id=org_id,
        agent_id="",
        channel="api",
        service_account_id=issued.id,
    )
    sess_b = await session_store.create_session(
        user_id=None,
        org_id=org_id,
        agent_id="",
        channel="api",
        service_account_id=issued.id,
    )
    # Token minted for session A only.
    token_a = create_service_account_session_token(
        org_id=org_id,
        service_account_id=issued.id,
        session_id=sess_a.id,
    )
    headers = {"Authorization": f"Bearer {token_a}"}

    # Session A: visible.
    ok = await client.get(f"/v1/sessions/{sess_a.id}", headers=headers)
    assert ok.status_code == 200
    assert ok.json()["id"] == str(sess_a.id)

    # Session B: 404 even though it exists in the same org.
    denied = await client.get(f"/v1/sessions/{sess_b.id}", headers=headers)
    assert denied.status_code == 404

    # Cannot inject user messages into session B.
    inject = await client.post(
        f"/v1/sessions/{sess_b.id}/messages",
        json={"content": "malicious injection"},
        headers=headers,
    )
    assert inject.status_code == 404

    # Cannot list session B's workspace.
    ls = await client.get(f"/v1/sessions/{sess_b.id}/workspace", headers=headers)
    assert ls.status_code == 404


async def test_sa_session_jwt_rejected_on_prompts_endpoint(
    client: AsyncClient, session_factory, session_store
):
    """Session-scoped SA JWTs cannot open new sessions via /v1/api/prompts."""
    org_id = await create_org(session_factory)
    issued = await ServiceAccountStore(session_factory).create(
        org_id=org_id, name="pipeline"
    )
    sa_session = await session_store.create_session(
        user_id=None,
        org_id=org_id,
        agent_id="",
        channel="api",
        service_account_id=issued.id,
    )
    jwt_token = create_service_account_session_token(
        org_id=org_id,
        service_account_id=issued.id,
        session_id=sa_session.id,
    )

    resp = await client.post(
        "/v1/api/prompts",
        json={"prompt": "leaked token reuse"},
        headers={"Authorization": f"Bearer {jwt_token}"},
    )
    assert resp.status_code == 403
    assert "session-scoped" in resp.json()["detail"].lower()


async def test_sa_session_jwt_peer_process_revocation_bounded_by_cache(
    client: AsyncClient, session_factory
):
    """Cached process keeps serving until TTL when a peer revokes directly.

    Simulates a multi-process deployment: process A (this test) has
    cached the SA resolution after auth; process B (a peer) revokes
    by writing ``revoked_at`` straight into the DB.  Process A's
    cache doesn't learn about the revoke, so the token keeps working
    within the TTL window — the documented trade-off for the cache.
    Clearing process A's cache by hand produces the convergence the
    peer expects.
    """
    from datetime import datetime, timezone

    from sqlalchemy import text

    from surogates.tenant.auth.service_account import _reset_caches

    _reset_caches()

    org_id = await create_org(session_factory)
    token = await _issue_token(session_factory, org_id)

    # Warm the cache via a bare-SA-token request.
    ok = await client.post(
        "/v1/api/prompts",
        json={"prompt": "first"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert ok.status_code == 202

    # Peer-process revoke: write revoked_at directly, skipping the
    # store method that would invalidate *this* process's cache.
    async with session_factory() as db:
        await db.execute(
            text(
                "UPDATE service_accounts SET revoked_at = :now "
                "WHERE org_id = :org"
            ),
            {"now": datetime.now(timezone.utc).replace(tzinfo=None), "org": org_id},
        )
        await db.commit()

    # Within the TTL window, this process still accepts the token.
    still_ok = await client.post(
        "/v1/api/prompts",
        json={"prompt": "second"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert still_ok.status_code == 202

    # Force convergence and retry — now the cache re-reads the DB.
    _reset_caches()
    denied = await client.post(
        "/v1/api/prompts",
        json={"prompt": "third"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert denied.status_code == 401


async def test_sa_session_jwt_rejected_after_revocation(
    client: AsyncClient, session_factory, session_store
):
    """Revoking the SA invalidates outstanding session JWTs immediately."""
    org_id = await create_org(session_factory)
    issued = await ServiceAccountStore(session_factory).create(
        org_id=org_id, name="pipeline"
    )
    sa_session = await session_store.create_session(
        user_id=None,
        org_id=org_id,
        agent_id="",
        channel="api",
        service_account_id=issued.id,
    )
    jwt_token = create_service_account_session_token(
        org_id=org_id,
        service_account_id=issued.id,
        session_id=sa_session.id,
    )

    # Token works before revocation.
    ok = await client.get(
        "/v1/memory",
        headers={"Authorization": f"Bearer {jwt_token}"},
    )
    assert ok.status_code == 200

    # Revoke the backing service account.
    await ServiceAccountStore(session_factory).revoke(
        service_account_id=issued.id, org_id=org_id
    )

    denied = await client.get(
        "/v1/memory",
        headers={"Authorization": f"Bearer {jwt_token}"},
    )
    assert denied.status_code == 401


async def test_sa_session_jwt_writes_shared_memory(
    client: AsyncClient, session_factory, session_store
):
    """SA-session writes to /v1/memory land in the shared/ memory scope."""
    org_id = await create_org(session_factory)
    issued = await ServiceAccountStore(session_factory).create(
        org_id=org_id, name="pipeline"
    )
    sa_session = await session_store.create_session(
        user_id=None,
        org_id=org_id,
        agent_id="",
        channel="api",
        service_account_id=issued.id,
    )
    jwt_token = create_service_account_session_token(
        org_id=org_id,
        service_account_id=issued.id,
        session_id=sa_session.id,
    )

    resp = await client.post(
        "/v1/memory",
        json={
            "action": "add",
            "target": "memory",
            "content": "shared fact for the org pipeline",
        },
        headers={"Authorization": f"Bearer {jwt_token}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["success"] is True

    # Read it back through the same token to confirm round-trip.
    get_resp = await client.get(
        "/v1/memory",
        headers={"Authorization": f"Bearer {jwt_token}"},
    )
    assert get_resp.status_code == 200
    assert "shared fact for the org pipeline" in get_resp.json()["memory"]


async def test_submit_batch_accepts_multiple_prompts(
    client: AsyncClient, session_factory, session_store
):
    """Batch endpoint creates one session per prompt in input order."""
    org_id = await create_org(session_factory)
    token = await _issue_token(session_factory, org_id)

    batch = {
        "prompts": [
            {"prompt": "first",  "idempotency_key": "r1", "metadata": {"i": 1}},
            {"prompt": "second", "idempotency_key": "r2", "metadata": {"i": 2}},
            {"prompt": "third",  "idempotency_key": "r3", "metadata": {"i": 3}},
        ],
    }
    resp = await client.post(
        "/v1/api/prompts:batch", json=batch,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 202, resp.text
    results = resp.json()["results"]
    assert len(results) == 3
    session_ids = [UUID(r["session_id"]) for r in results]
    assert len(set(session_ids)) == 3
    for expected_i, sid in enumerate(session_ids, start=1):
        session = await session_store.get_session(sid)
        assert session.channel == "api"
        assert session.config["pipeline_metadata"]["i"] == expected_i

    # Retrying the whole batch is fully deduplicated.
    retry = await client.post(
        "/v1/api/prompts:batch", json=batch,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert retry.status_code == 202
    retry_results = retry.json()["results"]
    assert all(r["deduplicated"] for r in retry_results)
    assert [r["session_id"] for r in retry_results] == [str(s) for s in session_ids]
