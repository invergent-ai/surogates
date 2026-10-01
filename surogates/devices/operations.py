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
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, TypeVar
from uuid import UUID

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import exists, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from surogates.db.models import Device, DeviceOperation
from surogates.db.models import Session as SessionRow
from surogates.devices.binding import BIND, binding_of, device_of
from surogates.devices.presence import DevicePresence, control_channel
from surogates.devices.store import REVOKED_OUTCOME
from surogates.devices.workspace import DeviceOperationError, is_well_formed
from surogates.runtime.turn_slots import turn_waiting

if TYPE_CHECKING:
    from surogates.devices.waits import DeviceWaitNotice

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

RECHECK_INTERVAL_S = 5.0

# How long a device operation may take before its turn gives its worker slots
# back: an operation the computer answers at once must not churn them.
WAIT_GRACE_S = 2.0

# How many sessions may wait on one computer at once.  Past it a new session's
# local operation fails at once, telling the user, rather than park another
# coroutine on the worker.
# ponytail: a constant; make it a worker setting when a deployment needs another value.
PARKED_SESSIONS_PER_DEVICE = 20


def operation_channel(operation_id: UUID) -> str:
    """Where the API announces that an operation's outcome was recorded."""
    return f"surogates:device_operation:{operation_id}"


def _database_unavailable(exc: Exception) -> bool:
    """Whether *exc* is the database being briefly out of reach, not a bug or a refusal.

    A failover, a reset connection or an exhausted pool is.  An integrity or
    programming error is not: asking again gets the same answer.
    """
    if isinstance(exc, DBAPIError):
        # A failover's shutdown arrives as the generic DBAPIError, with its connection invalidated.
        return isinstance(exc, (OperationalError, InterfaceError)) or exc.connection_invalidated
    return isinstance(exc, (PoolTimeoutError, ConnectionError, TimeoutError))


class OperationConflict(RuntimeError):
    """The same invocation and ordinal were recorded with a different request."""


async def _check_session(db: AsyncSession, request: OperationRequest, device: Any) -> None:
    """Refuse a request for a session that is not this device's, or not bound to it yet.

    The root session must name the device and belong to the device's user and
    agent; the calling session must be the root, or a session created under it.
    """
    rows = (await db.execute(
        select(SessionRow.id, SessionRow.org_id, SessionRow.agent_id, SessionRow.user_id, SessionRow.config)
        .where(SessionRow.id.in_([request.root_session_id, request.calling_session_id]))
    )).all()
    sessions = {row.id: row for row in rows}
    root = sessions.get(request.root_session_id)
    calling = sessions.get(request.calling_session_id)
    if (
        root is None
        or calling is None
        or device_of(root.config) != request.device_id
        or (root.org_id, root.agent_id, root.user_id) != (device.org_id, device.agent_id, device.user_id)
        or (
            request.calling_session_id != request.root_session_id
            and (calling.config or {}).get("sandbox_root_session_id") != str(request.root_session_id)
        )
    ):
        raise DeviceOperationError("This session does not work on this computer")
    is_binding = (
        request.calling_session_id == request.root_session_id
        and request.invocation_id == BIND
        and request.ordinal == 0
    )
    if (request.kind == BIND) != is_binding:
        raise ValueError("Only the root session's own first operation is its binding")
    if is_binding and (root.config or {}).get("sandbox_root_session_id"):
        # A session created under another works in that session's folder.
        raise ValueError("Only a root session is bound to a folder")
    if not is_binding and (await binding_of(db, request.root_session_id)).state != "bound":
        raise DeviceOperationError("This session's folder is not set up on this computer yet")


async def _refuse_when_full(db: AsyncSession, request: OperationRequest, device: Any) -> None:
    """Refuse a new operation from a session not yet waiting on a computer that has its fill.

    A session already waiting may keep asking.  A binding waits for its user,
    not for the computer, so it never counts.  An operation already recorded
    is a replay: it gets its recorded outcome, never a refusal.
    """
    already = (await db.execute(
        select(DeviceOperation.id).where(
            DeviceOperation.calling_session_id == request.calling_session_id,
            DeviceOperation.invocation_id == request.invocation_id,
            DeviceOperation.ordinal == request.ordinal,
        )
    )).scalar_one_or_none()
    if already is not None:
        return
    open_for_device = (
        DeviceOperation.device_id == request.device_id,
        DeviceOperation.completed_at.is_(None),
        DeviceOperation.kind != BIND,
    )
    others = (await db.execute(
        select(func.count(func.distinct(DeviceOperation.calling_session_id))).where(
            *open_for_device,
            DeviceOperation.calling_session_id != request.calling_session_id,
        )
    )).scalar_one()
    if others < PARKED_SESSIONS_PER_DEVICE:
        return
    mine = (await db.execute(
        select(DeviceOperation.id)
        .where(*open_for_device, DeviceOperation.calling_session_id == request.calling_session_id)
        .limit(1)
    )).scalar_one_or_none()
    if mine is None:
        raise DeviceOperationError(f"Too many sessions are waiting for {device.name}")


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
        notice: DeviceWaitNotice | None = None,
    ) -> None:
        self._sf = session_factory
        self._redis = redis
        self._recheck_interval_s = recheck_interval_s
        self._notice = notice

    # -- the worker's side -----------------------------------------------

    async def run(self, request: OperationRequest) -> dict[str, Any]:
        """Run *request* on its device and return the outcome, however long that takes.

        A request already recorded under the same invocation and ordinal is
        joined, never repeated: its outcome comes back when the device reports
        it, or at once if it already has.
        """
        operation_id, outcome = await self._while_database_recovers(
            "recording", lambda: self._record(request),
        )
        if outcome is not None:
            return outcome
        await self._announce(control_channel(request.device_id), f"op:{operation_id}")
        # One wait, never interrupted: a deadline here could cancel a query mid-flight.
        waiter = asyncio.ensure_future(self._wait_forever(operation_id))
        try:
            done, _ = await asyncio.wait({waiter}, timeout=WAIT_GRACE_S)
            if done:
                return waiter.result()
            # Still running, or the computer is away: the turn need not hold the worker meanwhile.
            async with turn_waiting():
                return await self._wait_watching(waiter, request)
        finally:
            waiter.cancel()  # a no-op once it finished; stops it if this caller is cancelled

    async def _wait_watching(self, waiter: asyncio.Future, request: OperationRequest) -> dict[str, Any]:
        """Await *waiter*, telling the session's viewers whenever its computer is away."""
        if self._notice is None:
            return await waiter
        presence = DevicePresence(self._redis)
        away = False
        try:
            while True:
                online = await self._online(presence, request.device_id)
                if online is False and not away:
                    away = True
                    await self._notice.away(request)
                elif online is True and away:
                    away = False
                    await self._notice.back(request)
                done, _ = await asyncio.wait({waiter}, timeout=self._recheck_interval_s)
                if done:
                    return waiter.result()
        finally:
            if away:
                await self._notice.back(request)

    async def _online(self, presence: DevicePresence, device_id: UUID) -> bool | None:
        """Whether a link holds the device: None when Redis cannot tell."""
        try:
            return device_id in await self._within_redis_patience(presence.online([device_id]))
        except Exception:
            return None

    async def _wait_forever(self, operation_id: UUID) -> dict[str, Any]:
        """Wait for the operation's outcome as long as it takes."""
        # ponytail: one pub/sub connection per waiting operation; share one
        # subscriber per worker when waits number in the thousands.
        while True:
            pubsub = self._redis.pubsub()
            try:
                await self._within_redis_patience(pubsub.subscribe(operation_channel(operation_id)))
                while True:
                    outcome = await self._while_database_recovers(
                        "waiting for", lambda: self._outcome(operation_id),
                    )
                    if outcome is not None:
                        return outcome
                    # It waits up to one interval for a message by itself; the
                    # bound is for a connection that never answers.
                    await self._within_redis_patience(
                        pubsub.get_message(
                            ignore_subscribe_messages=True, timeout=self._recheck_interval_s,
                        ),
                        intervals=2,
                    )
            except (RedisError, TimeoutError):
                # Postgres is the authority: keep checking it while Redis recovers.
                logger.warning("waiting for operation %s without Redis", operation_id, exc_info=True)
                outcome = await self._while_database_recovers(
                    "waiting for", lambda: self._outcome(operation_id),
                )
                if outcome is not None:
                    return outcome
                await asyncio.sleep(self._recheck_interval_s)
            finally:
                with contextlib.suppress(Exception):
                    await self._within_redis_patience(pubsub.aclose())

    async def _while_database_recovers(
        self, what: str, call: Callable[[], Awaitable[_T]],
    ) -> _T:
        """Await *call*, asking again every recheck interval while the database is unreachable.

        The device may already be running the operation, so a failed wait
        would tell the model it failed while a retry started it again.
        """
        while True:
            try:
                return await call()
            except Exception as exc:
                if not _database_unavailable(exc):
                    raise
                logger.warning("%s an operation: database unavailable (%s); retrying", what, type(exc).__name__)
                await asyncio.sleep(self._recheck_interval_s)

    async def _within_redis_patience(self, call: Awaitable[_T], *, intervals: float = 1) -> _T:
        """Await one Redis call for at most *intervals* recheck intervals.

        The worker's Redis client sets no socket timeout, so a blackholed
        Redis would otherwise hold the wait forever, with Postgres never rechecked.
        """
        async with asyncio.timeout(self._recheck_interval_s * intervals):
            return await call

    async def _announce(self, channel: str, message: str) -> None:
        """Publish a wake-up, best effort: the next reconcile or recheck covers a lost one."""
        try:
            await self._within_redis_patience(self._redis.publish(channel, message))
        except Exception:
            logger.warning("could not publish %s on %s", message, channel, exc_info=True)

    async def bind(self, *, session_id: UUID, device_id: UUID, folder: str, nonce: str) -> None:
        """Ask the device to bind a new root session to *folder*, without waiting.

        The app answers once its user has confirmed that folder under *nonce*.
        For an HTTP request, so a database outage fails it rather than holding
        it open.
        """
        operation_id, outcome = await self._record(OperationRequest(
            device_id=device_id,
            root_session_id=session_id,
            calling_session_id=session_id,
            invocation_id=BIND,
            ordinal=0,
            kind=BIND,
            args={"folder": folder, "nonce": nonce},
        ))
        if outcome is None:
            await self._announce(control_channel(device_id), f"op:{operation_id}")

    async def _record(self, request: OperationRequest) -> tuple[UUID, dict[str, Any] | None]:
        """Record *request*, or find it already recorded.

        Returns the operation's id and its outcome if it has one.  A request
        for a revoked device is recorded as already answered with the
        revocation, so the same request after a reauthorization cannot run.
        """
        if not is_well_formed(request.args):
            # Stored, it would be sent to the device as a frame the link cannot
            # encode, and every connection would end before reaching the
            # operations behind it.
            raise ValueError("Operation arguments must be valid Unicode")
        digest = request.digest
        async with self._sf() as db:
            # A shared lock on the device row, held until the insert commits: a
            # revocation's update of that row waits for it, so its cancelling
            # update sees the new operation.  Without it the insert could commit
            # just after that update and run once the device is reauthorized.
            device = (await db.execute(
                select(Device.name, Device.revoked_at, Device.org_id, Device.agent_id, Device.user_id)
                .where(Device.id == request.device_id)
                .with_for_update(read=True)
            )).one_or_none()
            if device is None:
                raise DeviceOperationError("This computer was removed")
            await _check_session(db, request, device)
            # ponytail: the device row is locked FOR SHARE, so concurrent recorders can pass the
            # count together and the limit is soft by their number; the count also includes
            # abandoned open operations, which cancellation (later work) will close.
            if request.kind != BIND and device.revoked_at is None:
                await _refuse_when_full(db, request, device)
            refused = (
                {"outcome": REVOKED_OUTCOME, "completed_at": func.now()}
                if device.revoked_at is not None
                else {}
            )
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
                    **refused,
                )
                .on_conflict_do_nothing(constraint="uq_device_operations_invocation")
                .returning(DeviceOperation.id)
            )).scalar_one_or_none()
            await db.commit()
            if inserted is not None:
                return inserted, REVOKED_OUTCOME if refused else None
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

        The credential check is part of the write, not a step before it, so a
        reauthorization or revocation cannot land between the two.
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
                    exists().where(
                        Device.id == DeviceOperation.device_id,
                        Device.credential_generation == generation,
                        Device.revoked_at.is_(None),
                    ),
                )
                .values(outcome=outcome, completed_at=func.now())
                .returning(DeviceOperation.id)
            )).scalar_one_or_none()
            await db.commit()
            row = None
            if completed is None:
                # Credentials only move forward, so a write refused for them
                # is still stale when read back.
                if not await self._current(db, device_id, generation):
                    return "stale"
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
