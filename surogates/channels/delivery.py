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

from sqlalchemy import select, text, update

from surogates.db.models import DeliveryOutbox

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
        self._sf = session_factory
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

        Uses ORM for the insert. The ``UNIQUE(channel, dedupe_key)``
        constraint prevents double-enqueue; on conflict the existing
        row's ID is returned.
        """
        dedupe_key = f"{channel}:{event_id}"
        row = DeliveryOutbox(
            session_id=session_id,
            event_id=event_id,
            channel=channel,
            destination=destination,
            payload=payload,
            dedupe_key=dedupe_key,
            status="pending",
        )

        async with self._sf() as db:
            # Try ORM insert; catch unique violation.
            try:
                db.add(row)
                await db.flush()
                outbox_id = row.id
                await db.commit()
                return int(outbox_id)
            except Exception:
                await db.rollback()

        # Dedupe hit — fetch existing row.
        async with self._sf() as db:
            result = await db.execute(
                select(DeliveryOutbox.id).where(
                    DeliveryOutbox.channel == channel,
                    DeliveryOutbox.dedupe_key == dedupe_key,
                )
            )
            existing_id = result.scalar_one()
        return int(existing_id)

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

        Uses ``SELECT ... FOR UPDATE SKIP LOCKED`` (raw SQL — ORM can't
        express this) so that multiple adapter instances for the same
        channel can run concurrently without double-delivering.
        """
        async with self._sf() as db:
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
        async with self._sf() as db:
            await db.execute(
                update(DeliveryOutbox)
                .where(DeliveryOutbox.id == outbox_id)
                .values(status="delivered")
            )
            await db.commit()
        logger.debug(
            "Outbox %d delivered (provider_message_id=%s)",
            outbox_id,
            provider_message_id,
        )

    async def mark_failed(self, outbox_id: int, error: str) -> None:
        """Mark an outbox item as failed.

        The row returns to ``'pending'`` status with ``available_at``
        pushed 30 seconds into the future for retry backoff.
        """
        async with self._sf() as db:
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
        """Publish a Redis notification that new events are available."""
        channel_name = f"{_CHANNEL_PREFIX}{session_id}"
        try:
            await self._redis.publish(channel_name, b"1")
        except Exception:
            logger.debug(
                "Redis nudge failed for session %s (non-fatal)", session_id,
                exc_info=True,
            )

    @asynccontextmanager
    async def subscribe(
        self,
        session_id: UUID,
    ) -> AsyncIterator[_SubscriptionIterator]:
        """Context manager yielding an async iterator of delivery nudges."""
        channel_name = f"{_CHANNEL_PREFIX}{session_id}"
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(channel_name)
        try:
            yield _SubscriptionIterator(pubsub)
        finally:
            await pubsub.unsubscribe(channel_name)
            await pubsub.aclose()


class _SubscriptionIterator:
    """Thin async-iterator wrapper around a Redis PubSub object."""

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
            if msg is None:
                return True
