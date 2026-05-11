"""API tests for /v1/inbox."""

from __future__ import annotations

import asyncio
import os
import uuid
from uuid import UUID

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from starlette.responses import StreamingResponse

from surogates.db.models import Event, InboxItem
from surogates.session.events import EventType
from surogates.session.store import SessionStore
from surogates.tenant.auth.jwt import create_access_token
from surogates.tenant.credentials import CredentialVault

from .conftest import create_org, create_user, issue_service_account_token

pytestmark = pytest.mark.asyncio(loop_scope="session")


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


@pytest_asyncio.fixture(loop_scope="session")
async def client(app):
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as c:
        yield c


async def _create_user_token_session(session_factory, session_store):
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
        agent_id="test-agent",
    )
    return org_id, user_id, token, session


async def _create_service_account_token(session_factory) -> tuple[UUID, str]:
    org_id = await create_org(session_factory)
    issued = await issue_service_account_token(session_factory, org_id)
    return org_id, issued.token


async def _get_inbox_item_for_event(session_store, event_id: int) -> InboxItem:
    async with session_store._sf() as db:
        return (
            await db.execute(
                select(InboxItem).where(InboxItem.source_event_id == event_id)
            )
        ).scalar_one()


async def _emit_task_complete(session_store, session_id) -> InboxItem:
    event_id = await session_store.emit_event(
        session_id,
        EventType.INBOX_TASK_COMPLETE,
        {
            "outcome": "success",
            "duration_seconds": 1,
            "summary": "All done.",
            "session_title": "Task complete",
        },
    )
    return await _get_inbox_item_for_event(session_store, event_id)


async def test_list_inbox_returns_only_callers_items(
    client,
    session_factory,
    session_store,
):
    _, _, token, session = await _create_user_token_session(
        session_factory,
        session_store,
    )
    await session_store.emit_event(
        session.id,
        EventType.INBOX_TASK_COMPLETE,
        {
            "outcome": "success",
            "duration_seconds": 1,
            "summary": "All done.",
            "session_title": "Task complete",
        },
    )

    response = await client.get(
        "/v1/inbox",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["kind"] == "task_complete"
    assert body["items"][0]["session_id"] == str(session.id)


async def test_list_inbox_rejects_service_account(
    client,
    session_factory,
):
    _, token = await _create_service_account_token(session_factory)

    response = await client.get(
        "/v1/inbox",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 403


async def test_get_inbox_item(client, session_factory, session_store):
    _, _, token, session = await _create_user_token_session(
        session_factory,
        session_store,
    )
    item = await _emit_task_complete(session_store, session.id)

    response = await client.get(
        f"/v1/inbox/{item.id}",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["id"] == item.id


async def test_get_other_users_item_returns_404(
    client,
    session_factory,
    session_store,
):
    _, _, owner_token, session = await _create_user_token_session(
        session_factory,
        session_store,
    )
    assert owner_token
    _, _, other_token, _ = await _create_user_token_session(
        session_factory,
        session_store,
    )
    item = await _emit_task_complete(session_store, session.id)

    response = await client.get(
        f"/v1/inbox/{item.id}",
        headers={"Authorization": f"Bearer {other_token}"},
    )

    assert response.status_code == 404


async def test_mark_read_is_idempotent(client, session_factory, session_store):
    _, _, token, session = await _create_user_token_session(
        session_factory,
        session_store,
    )
    item = await _emit_task_complete(session_store, session.id)
    headers = {"Authorization": f"Bearer {token}"}

    first = await client.post(f"/v1/inbox/{item.id}/read", headers=headers)
    second = await client.post(f"/v1/inbox/{item.id}/read", headers=headers)

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["read_at"] is not None
    assert first.json()["read_at"] == second.json()["read_at"]


async def test_ack_flips_status_to_acknowledged(
    client,
    session_factory,
    session_store,
):
    _, _, token, session = await _create_user_token_session(
        session_factory,
        session_store,
    )
    item = await _emit_task_complete(session_store, session.id)

    response = await client.post(
        f"/v1/inbox/{item.id}/ack",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "acknowledged"
    assert response.json()["responded_at"] is not None


async def test_ack_rejects_non_ackable_kind(
    client,
    session_factory,
    session_store,
):
    _, _, token, session = await _create_user_token_session(
        session_factory,
        session_store,
    )
    event_id = await session_store.emit_event(
        session.id,
        EventType.INBOX_INPUT_REQUIRED,
        {
            "tool_call_id": "tc-ack-reject",
            "questions": [{"prompt": "Which color?"}],
            "context": "",
        },
    )
    item = await _get_inbox_item_for_event(session_store, event_id)

    response = await client.post(
        f"/v1/inbox/{item.id}/ack",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 409


async def test_respond_governance_records_decision_and_wakes_session(
    client,
    session_factory,
    session_store,
    monkeypatch,
):
    _, _, token, session = await _create_user_token_session(
        session_factory,
        session_store,
    )
    event_id = await session_store.emit_event(
        session.id,
        EventType.INBOX_GOVERNANCE_GATE,
        {
            "tool_name": "send_email",
            "tool_call_id": "tc-gov-3",
            "arguments_excerpt": "to=ceo@example.com",
            "deny_reason": "External recipient",
            "policy_id": "external-comms-v1",
        },
    )
    item = await _get_inbox_item_for_event(session_store, event_id)
    woken = []

    async def fake_wake(request, session_id):
        assert request
        woken.append(session_id)

    monkeypatch.setattr(
        "surogates.api.routes.inbox._wake_session_from_request",
        fake_wake,
        raising=False,
    )

    response = await client.post(
        f"/v1/inbox/{item.id}/respond",
        json={"decision": "approve"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "responded"
    assert response.json()["responded_at"] is not None
    assert woken == [session.id]

    async with session_store._sf() as db:
        rows = (
            await db.execute(
                select(Event)
                .where(
                    Event.session_id == session.id,
                    Event.type == EventType.USER_MESSAGE.value,
                )
                .order_by(Event.id)
            )
        ).scalars().all()

    assert len(rows) == 1
    assert rows[0].data["source"] == "inbox_governance_decision"
    assert "APPROVE" in rows[0].data["content"]
    assert "send_email" in rows[0].data["content"]


async def test_respond_rejects_non_governance_kind(
    client,
    session_factory,
    session_store,
):
    _, _, token, session = await _create_user_token_session(
        session_factory,
        session_store,
    )
    item = await _emit_task_complete(session_store, session.id)

    response = await client.post(
        f"/v1/inbox/{item.id}/respond",
        json={"decision": "approve"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 409


async def test_sse_stream_emits_snapshot_and_nudge_for_new_item(
    client,
    app,
    session_factory,
    monkeypatch,
):
    _, _, token, session = await _create_user_token_session(
        session_factory,
        app.state.session_store,
    )
    headers = {"Authorization": f"Bearer {token}"}

    def finite_event_source(generator):
        async def limited_stream():
            async for event in generator:
                if "event" not in event:
                    continue
                yield f"event: {event['event']}\n"
                yield f"data: {event['data']}\n\n"
                if event["event"] == "item":
                    break

        return StreamingResponse(
            limited_stream(),
            media_type="text/event-stream",
        )

    monkeypatch.setattr(
        "surogates.api.routes.inbox.EventSourceResponse",
        finite_event_source,
        raising=False,
    )

    async def emit_item():
        await asyncio.sleep(0.1)
        await app.state.session_store.emit_event(
            session.id,
            EventType.INBOX_TASK_COMPLETE,
            {
                "outcome": "success",
                "duration_seconds": 1,
                "summary": "All done.",
                "session_title": "Task complete",
            },
        )

    emitter = asyncio.create_task(emit_item())
    try:
        async with asyncio.timeout(5):
            response = await client.get(
                "/v1/inbox/stream",
                headers=headers,
            )
    finally:
        await emitter

    assert response.status_code == 200, response.text
    assert "event: snapshot" in response.text
    assert "event: item" in response.text
    assert "task_complete" in response.text
