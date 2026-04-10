"""Integration tests for DeliveryService against real PostgreSQL + Redis."""

from __future__ import annotations

import asyncio
import uuid

import pytest
import pytest_asyncio

from surogates.channels.delivery import DeliveryService
from surogates.session.events import EventType

from .conftest import create_org, create_user

pytestmark = pytest.mark.asyncio(loop_scope="session")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(loop_scope="session")
async def delivery_service(session_factory, redis_client):
    """DeliveryService backed by test containers."""
    return DeliveryService(session_factory, redis_client)


async def _create_session_and_event(session_store, session_factory):
    """Helper: create an org, user, session, and event. Return IDs."""
    from surogates.session.store import SessionStore

    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    session = await session_store.create_session(
        user_id=user_id, org_id=org_id
    )
    event_id = await session_store.emit_event(
        session.id, EventType.USER_MESSAGE, {"content": "test"}
    )
    return session.id, event_id


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_enqueue_and_claim(delivery_service, session_store, session_factory):
    """Enqueue a delivery, claim a batch, verify the item is returned."""
    session_id, event_id = await _create_session_and_event(
        session_store, session_factory
    )

    outbox_id = await delivery_service.enqueue(
        session_id=session_id,
        event_id=event_id,
        channel="web",
        destination={"user_id": "u1"},
        payload={"text": "Hello"},
    )
    assert outbox_id > 0

    batch = await delivery_service.claim_batch("web", "worker-1", limit=10)
    assert len(batch) >= 1
    item = next(i for i in batch if i.id == outbox_id)
    assert item.session_id == session_id
    assert item.event_id == event_id
    assert item.channel == "web"
    assert item.payload == {"text": "Hello"}


async def test_dedupe_key_prevents_duplicates(
    delivery_service, session_store, session_factory
):
    """Enqueuing the same event twice for the same channel only creates one row."""
    session_id, event_id = await _create_session_and_event(
        session_store, session_factory
    )

    id1 = await delivery_service.enqueue(
        session_id=session_id,
        event_id=event_id,
        channel="slack",
        destination={"channel": "#general"},
        payload={"text": "dup test"},
    )

    id2 = await delivery_service.enqueue(
        session_id=session_id,
        event_id=event_id,
        channel="slack",
        destination={"channel": "#general"},
        payload={"text": "dup test again"},
    )

    # Both calls should return the same outbox ID
    assert id1 == id2


async def test_mark_delivered(delivery_service, session_store, session_factory):
    """After mark_delivered, the item is no longer claimable."""
    session_id, event_id = await _create_session_and_event(
        session_store, session_factory
    )

    outbox_id = await delivery_service.enqueue(
        session_id=session_id,
        event_id=event_id,
        channel="email",
        destination={"to": "user@test.com"},
        payload={"subject": "test"},
    )

    # Claim it
    batch = await delivery_service.claim_batch("email", "worker-1")
    assert any(i.id == outbox_id for i in batch)

    # Mark delivered
    await delivery_service.mark_delivered(outbox_id, provider_message_id="ext-123")

    # Claiming again should NOT return this item
    batch2 = await delivery_service.claim_batch("email", "worker-2")
    assert not any(i.id == outbox_id for i in batch2)


async def test_mark_failed_makes_retryable(
    delivery_service, session_store, session_factory
):
    """After mark_failed, the item returns to pending with a future available_at."""
    session_id, event_id = await _create_session_and_event(
        session_store, session_factory
    )

    outbox_id = await delivery_service.enqueue(
        session_id=session_id,
        event_id=event_id,
        channel="webhook",
        destination={"url": "https://example.com/hook"},
        payload={"event": "test"},
    )

    # Claim and fail
    batch = await delivery_service.claim_batch("webhook", "worker-1")
    assert any(i.id == outbox_id for i in batch)

    await delivery_service.mark_failed(outbox_id, "Connection refused")

    # The item should be back to 'pending' status but with available_at in
    # the future, so an immediate claim_batch should NOT return it (the
    # available_at is 30 seconds out).
    batch2 = await delivery_service.claim_batch("webhook", "worker-2")
    assert not any(i.id == outbox_id for i in batch2)

    # Verify the row still exists and is in pending status by checking
    # with raw SQL
    from sqlalchemy import text

    async with delivery_service._sf() as db:
        row = (
            await db.execute(
                text("SELECT status FROM delivery_outbox WHERE id = :id"),
                {"id": outbox_id},
            )
        ).mappings().one()
    assert row["status"] == "pending"


async def test_nudge_publishes_to_redis(
    delivery_service, session_store, session_factory, redis_client
):
    """nudge() publishes a message to the per-session Redis channel."""
    session_id, event_id = await _create_session_and_event(
        session_store, session_factory
    )

    # Subscribe to the session's delivery channel
    channel_name = f"surogates:delivery:{session_id}"
    pubsub = redis_client.pubsub()
    await pubsub.subscribe(channel_name)

    # Consume the subscribe confirmation message
    msg = await pubsub.get_message(timeout=2.0)
    assert msg is not None and msg["type"] == "subscribe"

    # Nudge
    await delivery_service.nudge(session_id)

    # Should receive the notification
    msg = await pubsub.get_message(timeout=2.0)
    assert msg is not None
    assert msg["type"] == "message"
    assert msg["data"] == b"1"

    await pubsub.unsubscribe(channel_name)
    await pubsub.aclose()
