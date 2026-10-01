"""The device operation journal: what workers asked devices to run, and what came back.

A worker records an operation, announces ``op:<id>`` on the device's control
channel, and waits on ``surogates:device_operation:{id}`` for the API to say
its outcome was recorded.  Postgres holds the truth: a lost announcement is
caught when the device's connection next reconciles, and a lost completion
notice, or a Redis outage, by the worker's periodic recheck.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
from collections.abc import Collection
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from surogates.db.models import Device, DeviceOperation
from surogates.devices.presence import control_channel
from surogates.devices.store import REVOKED_OUTCOME
from surogates.devices.workspace import DeviceOperationError

logger = logging.getLogger(__name__)

RECHECK_INTERVAL_S = 5.0


def operation_channel(operation_id: UUID) -> str:
    """Where the API announces that an operation's outcome was recorded."""
    return f"surogates:device_operation:{operation_id}"


class OperationConflict(RuntimeError):
    """The same invocation and ordinal were recorded with a different request."""


@dataclass(frozen=True, slots=True)
class OperationRequest:
    device_id: UUID
    root_session_id: UUID
    calling_session_id: UUID
    invocation_id: str
    ordinal: int
    kind: str
    args: dict[str, Any]
    # The execution generation: recorded, never part of the digest.
    lease_token: str | None = None

    def __post_init__(self) -> None:
        if not self.invocation_id:
            raise ValueError("An operation needs the invocation it belongs to")

    @property
    def digest(self) -> str:
        """SHA-256 of the immutable request."""
        body = {
            "device_id": str(self.device_id),
            "root_session_id": str(self.root_session_id),
            "calling_session_id": str(self.calling_session_id),
            "invocation_id": self.invocation_id,
            "ordinal": self.ordinal,
            "kind": self.kind,
            "args": self.args,
        }
        canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class OpenOperation:
    """An operation with no outcome yet, as the link delivers it."""

    id: UUID
    root_session_id: UUID
    calling_session_id: UUID
    invocation_id: str
    ordinal: int
    kind: str
    args: dict[str, Any]
    digest: str

    def frame(self) -> dict[str, Any]:
        return {
            "type": "op",
            "id": str(self.id),
            "session_id": str(self.root_session_id),
            "calling_session_id": str(self.calling_session_id),
            "invocation_id": self.invocation_id,
            "ordinal": self.ordinal,
            "kind": self.kind,
            "args": self.args,
            "digest": self.digest,
        }


class DeviceOperations:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        redis: Redis,
        *,
        recheck_interval_s: float = RECHECK_INTERVAL_S,
    ) -> None:
        self._sf = session_factory
        self._redis = redis
        self._recheck_interval_s = recheck_interval_s

    # -- the worker's side -----------------------------------------------

    async def run(self, request: OperationRequest) -> dict[str, Any]:
        """Run *request* on its device and return the outcome, however long that takes.

        A request already recorded under the same invocation and ordinal is
        joined, never repeated: its outcome comes back when the device reports
        it, or at once if it already has.
        """
        operation_id, outcome = await self._record(request)
        if outcome is not None:
            return outcome
        await self._announce(control_channel(request.device_id), f"op:{operation_id}")
        # ponytail: one pub/sub connection per waiting operation; share one
        # subscriber per worker when waits number in the thousands.
        while True:
            pubsub = self._redis.pubsub()
            try:
                await pubsub.subscribe(operation_channel(operation_id))
                while True:
                    outcome = await self._outcome(operation_id)
                    if outcome is not None:
                        return outcome
                    await pubsub.get_message(
                        ignore_subscribe_messages=True, timeout=self._recheck_interval_s,
                    )
            except RedisError:
                # Postgres is the authority: keep checking it while Redis recovers.
                logger.warning("waiting for operation %s without Redis", operation_id, exc_info=True)
                outcome = await self._outcome(operation_id)
                if outcome is not None:
                    return outcome
                await asyncio.sleep(self._recheck_interval_s)
            finally:
                with contextlib.suppress(Exception):
                    await pubsub.aclose()

    async def _announce(self, channel: str, message: str) -> None:
        """Publish a wake-up, best effort: the next reconcile or recheck covers a lost one."""
        try:
            await self._redis.publish(channel, message)
        except Exception:
            logger.warning("could not publish %s on %s", message, channel, exc_info=True)

    async def _record(self, request: OperationRequest) -> tuple[UUID, dict[str, Any] | None]:
        digest = request.digest
        async with self._sf() as db:
            inserted = (await db.execute(
                pg_insert(DeviceOperation)
                .values(
                    device_id=request.device_id,
                    root_session_id=request.root_session_id,
                    calling_session_id=request.calling_session_id,
                    invocation_id=request.invocation_id,
                    ordinal=request.ordinal,
                    kind=request.kind,
                    args=request.args,
                    digest=digest,
                    lease_token=request.lease_token,
                )
                .on_conflict_do_nothing(constraint="uq_device_operations_invocation")
                .returning(DeviceOperation.id)
            )).scalar_one_or_none()
            await db.commit()
            if inserted is not None:
                return inserted, None
            row = (await db.execute(
                select(DeviceOperation.id, DeviceOperation.digest, DeviceOperation.outcome)
                .where(
                    DeviceOperation.calling_session_id == request.calling_session_id,
                    DeviceOperation.invocation_id == request.invocation_id,
                    DeviceOperation.ordinal == request.ordinal,
                )
            )).one()
        if row.digest != digest:
            raise OperationConflict(
                f"Operation {request.ordinal} of {request.invocation_id} was recorded "
                "with a different request"
            )
        return row.id, row.outcome

    async def _outcome(self, operation_id: UUID) -> dict[str, Any] | None:
        """The recorded outcome, or None while the device has not answered.

        An operation still open on a revoked device is failed here, so a
        request recorded after the revocation, or one that raced with it,
        cannot wait forever.
        """
        async with self._sf() as db:
            row = (await db.execute(
                select(DeviceOperation.outcome, Device.revoked_at)
                .join(Device, Device.id == DeviceOperation.device_id)
                .where(DeviceOperation.id == operation_id)
            )).one_or_none()
            if row is None:
                # Deleted with its device: nothing will ever answer.
                raise DeviceOperationError("This computer was removed")
            if row.outcome is not None or row.revoked_at is None:
                return row.outcome
            failed = (await db.execute(
                update(DeviceOperation)
                .where(DeviceOperation.id == operation_id, DeviceOperation.completed_at.is_(None))
                .values(outcome=REVOKED_OUTCOME, completed_at=func.now())
                .returning(DeviceOperation.id)
            )).scalar_one_or_none()
            await db.commit()
        if failed is None:
            # The device's own outcome landed first: return that one.
            return await self._outcome(operation_id)
        return REVOKED_OUTCOME

    # -- the link's side -------------------------------------------------

    async def _current(self, db: AsyncSession, device_id: UUID, generation: int) -> bool:
        """Whether *generation* is the device's live credential generation."""
        row = (await db.execute(
            select(Device.credential_generation, Device.revoked_at).where(Device.id == device_id)
        )).one_or_none()
        return row is not None and row.revoked_at is None and row.credential_generation == generation

    async def pending(
        self,
        device_id: UUID,
        generation: int,
        *,
        exclude: Collection[UUID] = frozenset(),
        limit: int = 100,
    ) -> list[OpenOperation]:
        """The device's operations with no outcome yet, oldest first.

        Only for the device's current credentials: a connection still open
        under rotated-out or revoked credentials is offered nothing.
        """
        async with self._sf() as db:
            if not await self._current(db, device_id, generation):
                return []
            query = select(DeviceOperation).where(
                DeviceOperation.device_id == device_id,
                DeviceOperation.completed_at.is_(None),
            )
            if exclude:
                query = query.where(DeviceOperation.id.not_in(list(exclude)))
            rows = (await db.execute(
                query.order_by(DeviceOperation.created_at, DeviceOperation.ordinal).limit(limit)
            )).scalars().all()
        return [
            OpenOperation(
                id=row.id,
                root_session_id=row.root_session_id,
                calling_session_id=row.calling_session_id,
                invocation_id=row.invocation_id,
                ordinal=row.ordinal,
                kind=row.kind,
                args=row.args,
                digest=row.digest,
            )
            for row in rows
        ]

    async def complete(
        self,
        device_id: UUID,
        generation: int,
        operation_id: UUID,
        digest: str,
        outcome: dict[str, Any],
    ) -> Literal["completed", "duplicate", "rejected", "stale"]:
        """Record the device's outcome for one of its own operations.

        "duplicate": already recorded.  "rejected": unknown, another device's,
        or asked for with a different digest.  "stale": the reply came under
        rotated-out or revoked credentials.
        """
        async with self._sf() as db:
            if not await self._current(db, device_id, generation):
                return "stale"
            completed = (await db.execute(
                update(DeviceOperation)
                .where(
                    DeviceOperation.id == operation_id,
                    DeviceOperation.device_id == device_id,
                    DeviceOperation.digest == digest,
                    DeviceOperation.completed_at.is_(None),
                )
                .values(outcome=outcome, completed_at=func.now())
                .returning(DeviceOperation.id)
            )).scalar_one_or_none()
            await db.commit()
            row = None
            if completed is None:
                row = (await db.execute(
                    select(DeviceOperation.device_id, DeviceOperation.digest)
                    .where(DeviceOperation.id == operation_id)
                )).one_or_none()
        if completed is None and (row is None or row.device_id != device_id or row.digest != digest):
            return "rejected"
        # Announced for a duplicate too: its worker may have missed the first.
        await self._announce(operation_channel(operation_id), "completed")
        return "completed" if completed is not None else "duplicate"


class JournalRunner:
    """The operations of one tool call, numbered in the order its handler asks for them."""

    def __init__(
        self,
        operations: DeviceOperations,
        *,
        device_id: UUID,
        root_session_id: UUID,
        calling_session_id: UUID,
        invocation_id: str,
        lease_token: str | None = None,
    ) -> None:
        self._operations = operations
        self._device_id = device_id
        self._root_session_id = root_session_id
        self._calling_session_id = calling_session_id
        self._invocation_id = invocation_id
        self._lease_token = lease_token
        self._ordinal = 0

    async def run(self, kind: str, args: dict[str, Any]) -> dict[str, Any]:
        self._ordinal += 1
        return await self._operations.run(OperationRequest(
            device_id=self._device_id,
            root_session_id=self._root_session_id,
            calling_session_id=self._calling_session_id,
            invocation_id=self._invocation_id,
            ordinal=self._ordinal,
            kind=kind,
            args=args,
            lease_token=self._lease_token,
        ))
