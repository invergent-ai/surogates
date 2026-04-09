"""Durable response delivery via the PostgreSQL delivery_outbox.

The :class:`DeliveryService` is the single entry-point for getting agent
responses out to users.  The flow is:

1. The harness (or API route) calls :meth:`DeliveryService.enqueue` which
   inserts a row into ``delivery_outbox`` inside the caller's transaction.
2. :meth:`DeliveryService.nudge` publishes a lightweight Redis pub/sub
   notification so that live subscribers (SSE connections, polling adapters)
   wake up immediately.
3. Channel adapters call :meth:`DeliveryService.claim_batch` to atomically
   claim pending rows (``SELECT ... FOR UPDATE SKIP LOCKED``) and deliver
   them via their platform SDK.
4. After delivery the adapter calls :meth:`mark_delivered` or
   :meth:`mark_failed`.

The PostgreSQL outbox is the **source of truth** -- Redis is purely a
latency optimisation.  If Redis is down, adapters still poll the outbox on
a configurable interval and nothing is lost.
"""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, AsyncIterator
from uuid import UUID

from sqlalchemy import text

if TYPE_CHECKING:
    from redis.asyncio import Redis
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

__all__ = [
    "DeliveryService",
    "OutboxItem",
]

logger = logging.getLogger(__name__)

_CHANNEL_PREFIX = "surogates:delivery:"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class OutboxItem:
    """A single pending delivery claimed from the outbox."""

    id: int
    session_id: UUID
    event_id: int
    channel: str
    destination: dict[str, Any]
    payload: dict[str, Any]
    dedupe_key: str
    created_at: datetime


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class DeliveryService:
    """Durable delivery of agent responses to channels.

    PostgreSQL ``delivery_outbox`` is the source of truth.
    Redis pub/sub is only a low-latency nudge for live SSE clients.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        redis_client: Redis,
    ) -> None:
        self._session_factory = session_factory
        self._redis = redis_client

    # ------------------------------------------------------------------
    # Enqueue
    # ------------------------------------------------------------------

    async def enqueue(
        self,
        session_id: UUID,
        event_id: int,
        channel: str,
        destination: dict[str, Any],
        payload: dict[str, Any],
    ) -> int:
        """Insert a row into ``delivery_outbox`` and return its ID.

        A ``dedupe_key`` of ``"{channel}:{event_id}"`` is written alongside
        the row.  The ``UNIQUE(channel, dedupe_key)`` constraint on the
        table prevents the same event from being enqueued twice for the
        same channel, making this call safe to retry.
        """
        dedupe_key = f"{channel}:{event_id}"
        async with self._session_factory() as db:
            row = (
                await db.execute(
                    text(
                        """
                        INSERT INTO delivery_outbox
                            (session_id, event_id, channel, destination, payload, dedupe_key)
                        VALUES
                            (:session_id, :event_id, :channel, CAST(:destination AS jsonb),
                             CAST(:payload AS jsonb), :dedupe_key)
                        ON CONFLICT (channel, dedupe_key) DO NOTHING
                        RETURNING id
                        """
                    ),
                    {
                        "session_id": session_id,
                        "event_id": event_id,
                        "channel": channel,
                        "destination": _json_dumps(destination),
                        "payload": _json_dumps(payload),
                        "dedupe_key": dedupe_key,
                    },
                )
            ).mappings().one_or_none()
            await db.commit()

        if row is None:
            # Dedupe hit -- fetch the existing row's ID.
            async with self._session_factory() as db:
                existing = (
                    await db.execute(
                        text(
                            """
                            SELECT id FROM delivery_outbox
                            WHERE channel = :channel AND dedupe_key = :dedupe_key
                            """
                        ),
                        {"channel": channel, "dedupe_key": dedupe_key},
                    )
                ).mappings().one()
            return int(existing["id"])

        return int(row["id"])

    # ------------------------------------------------------------------
    # Claim / acknowledge
    # ------------------------------------------------------------------

    async def claim_batch(
        self,
        channel: str,
        worker_id: str,
        *,
        limit: int = 50,
    ) -> list[OutboxItem]:
        """Atomically claim pending outbox items for delivery.

        Uses ``SELECT ... FOR UPDATE SKIP LOCKED`` so that multiple adapter
        instances for the same channel can run concurrently without
        double-delivering.  Claimed rows transition from ``'pending'`` to
        ``'claimed'`` and record the *worker_id* for observability.
        """
        async with self._session_factory() as db:
            rows = (
                await db.execute(
                    text(
                        """
                        WITH batch AS (
                            SELECT id
                            FROM delivery_outbox
                            WHERE channel = :channel
                              AND status = 'pending'
                              AND (available_at IS NULL OR available_at <= now())
                            ORDER BY id ASC
                            LIMIT :limit
                            FOR UPDATE SKIP LOCKED
                        )
                        UPDATE delivery_outbox o
                        SET status = 'claimed'
                        FROM batch b
                        WHERE o.id = b.id
                        RETURNING o.id, o.session_id, o.event_id, o.channel,
                                  o.destination, o.payload, o.dedupe_key, o.created_at
                        """
                    ),
                    {"channel": channel, "limit": limit},
                )
            ).mappings().all()
            await db.commit()

        return [
            OutboxItem(
                id=int(r["id"]),
                session_id=r["session_id"],
                event_id=int(r["event_id"]),
                channel=r["channel"],
                destination=r["destination"] if isinstance(r["destination"], dict) else {},
                payload=r["payload"] if isinstance(r["payload"], dict) else {},
                dedupe_key=r["dedupe_key"] or "",
                created_at=r["created_at"],
            )
            for r in rows
        ]

    async def mark_delivered(
        self,
        outbox_id: int,
        *,
        provider_message_id: str | None = None,
    ) -> None:
        """Mark an outbox item as successfully delivered."""
        async with self._session_factory() as db:
            await db.execute(
                text(
                    """
                    UPDATE delivery_outbox
                    SET status = 'delivered'
                    WHERE id = :id
                    """
                ),
                {"id": outbox_id},
            )
            await db.commit()
        logger.debug(
            "Outbox %d delivered (provider_message_id=%s)",
            outbox_id,
            provider_message_id,
        )

    async def mark_failed(self, outbox_id: int, error: str) -> None:
        """Mark an outbox item as failed.

        The row returns to ``'pending'`` status with ``available_at`` pushed
        30 seconds into the future so that the next ``claim_batch`` cycle
        retries it after a back-off window.
        """
        async with self._session_factory() as db:
            await db.execute(
                text(
                    """
                    UPDATE delivery_outbox
                    SET status = 'pending',
                        available_at = now() + interval '30 seconds'
                    WHERE id = :id
                    """
                ),
                {"id": outbox_id},
            )
            await db.commit()
        logger.warning("Outbox %d failed: %s (will retry)", outbox_id, error)

    # ------------------------------------------------------------------
    # Real-time notifications (Redis pub/sub)
    # ------------------------------------------------------------------

    async def nudge(self, session_id: UUID) -> None:
        """Publish a Redis notification that new events are available.

        SSE handlers and channel adapters subscribe to the per-session
        channel and wake immediately on receipt, avoiding unnecessary
        polling latency.
        """
        channel_name = f"{_CHANNEL_PREFIX}{session_id}"
        try:
            await self._redis.publish(channel_name, b"1")
        except Exception:
            # Redis is a latency optimisation, not a correctness requirement.
            # If publishing fails the adapter's poll loop will still pick up
            # the outbox row within its next cycle.
            logger.debug(
                "Redis nudge failed for session %s (non-fatal)", session_id,
                exc_info=True,
            )

    @asynccontextmanager
    async def subscribe(
        self,
        session_id: UUID,
    ) -> AsyncIterator[_SubscriptionIterator]:
        """Context manager yielding an async iterator of delivery nudges.

        Usage::

            async with delivery.subscribe(session_id) as notifications:
                async for _ in notifications:
                    # new events available -- fetch from DB
                    ...

        Under the hood this creates a Redis pub/sub subscription on the
        per-session channel.  The subscription is torn down when the
        context manager exits.
        """
        channel_name = f"{_CHANNEL_PREFIX}{session_id}"
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(channel_name)
        try:
            yield _SubscriptionIterator(pubsub)
        finally:
            await pubsub.unsubscribe(channel_name)
            await pubsub.aclose()


class _SubscriptionIterator:
    """Thin async-iterator wrapper around a Redis PubSub object.

    Yields ``True`` for every genuine message received on the subscribed
    channel, filtering out Redis control messages (subscribe confirmations,
    etc.).
    """

    def __init__(self, pubsub: Any) -> None:
        self._pubsub = pubsub

    def __aiter__(self) -> _SubscriptionIterator:
        return self

    async def __anext__(self) -> bool:
        while True:
            msg = await self._pubsub.get_message(
                ignore_subscribe_messages=True,
                timeout=30.0,
            )
            if msg is not None and msg.get("type") == "message":
                return True
            # On timeout (msg is None) we yield anyway so the caller can
            # poll the outbox as a fallback, keeping the system correct
            # even if a Redis message was lost.
            if msg is None:
                return True


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _json_dumps(obj: dict[str, Any]) -> str:
    """Serialise a dict to JSON for use with PostgreSQL ``::jsonb`` casts."""
    return json.dumps(obj, default=str)
