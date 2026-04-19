"""Integration tests for the feedback endpoint.

Covers ``POST /v1/sessions/{session_id}/events/{event_id}/feedback`` —
the endpoint the web chat UI hits when a user clicks thumbs-up or
thumbs-down on an assistant turn.  Expert results and regular LLM
responses both flow through this endpoint; experts emit dedicated
EXPERT_ENDORSE / EXPERT_OVERRIDE events, responses emit USER_FEEDBACK.
"""

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

from .conftest import create_org, create_user

pytestmark = pytest.mark.asyncio(loop_scope="session")


# ---------------------------------------------------------------------------
# Fixtures (mirror those in test_api.py; kept local for SRP)
# ---------------------------------------------------------------------------


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
    application.state.session_store = SessionStore(session_factory)
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


async def _create_test_tenant(
    session_factory,
) -> tuple[UUID, UUID, str]:
    """Create org + user + JWT. Returns (org_id, user_id, token)."""
    org_id = await create_org(session_factory)
    user_id = uuid.uuid4()
    await create_user(
        session_factory,
        org_id,
        user_id=user_id,
        email=f"user-{user_id}@test.com",
        password="testpass123",
    )
    token = create_access_token(
        org_id, user_id, {"sessions:read", "sessions:write"},
    )
    return org_id, user_id, token


async def _create_session_with_expert_result(
    client: AsyncClient,
    session_store: SessionStore,
    token: str,
    *,
    expert_name: str = "sql_writer",
) -> tuple[str, int]:
    """Create a session via the API and seed an expert.result event.

    Returns ``(session_id, expert_result_event_id)``.
    """
    create_resp = await client.post(
        "/v1/sessions",
        json={"model": "gpt-4o"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert create_resp.status_code == 201
    session_id = create_resp.json()["id"]

    event_id = await session_store.emit_event(
        UUID(session_id),
        EventType.EXPERT_RESULT,
        {"expert": expert_name, "success": True, "iterations_used": 3},
    )
    return session_id, event_id


# ---------------------------------------------------------------------------
# Thumbs-up → EXPERT_ENDORSE
# ---------------------------------------------------------------------------


async def test_thumbs_up_emits_endorse(
    client: AsyncClient, session_factory, session_store
):
    """POST feedback with rating=up emits an EXPERT_ENDORSE event."""
    _, user_id, token = await _create_test_tenant(session_factory)
    session_id, expert_event_id = await _create_session_with_expert_result(
        client, session_store, token,
    )

    resp = await client.post(
        f"/v1/sessions/{session_id}/events/{expert_event_id}/feedback",
        json={"rating": "up"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["event_type"] == EventType.EXPERT_ENDORSE.value
    assert body["event_id"] > expert_event_id

    # Verify the event landed in the log with the expected shape.
    events = await session_store.get_events(
        UUID(session_id), types=[EventType.EXPERT_ENDORSE],
    )
    assert len(events) == 1
    data = events[0].data
    assert data["expert"] == "sql_writer"
    assert data["target_event_id"] == expert_event_id
    assert data["rating"] == "up"
    assert data["rated_by_user_id"] == str(user_id)


async def test_thumbs_down_emits_override_with_reason(
    client: AsyncClient, session_factory, session_store
):
    """POST feedback with rating=down and a reason emits EXPERT_OVERRIDE."""
    _, _, token = await _create_test_tenant(session_factory)
    session_id, expert_event_id = await _create_session_with_expert_result(
        client, session_store, token,
    )

    resp = await client.post(
        f"/v1/sessions/{session_id}/events/{expert_event_id}/feedback",
        json={"rating": "down", "reason": "query missed the WHERE clause"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201
    assert resp.json()["event_type"] == EventType.EXPERT_OVERRIDE.value

    events = await session_store.get_events(
        UUID(session_id), types=[EventType.EXPERT_OVERRIDE],
    )
    assert len(events) == 1
    assert events[0].data["reason"] == "query missed the WHERE clause"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


async def test_feedback_on_unsupported_event_type_rejected(
    client: AsyncClient, session_factory, session_store
):
    """Rating an event that is neither expert.result nor llm.response returns 400."""
    _, _, token = await _create_test_tenant(session_factory)

    create_resp = await client.post(
        "/v1/sessions",
        json={"model": "gpt-4o"},
        headers={"Authorization": f"Bearer {token}"},
    )
    session_id = create_resp.json()["id"]

    # Seed an event of the wrong type (user.message).
    wrong_event_id = await session_store.emit_event(
        UUID(session_id),
        EventType.USER_MESSAGE,
        {"content": "hello"},
    )

    resp = await client.post(
        f"/v1/sessions/{session_id}/events/{wrong_event_id}/feedback",
        json={"rating": "up"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "expert.result" in detail
    assert "llm.response" in detail


# ---------------------------------------------------------------------------
# llm.response feedback → USER_FEEDBACK
# ---------------------------------------------------------------------------


async def _seed_llm_response(
    client: AsyncClient,
    session_store: SessionStore,
    token: str,
) -> tuple[str, int]:
    """Create a session and seed an llm.response.  Returns (session_id, event_id)."""
    create_resp = await client.post(
        "/v1/sessions",
        json={"model": "gpt-4o"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert create_resp.status_code == 201
    session_id = create_resp.json()["id"]

    event_id = await session_store.emit_event(
        UUID(session_id),
        EventType.LLM_RESPONSE,
        {
            "message": {"role": "assistant", "content": "the answer is 42"},
            "model": "gpt-4o",
            "input_tokens": 5,
            "output_tokens": 6,
        },
    )
    return session_id, event_id


async def test_thumbs_up_on_llm_response_emits_user_feedback(
    client: AsyncClient, session_factory, session_store,
):
    """POST feedback with rating=up on an llm.response emits USER_FEEDBACK."""
    _, user_id, token = await _create_test_tenant(session_factory)
    session_id, response_event_id = await _seed_llm_response(
        client, session_store, token,
    )

    resp = await client.post(
        f"/v1/sessions/{session_id}/events/{response_event_id}/feedback",
        json={"rating": "up"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["event_type"] == EventType.USER_FEEDBACK.value

    events = await session_store.get_events(
        UUID(session_id), types=[EventType.USER_FEEDBACK],
    )
    assert len(events) == 1
    data = events[0].data
    assert data["target_event_id"] == response_event_id
    assert data["rating"] == "up"
    assert data["rated_by_user_id"] == str(user_id)


async def test_llm_response_feedback_is_idempotent_per_user(
    client: AsyncClient, session_factory, session_store,
):
    """A user who rates the same llm.response twice gets the first feedback back."""
    _, _, token = await _create_test_tenant(session_factory)
    session_id, response_event_id = await _seed_llm_response(
        client, session_store, token,
    )

    first = await client.post(
        f"/v1/sessions/{session_id}/events/{response_event_id}/feedback",
        json={"rating": "up"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert first.status_code == 201
    first_event_id = first.json()["event_id"]

    second = await client.post(
        f"/v1/sessions/{session_id}/events/{response_event_id}/feedback",
        json={"rating": "down"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert second.status_code == 201
    assert second.json()["event_id"] == first_event_id
    assert second.json()["event_type"] == EventType.USER_FEEDBACK.value

    events = await session_store.get_events(
        UUID(session_id), types=[EventType.USER_FEEDBACK],
    )
    assert len(events) == 1
    assert events[0].data["rating"] == "up"


async def test_llm_response_feedback_reason_is_recorded(
    client: AsyncClient, session_factory, session_store,
):
    """Optional reason text survives on USER_FEEDBACK."""
    _, _, token = await _create_test_tenant(session_factory)
    session_id, response_event_id = await _seed_llm_response(
        client, session_store, token,
    )

    resp = await client.post(
        f"/v1/sessions/{session_id}/events/{response_event_id}/feedback",
        json={"rating": "down", "reason": "hallucinated the date"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201

    events = await session_store.get_events(
        UUID(session_id), types=[EventType.USER_FEEDBACK],
    )
    assert len(events) == 1
    assert events[0].data["reason"] == "hallucinated the date"


async def test_feedback_on_missing_event_returns_404(
    client: AsyncClient, session_factory
):
    """Rating an event id that doesn't exist returns 404."""
    _, _, token = await _create_test_tenant(session_factory)
    create_resp = await client.post(
        "/v1/sessions",
        json={"model": "gpt-4o"},
        headers={"Authorization": f"Bearer {token}"},
    )
    session_id = create_resp.json()["id"]

    resp = await client.post(
        f"/v1/sessions/{session_id}/events/999999999/feedback",
        json={"rating": "up"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404


async def test_feedback_on_missing_session_returns_404(
    client: AsyncClient, session_factory
):
    """Rating against a non-existent session id returns 404."""
    _, _, token = await _create_test_tenant(session_factory)

    fake_session_id = uuid.uuid4()
    resp = await client.post(
        f"/v1/sessions/{fake_session_id}/events/1/feedback",
        json={"rating": "up"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404


async def test_feedback_invalid_rating_rejected(
    client: AsyncClient, session_factory, session_store
):
    """Rating values other than 'up'/'down' are rejected by validation."""
    _, _, token = await _create_test_tenant(session_factory)
    session_id, expert_event_id = await _create_session_with_expert_result(
        client, session_store, token,
    )

    resp = await client.post(
        f"/v1/sessions/{session_id}/events/{expert_event_id}/feedback",
        json={"rating": "maybe"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Idempotency + tenant isolation
# ---------------------------------------------------------------------------


async def test_feedback_is_idempotent_per_user(
    client: AsyncClient, session_factory, session_store
):
    """Same user rating the same event twice returns the prior event, not a new one."""
    _, _, token = await _create_test_tenant(session_factory)
    session_id, expert_event_id = await _create_session_with_expert_result(
        client, session_store, token,
    )

    first = await client.post(
        f"/v1/sessions/{session_id}/events/{expert_event_id}/feedback",
        json={"rating": "up"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert first.status_code == 201
    first_event_id = first.json()["event_id"]

    second = await client.post(
        f"/v1/sessions/{session_id}/events/{expert_event_id}/feedback",
        json={"rating": "down"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert second.status_code == 201
    assert second.json()["event_id"] == first_event_id
    # Type of the stored feedback is preserved — second call does not flip it.
    assert second.json()["event_type"] == EventType.EXPERT_ENDORSE.value

    endorses = await session_store.get_events(
        UUID(session_id), types=[EventType.EXPERT_ENDORSE],
    )
    overrides = await session_store.get_events(
        UUID(session_id), types=[EventType.EXPERT_OVERRIDE],
    )
    assert len(endorses) == 1
    assert len(overrides) == 0


async def test_feedback_cross_tenant_returns_404(
    client: AsyncClient, session_factory, session_store
):
    """A user from a different org cannot rate another org's expert result."""
    _, _, owner_token = await _create_test_tenant(session_factory)
    _, _, intruder_token = await _create_test_tenant(session_factory)

    session_id, expert_event_id = await _create_session_with_expert_result(
        client, session_store, owner_token,
    )

    resp = await client.post(
        f"/v1/sessions/{session_id}/events/{expert_event_id}/feedback",
        json={"rating": "up"},
        headers={"Authorization": f"Bearer {intruder_token}"},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Judge feedback (service-account clients on the API channel)
# ---------------------------------------------------------------------------


async def test_jwt_rejected_on_api_feedback_route(
    client: AsyncClient, session_factory, session_store,
):
    """Interactive JWTs cannot hit the /v1/api/ mount of the feedback route.

    The /v1/api/ prefix is the programmatic surface — only service-account
    tokens belong there.  A user JWT reaches the handler through the
    /v1/ mount instead.
    """
    _, _, jwt_token = await _create_test_tenant(session_factory)
    session_id, expert_event_id = await _create_session_with_expert_result(
        client, session_store, jwt_token,
    )

    resp = await client.post(
        f"/v1/api/sessions/{session_id}/events/{expert_event_id}/feedback",
        json={"rating": "up"},
        headers={"Authorization": f"Bearer {jwt_token}"},
    )
    assert resp.status_code == 403
    assert "service-account" in resp.json()["detail"].lower()


async def test_judge_feedback_via_api_channel_emits_source_judge(
    client: AsyncClient, session_factory, session_store,
):
    """A service-account judge submits rich feedback via /v1/api/... and the
    event carries source='judge' plus the numeric score/criteria payload.
    """
    from .conftest import issue_service_account_token

    org_id = await create_org(session_factory)
    user_id = uuid.uuid4()
    await create_user(session_factory, org_id, user_id=user_id)

    # Seed a session + llm.response that the judge will rate.
    sess = await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id="", channel="api",
    )
    response_event_id = await session_store.emit_event(
        sess.id,
        EventType.LLM_RESPONSE,
        {
            "message": {"role": "assistant", "content": "42"},
            "model": "gpt-4o", "input_tokens": 1, "output_tokens": 1,
        },
    )

    # Judge presents an SA bearer token.
    issued = await issue_service_account_token(
    session_factory, org_id, "eval-judge-v1",
    )

    resp = await client.post(
        f"/v1/api/sessions/{sess.id}/events/{response_event_id}/feedback",
        json={
            "rating": "up",
            "score": 0.87,
            "criteria": {"correctness": 0.9, "relevance": 0.85},
            "rationale": "Matches the reference; arithmetic is correct.",
        },
        headers={"Authorization": f"Bearer {issued.token}"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["source"] == "judge"
    assert body["event_type"] == EventType.USER_FEEDBACK.value

    events = await session_store.get_events(
        sess.id, types=[EventType.USER_FEEDBACK],
    )
    assert len(events) == 1
    data = events[0].data
    assert data["source"] == "judge"
    assert data["rated_by_service_account_id"] == str(issued.id)
    assert "rated_by_user_id" not in data
    assert data["score"] == 0.87
    assert data["criteria"] == {"correctness": 0.9, "relevance": 0.85}
    assert data["rationale"].startswith("Matches the reference")


async def test_judge_feedback_dedupe_is_per_service_account(
    client: AsyncClient, session_factory, session_store,
):
    """A judge that retries the same submission gets the first event back."""
    from .conftest import issue_service_account_token

    org_id = await create_org(session_factory)
    user_id = uuid.uuid4()
    await create_user(session_factory, org_id, user_id=user_id)

    sess = await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id="", channel="api",
    )
    response_event_id = await session_store.emit_event(
        sess.id,
        EventType.LLM_RESPONSE,
        {
            "message": {"role": "assistant", "content": "..."},
            "model": "gpt-4o", "input_tokens": 1, "output_tokens": 1,
        },
    )
    issued = await issue_service_account_token(
    session_factory, org_id, "eval-judge",
    )
    headers = {"Authorization": f"Bearer {issued.token}"}
    url = f"/v1/api/sessions/{sess.id}/events/{response_event_id}/feedback"

    first = await client.post(url, json={"rating": "up"}, headers=headers)
    assert first.status_code == 201
    first_event_id = first.json()["event_id"]

    second = await client.post(url, json={"rating": "down"}, headers=headers)
    assert second.status_code == 201
    assert second.json()["event_id"] == first_event_id

    events = await session_store.get_events(
        sess.id, types=[EventType.USER_FEEDBACK],
    )
    assert len(events) == 1
    assert events[0].data["rating"] == "up"


async def test_judge_and_user_feedback_coexist_on_same_event(
    client: AsyncClient, session_factory, session_store,
):
    """A judge and a user rating the same event both emit independent feedback."""
    from .conftest import issue_service_account_token

    org_id, user_id, jwt_token = await _create_test_tenant(session_factory)

    # Re-use the JWT-owned session so the JWT caller's request passes the
    # org-scope check, and seed an llm.response inside it.
    create_resp = await client.post(
        "/v1/sessions",
        json={"model": "gpt-4o"},
        headers={"Authorization": f"Bearer {jwt_token}"},
    )
    session_id = create_resp.json()["id"]
    response_event_id = await session_store.emit_event(
        UUID(session_id),
        EventType.LLM_RESPONSE,
        {
            "message": {"role": "assistant", "content": "..."},
            "model": "gpt-4o", "input_tokens": 1, "output_tokens": 1,
        },
    )

    issued = await issue_service_account_token(
    session_factory, org_id, "eval-judge",
    )

    user_resp = await client.post(
        f"/v1/sessions/{session_id}/events/{response_event_id}/feedback",
        json={"rating": "up"},
        headers={"Authorization": f"Bearer {jwt_token}"},
    )
    assert user_resp.status_code == 201
    assert user_resp.json()["source"] == "user"

    judge_resp = await client.post(
        f"/v1/api/sessions/{session_id}/events/{response_event_id}/feedback",
        json={"rating": "down", "score": 0.1},
        headers={"Authorization": f"Bearer {issued.token}"},
    )
    assert judge_resp.status_code == 201
    assert judge_resp.json()["source"] == "judge"
    assert judge_resp.json()["event_id"] != user_resp.json()["event_id"]

    events = await session_store.get_events(
        UUID(session_id), types=[EventType.USER_FEEDBACK],
    )
    sources = [e.data["source"] for e in events]
    assert sorted(sources) == ["judge", "user"]
