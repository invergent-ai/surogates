"""API tests for /v1/inbox."""

from __future__ import annotations

import asyncio
import json
import uuid
from uuid import UUID

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from starlette.responses import StreamingResponse

from surogates.db.models import Event, InboxItem
from surogates.session.events import EventType
from surogates.tenant.auth.jwt import create_access_token

from .conftest import create_org, create_user, issue_service_account_token
from .inbox_e2e_helpers import (
    AGENT_ID,
    OTHER_AGENT_ID,
    build_inbox_test_app,
    inbox_path,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest_asyncio.fixture(loop_scope="session")
async def app(session_factory, redis_client, pg_url, redis_url):
    return build_inbox_test_app(session_factory, redis_client, pg_url, redis_url)


@pytest_asyncio.fixture(loop_scope="session")
async def client(app):
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as c:
        yield c


async def _create_user_token_session(
    session_factory,
    session_store,
    *,
    agent_id: str = AGENT_ID,
):
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
        inbox_path(),
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
        inbox_path(),
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
        inbox_path(f"/{item.id}"),
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
        inbox_path(f"/{item.id}"),
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

    first = await client.post(inbox_path(f"/{item.id}/read"), headers=headers)
    second = await client.post(inbox_path(f"/{item.id}/read"), headers=headers)

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
        inbox_path(f"/{item.id}/ack"),
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
        inbox_path(f"/{item.id}/ack"),
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 409


async def test_delete_inbox_item_expires_and_hides_item(
    client,
    session_factory,
    session_store,
):
    _, _, token, session = await _create_user_token_session(
        session_factory,
        session_store,
    )
    item = await _emit_task_complete(session_store, session.id)
    headers = {"Authorization": f"Bearer {token}"}

    response = await client.delete(inbox_path(f"/{item.id}"), headers=headers)

    assert response.status_code == 204, response.text

    default_list = await client.get(inbox_path(), headers=headers)
    assert default_list.status_code == 200, default_list.text
    assert default_list.json()["items"] == []

    expired_list = await client.get(inbox_path(query="status=expired"), headers=headers)
    assert expired_list.status_code == 200, expired_list.text
    assert [row["id"] for row in expired_list.json()["items"]] == [item.id]
    assert expired_list.json()["items"][0]["status"] == "expired"

    async with session_store._sf() as db:
        row = await db.get(InboxItem, item.id)
    assert row is not None
    assert row.status == "expired"
    assert row.responded_at is not None


async def test_list_accepts_several_statuses_at_once(
    client,
    session_factory,
    session_store,
):
    """A history view asks for everything the user is done with, which
    spans three statuses — and expired items are invisible to any request
    that does not name them."""
    _, user_id, token, session = await _create_user_token_session(
        session_factory,
        session_store,
    )
    headers = {"Authorization": f"Bearer {token}"}
    still_pending = await _emit_task_complete(session_store, session.id)
    acknowledged = await _emit_task_complete(session_store, session.id)
    hidden = await _emit_task_complete(session_store, session.id)
    await session_store.set_inbox_status(
        item_id=acknowledged.id,
        user_id=user_id,
        agent_id=AGENT_ID,
        new_status="acknowledged",
    )
    await session_store.delete_inbox_item(
        item_id=hidden.id, user_id=user_id, agent_id=AGENT_ID,
    )

    response = await client.get(
        inbox_path(query="status=acknowledged&status=responded&status=expired"),
        headers=headers,
    )

    assert response.status_code == 200, response.text
    returned = {row["id"] for row in response.json()["items"]}
    assert returned == {acknowledged.id, hidden.id}
    assert still_pending.id not in returned


async def test_list_accepts_several_kinds_at_once(
    client,
    session_factory,
    session_store,
):
    """Active and Updates are two lists, not one list filtered twice.

    The kinds that need a response and the ones that are only news are
    shown apart, so the request has to be able to name a set.
    """
    _, _, token, session = await _create_user_token_session(
        session_factory,
        session_store,
    )
    update = await _emit_task_complete(session_store, session.id)
    question_event = await session_store.emit_event(
        session.id,
        EventType.INBOX_INPUT_REQUIRED,
        {
            "tool_call_id": "tc-kinds-1",
            "questions": [{"prompt": "Which one?"}],
            "context": "",
        },
    )
    question = await _get_inbox_item_for_event(session_store, question_event)

    answerable = await client.get(
        inbox_path(query="kind=input_required&kind=governance_gate"),
        headers=headers_for(token),
    )
    news = await client.get(
        inbox_path(query="kind=task_complete&kind=progress_checkin"),
        headers=headers_for(token),
    )

    assert answerable.status_code == 200, answerable.text
    assert [row["id"] for row in answerable.json()["items"]] == [question.id]
    assert news.status_code == 200, news.text
    assert [row["id"] for row in news.json()["items"]] == [update.id]


async def test_list_rejects_an_unknown_kind(
    client,
    session_factory,
    session_store,
):
    """Same reason an unknown status is rejected: an empty list would
    read as an empty inbox."""
    _, _, token, _ = await _create_user_token_session(
        session_factory,
        session_store,
    )

    response = await client.get(
        inbox_path(query="kind=nonsense"),
        headers=headers_for(token),
    )

    assert response.status_code == 422, response.text
    assert "nonsense" in response.text


async def test_list_rejects_an_unknown_status(
    client,
    session_factory,
    session_store,
):
    """Silently returning nothing would read as an empty inbox."""
    _, _, token, _ = await _create_user_token_session(
        session_factory,
        session_store,
    )

    response = await client.get(
        inbox_path(query="status=pending&status=nonsense"),
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 422, response.text
    assert "nonsense" in response.text


async def test_delete_other_users_item_returns_404(
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

    response = await client.delete(
        inbox_path(f"/{item.id}"),
        headers={"Authorization": f"Bearer {other_token}"},
    )

    assert response.status_code == 404


def headers_for(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _item_on_another_agent(session_factory, session_store, org_id, user_id):
    """One inbox item for the same person, raised by a different agent."""
    other_session = await session_store.create_session(
        user_id=user_id,
        org_id=org_id,
        agent_id=OTHER_AGENT_ID,
    )
    return await _emit_task_complete(session_store, other_session.id)


async def test_list_excludes_items_raised_by_another_agent(
    client,
    session_factory,
    session_store,
):
    """Each agent's inbox is its own.

    One person talking to several agents has items from all of them; the
    inbox they are looking at must show only the agent whose app they
    opened, the same way the session list already does.
    """
    org_id, user_id, token, session = await _create_user_token_session(
        session_factory,
        session_store,
    )
    mine = await _emit_task_complete(session_store, session.id)
    theirs = await _item_on_another_agent(
        session_factory, session_store, org_id, user_id,
    )

    response = await client.get(
        inbox_path(),
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200, response.text
    returned = {row["id"] for row in response.json()["items"]}
    assert returned == {mine.id}
    assert theirs.id not in returned


@pytest.mark.parametrize(
    ("method", "suffix", "body"),
    [
        ("get", "", None),
        ("post", "/read", None),
        ("post", "/ack", None),
        ("delete", "", None),
        ("post", "/respond", {"completed": True}),
    ],
    ids=["get", "read", "ack", "delete", "respond"],
)
async def test_item_of_another_agent_is_not_found(
    client,
    session_factory,
    session_store,
    method,
    suffix,
    body,
):
    org_id, user_id, token, _ = await _create_user_token_session(
        session_factory,
        session_store,
    )
    theirs = await _item_on_another_agent(
        session_factory, session_store, org_id, user_id,
    )

    kwargs = {"headers": {"Authorization": f"Bearer {token}"}}
    if body is not None:
        kwargs["json"] = body
    response = await getattr(client, method)(
        inbox_path(f"/{theirs.id}{suffix}"), **kwargs,
    )

    assert response.status_code == 404, response.text


async def test_inbox_requires_an_agent(
    client,
    session_factory,
    session_store,
):
    """Without an agent there is no inbox to show — say so, don't guess."""
    _, _, token, _ = await _create_user_token_session(
        session_factory,
        session_store,
    )

    response = await client.get(
        inbox_path(agent_id=None),
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 400, response.text


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
        inbox_path(f"/{item.id}/respond"),
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


async def test_respond_action_required_records_completion_and_wakes_session(
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
        EventType.INBOX_ACTION_REQUIRED,
        {
            "title": "Sign in required",
            "instructions": "Open the browser session and complete sign-in.",
            "context": "The browser is showing the login page.",
            "action_type": "browser",
            "target": "browser",
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
        inbox_path(f"/{item.id}/respond"),
        json={"completed": True},
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
    assert rows[0].data["source"] == "inbox_action_completed"
    assert rows[0].data["action_type"] == "browser"
    assert "completed" in rows[0].data["content"].lower()


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
        inbox_path(f"/{item.id}/respond"),
        json={"decision": "approve"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 409


def _finite_event_source(stop_on: str):
    """Turn the route's endless SSE generator into one that stops.

    The test client drains a response fully before handing it back, and
    an inbox stream is designed never to end.
    """

    def factory(generator):
        async def limited_stream():
            async for event in generator:
                if "event" not in event:
                    continue
                yield f"event: {event['event']}\n"
                yield f"data: {event['data']}\n\n"
                if event["event"] == stop_on:
                    break

        return StreamingResponse(
            limited_stream(),
            media_type="text/event-stream",
        )

    return factory


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

    monkeypatch.setattr(
        "surogates.api.routes.inbox.EventSourceResponse",
        _finite_event_source("item"),
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
                inbox_path("/stream"),
                headers=headers,
            )
    finally:
        await emitter

    assert response.status_code == 200, response.text
    assert "event: snapshot" in response.text
    assert "event: item" in response.text
    assert "task_complete" in response.text


async def test_sse_snapshot_counts_only_pending_unread(
    client,
    app,
    session_factory,
    monkeypatch,
):
    """The badge derives from pending items, so the snapshot it seeds must
    agree — counting unread items the user already dealt with made the
    number jump the moment the stream connected."""
    store = app.state.session_store
    _, user_id, token, session = await _create_user_token_session(
        session_factory,
        store,
    )
    headers = {"Authorization": f"Bearer {token}"}

    still_pending = await _emit_task_complete(store, session.id)
    already_handled = await _emit_task_complete(store, session.id)
    await store.set_inbox_status(
        item_id=already_handled.id,
        user_id=user_id,
        agent_id=AGENT_ID,
        new_status="acknowledged",
    )

    monkeypatch.setattr(
        "surogates.api.routes.inbox.EventSourceResponse",
        _finite_event_source("snapshot"),
        raising=False,
    )

    async with asyncio.timeout(5):
        response = await client.get(inbox_path("/stream"), headers=headers)

    assert response.status_code == 200, response.text
    snapshot = json.loads(
        next(
            line[len("data: "):]
            for line in response.text.splitlines()
            if line.startswith("data: ")
        )
    )
    assert snapshot["unread_ids"] == [still_pending.id]


async def test_sse_snapshot_excludes_another_agents_items(
    client,
    app,
    session_factory,
    monkeypatch,
):
    """The stream seeds the unread badge, so it is scoped like the list.

    Items published to this person's channel by their OTHER agents land
    on the same Redis channel — the snapshot must not count them, or the
    badge shows a number the list cannot explain.
    """
    store = app.state.session_store
    org_id, user_id, token, session = await _create_user_token_session(
        session_factory,
        store,
    )
    mine = await _emit_task_complete(store, session.id)
    theirs = await _item_on_another_agent(
        session_factory, store, org_id, user_id,
    )

    monkeypatch.setattr(
        "surogates.api.routes.inbox.EventSourceResponse",
        _finite_event_source("snapshot"),
        raising=False,
    )

    async with asyncio.timeout(5):
        response = await client.get(inbox_path("/stream"), headers=headers_for(token))

    assert response.status_code == 200, response.text
    snapshot = json.loads(
        next(
            line[len("data: "):]
            for line in response.text.splitlines()
            if line.startswith("data: ")
        )
    )
    assert snapshot["unread_ids"] == [mine.id]
    assert theirs.id not in snapshot["unread_ids"]


async def test_auth_config_returns_current_agent_id(client):
    # The app fixture seeds the runtime-config cache so
    # agent_runtime_context_dep resolves without a live management plane.
    response = await client.get(f"/v1/auth/config?agent_id={AGENT_ID}")

    assert response.status_code == 200, response.text
    assert response.json()["agent_id"] == AGENT_ID


async def test_ask_user_question_response_flips_inbox_to_responded(
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
            "tool_call_id": "tc-clr-1",
            "questions": [{"prompt": "Which color?"}],
            "context": "",
        },
    )
    item = await _get_inbox_item_for_event(session_store, event_id)
    assert item.status == "pending"

    response = await client.post(
        f"/v1/sessions/{session.id}/ask_user_question/tc-clr-1/respond",
        json={"responses": [{"question": "Which color?", "answer": "blue"}]},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 201, response.text
    async with session_store._sf() as db:
        updated = await db.get(InboxItem, item.id)

    assert updated is not None
    assert updated.status == "responded"
    assert updated.responded_at is not None
