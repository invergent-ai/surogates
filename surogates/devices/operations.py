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
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Literal, TypeVar
from uuid import UUID

from asyncpg.exceptions import InternalClientError
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import and_, delete, exists, func, insert, or_, select, text, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from surogates.db.models import Device, DeviceOperation, DeviceTransfer, DeviceTransferChunk
from surogates.db.models import Session as SessionRow
from surogates.devices.binding import (
    BIND,
    RETIRE,
    THREAD_ACTIONS,
    THREAD_KINDS,
    binding_of,
    copy_of,
    device_of,
    is_binding_root,
)
from surogates.devices.presence import DevicePresence, control_channel
from surogates.devices.store import REVOKED_OUTCOME
from surogates.devices.workspace import (
    CHUNK_BYTES,
    RESULT_TRANSFERS,
    DeviceOperationError,
    is_well_formed,
    transfer_of,
)
from surogates.runtime.turn_slots import turn_detached, turn_waiting
from surogates.workstreams import is_project_thread

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

# The outcome of an operation its session stopped before the computer reported
# a result.  The computer may have run it anyway: its late reply is dropped.
CANCELLED_OUTCOME: dict[str, Any] = {
    "error": {
        "type": "cancelled",
        "message": "Stopped before the computer reported a result. It may have run in part or in full: check before repeating it.",
    },
}

# A calling session in one of these records none of the agent's operations.  A
# cancellation of it then cannot miss one recorded beside it.  The user's own
# requests, and a thread's put-back of its copy and its landing, are refused
# only for a deleted chat (see _check_session).
_STOPPED_STATUSES = frozenset({"paused", "archived", "failed"})

# The longest a stopped call waits to close its own operation.
_CANCEL_PATIENCE_S = 5.0
# How soon it asks again when the database was out of reach.
_CANCEL_RETRY_S = 0.05

# The invocation of an operation asked for outside any tool call: the user's
# own request on a chat's files.  A tool call's invocation starts with its
# event id, a number, and a binding's is "bind".
REQUEST_PREFIX = "request:"

# A thread's turn's open of its copy (surogates.devices.history), asked under
# ``open:<turn>``, or ``open:<turn>:<n>`` after a refusal.
OPEN_PREFIX = "open:"

# How many of the user's changes one session may have open on its computer at
# once: a change waiting for its user holds its data in Postgres.  A read never
# counts, nor is refused: its own deadline (READ_WITHIN_S) bounds it.
# ponytail: a constant; make it a setting when a deployment needs another value.
OPEN_REQUESTS_PER_SESSION = 8

# How long a request's rows are kept once the last of them closed.  A request
# is never replayed: only the same request sent again reads them, a 202's
# retry, which the web client gives up on after eleven minutes in all
# (web/src/api/device-requests.ts), so no retry comes after they are gone.
REQUEST_RETENTION = timedelta(hours=1)

# What a change's ordinal 0 is answered: it is recorded as done at once, never
# sent, and names what the change does, so its request id cannot be sent again
# with another change.
_CLAIMED: dict[str, Any] = {"ok": None}

# How long a read's data is kept once the tool result that read it is
# committed, and how long a read's data nothing has read is kept at all.  A
# write's goes at the first pass after the write closes, whatever its age.
RETAIN_CONSUMED = timedelta(hours=24)
ORPHAN_AFTER = timedelta(days=7)


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
    if isinstance(exc, InternalClientError):
        # What the driver says of a connection whose backend ended between two of a
        # call's statements, which SQLAlchemy passes on as it is.  Its other faults
        # (no encoder for a type, a malformed bind) never go away, and are not this.
        return str(exc).startswith("cannot switch to state")
    return isinstance(exc, (PoolTimeoutError, ConnectionError, TimeoutError))


def _completing(device_id: UUID, generation: int, operation_id: UUID, digest: str, outcome: dict[str, Any]) -> Any:
    """Record a device's outcome for one of its open operations, under its current credentials.

    The credential check is part of the write, so a reauthorization or a
    revocation cannot land between a check and the write.
    """
    return (
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
    )


class OperationConflict(RuntimeError):
    """The same invocation and ordinal were recorded with a different request."""


class TransferGone(DeviceOperationError):
    """A read's or a screenshot's recorded result names a transfer whose data is no longer kept."""


async def _check_session(db: AsyncSession, request: OperationRequest, device: Any) -> bool:
    """Refuse a request for a session that is not this device's, or not bound to it yet.

    The root session must name the device and belong to the device's user and
    agent; the calling session must be the root, or a session created under it.

    Returns whether the session is stopped for this request: for the agent's
    operations, paused, deleted or failed, or under a deleted root; for the
    user's own request and for a thread's put-back of its copy and its
    landing, deleted, or under a deleted root.  The rows stay locked FOR
    SHARE until the operation commits, so a pause or a delete waits for the
    operation it must cancel.
    """
    rows = (await db.execute(
        select(
            SessionRow.id, SessionRow.org_id, SessionRow.agent_id, SessionRow.user_id,
            SessionRow.config, SessionRow.status,
        )
        .where(SessionRow.id.in_([request.root_session_id, request.calling_session_id]))
        .order_by(SessionRow.id)
        .with_for_update(read=True)
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
    own = request.calling_session_id == request.root_session_id and request.ordinal == 0
    is_binding = own and request.invocation_id == BIND
    is_retiring = own and request.invocation_id == RETIRE
    if (request.kind == BIND) != is_binding:
        raise ValueError("Only the root session's own first operation is its binding")
    if (request.kind == RETIRE) != is_retiring:
        raise ValueError("Only the root session's own retirement retires its folder")
    if (is_binding or is_retiring) and not is_binding_root(root.id, root.config):
        # A session created under another works in that session's folder.
        raise ValueError("Only a root session is bound to a folder")
    if is_binding:
        # The bind and the session say the same, and the copy is the root's
        # own: else the worker and the computer would each take the thread to
        # work somewhere else.
        copy = copy_of(root.config)
        if copy not in (None, root.id) or request.args.get("history") != (None if copy is None else {"thread": str(copy)}):
            raise ValueError("A root is bound with a copy only where the server made it a thread that works in one")
    if request.kind in THREAD_KINDS and not _its_own_turn(request, root.config):
        raise ValueError("Only a thread's own turn asks its computer for its snapshots, its history or its landing")
    if not is_binding and (await binding_of(db, request.root_session_id)).state != "bound":
        raise DeviceOperationError("This session's folder is not set up on this computer yet")
    if is_retiring:
        if root.status != "archived":
            raise ValueError("Only a deleted root session's folder is retired")
        # Recorded once its chat is deleted, for its computer to forget the folder.
        return False
    if request.invocation_id.startswith(REQUEST_PREFIX) or (
        request.kind in THREAD_KINDS and not _starts_work(request)
    ):
        # The user's own look at the folder: a paused or failed chat's files
        # are still theirs to open.  A deleted chat's are not.  Nor are a
        # thread's own put-back of its copy and its landing stopped with its
        # turn, which no tool asks: a Stop pauses the thread before its turn
        # hears of it, and what the turn must still do on its computer comes
        # after, a snapshot put back or a landing finished.  A turn's open
        # and a step's snapshot are new work, refused once the thread is
        # stopped, so none outlives the Stop.
        return "archived" in (calling.status, root.status)
    return calling.status in _STOPPED_STATUSES or root.status == "archived"


def _starts_work(request: OperationRequest) -> bool:
    """Whether one of a thread's own kinds is new work of its turn: the turn's open, or the snapshot a step starts from."""
    return request.invocation_id.startswith(OPEN_PREFIX) or (
        request.kind == "checkpoint" and request.args.get("action") == "take"
    )


def _its_own_turn(request: OperationRequest, root: dict[str, Any] | None) -> bool:
    """Whether a thread's own kind is asked as its turn asks it, and as its computer's app takes it.

    The root is a project's thread the server bound with a copy of its own.
    A snapshot is asked under ``checkpoint:``, by any session of the
    thread's; a step of its history or of its landing by the thread itself,
    under ``land:``, and its turn's open also under ``open:``.  No tool call,
    and no request of the user's own, carries one.
    """
    action = request.args.get("action")
    if copy_of(root) != request.root_session_id or not is_project_thread(root) or action not in THREAD_ACTIONS[request.kind]:
        return False
    if request.kind == "checkpoint":
        return request.invocation_id.startswith("checkpoint:")
    opening = request.kind == "history" and action == "open" and request.invocation_id.startswith(OPEN_PREFIX)
    return request.calling_session_id == request.root_session_id and (opening or request.invocation_id.startswith("land:"))


class TooManyRequests(DeviceOperationError):
    """The session already has its fill of the user's changes open on its computer."""


async def _refuse_too_many_requests(db: AsyncSession, request: OperationRequest, device: Any) -> None:
    """Refuse a new change from a session that has OPEN_REQUESTS_PER_SESSION changes open already.

    Only a change counts, or is refused: a request whose claim at ordinal 0
    names what it changes.  A read has none.  A change already under way may
    go on: its next operation, or the same change sent again; its claim does
    not count as under way.  Nor does a change count against itself: sent
    twice at once, the second finds the first recorded only once it holds the
    lock, and joins it.
    """
    already = (await db.execute(
        select(DeviceOperation.id).where(
            DeviceOperation.calling_session_id == request.calling_session_id,
            DeviceOperation.invocation_id == request.invocation_id,
            DeviceOperation.ordinal > 0,
        ).limit(1)
    )).scalar_one_or_none()
    if already is not None:
        return
    changes = select(DeviceOperation.invocation_id).where(
        DeviceOperation.calling_session_id == request.calling_session_id,
        DeviceOperation.invocation_id.startswith(REQUEST_PREFIX),
        DeviceOperation.ordinal == 0,
        DeviceOperation.kind == "request",
    )
    if (await db.execute(
        changes.where(DeviceOperation.invocation_id == request.invocation_id).limit(1)
    )).scalar_one_or_none() is None:
        return
    # Held to the commit, so a burst of new changes counts one at a time: the
    # device row's lock is FOR SHARE, and every one would pass the count together.
    await db.execute(select(func.pg_advisory_xact_lock(func.hashtext(f"device-requests:{request.calling_session_id}"))))
    open_changes = (await db.execute(
        select(func.count(func.distinct(DeviceOperation.invocation_id))).where(
            DeviceOperation.calling_session_id == request.calling_session_id,
            DeviceOperation.invocation_id.in_(changes),
            DeviceOperation.invocation_id != request.invocation_id,
            DeviceOperation.completed_at.is_(None),
        )
    )).scalar_one()
    if open_changes >= OPEN_REQUESTS_PER_SESSION:
        raise TooManyRequests(
            f"Too much of this chat is waiting for {device.name} already: try again once it is done",
        )


async def _check_lease(db: AsyncSession, request: OperationRequest) -> None:
    """Refuse an operation from a worker that no longer holds its session's lease.

    The lease row stays locked FOR SHARE until the operation commits, so a
    worker taking the session over waits for it, then finds it in the journal.
    """
    current = (await db.execute(
        text("SELECT lease_token FROM session_leases WHERE session_id = :id FOR SHARE"),
        {"id": request.calling_session_id},
    )).scalar_one_or_none()
    if current is None or str(current) != request.lease_token:
        raise DeviceOperationError("Another worker runs this session now")


async def _refuse_when_full(db: AsyncSession, request: OperationRequest, device: Any) -> None:
    """Refuse a new operation from a session not yet waiting on a computer that has its fill.

    A session already waiting may keep asking.  A binding waits for its user,
    not for the computer, so it never counts; nor does the user's own request,
    which parks nothing on a worker.  A replay gets its recorded
    outcome, and a tool call already under way may finish its remaining steps:
    neither is ever refused.
    """
    already = (await db.execute(
        select(DeviceOperation.id).where(
            DeviceOperation.calling_session_id == request.calling_session_id,
            DeviceOperation.invocation_id == request.invocation_id,
        ).limit(1)
    )).scalar_one_or_none()
    if already is not None:
        return
    open_for_device = (
        DeviceOperation.device_id == request.device_id,
        DeviceOperation.completed_at.is_(None),
        DeviceOperation.kind.not_in((BIND, RETIRE)),
        DeviceOperation.invocation_id.not_like(f"{REQUEST_PREFIX}%"),
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


async def _keep_payload(db: AsyncSession, operation_id: UUID, transfer: dict[str, Any], data: bytes) -> None:
    """Store a write's data with its operation, in its transaction: the link sends it after the op."""
    await db.execute(insert(DeviceTransfer).values(
        operation_id=operation_id,
        size=transfer["size"],
        sha256=transfer["sha256"],
        # Whole: a read's transfer starting on this device deletes only half-sent ones.
        received=len(data),
        # Stored by the worker: no connection sends it in.
        holder="",
    ))
    # Views, not slices: a slice of bytes is a copy, and the chunks would hold a second one of the data.
    view = memoryview(data)
    chunk = insert(DeviceTransferChunk)
    for seq, at in enumerate(range(0, len(data), CHUNK_BYTES)):
        # One statement a chunk, as the link stores a read's, never one of many
        # rows: the driver writes that in parts, and a stop that comes between
        # two of them is lost there.  The call then waits for ever on a
        # statement it never finished sending, and keeps its connection.
        await db.execute(chunk, {"operation_id": operation_id, "seq": seq, "data": view[at:at + CHUNK_BYTES]})


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
    # A write's data that its args name as a transfer: kept with the operation,
    # never part of the digest, and never in a log line.
    payload: bytes | None = field(default=None, repr=False)

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

    async def run(self, request: OperationRequest, *, keep_open: bool = False) -> dict[str, Any]:
        """Run *request* on its device and return the outcome, however long that takes.

        A request already recorded under the same invocation and ordinal is
        joined, never repeated: its outcome comes back when the device reports
        it, or at once if it already has.

        A new operation of the agent's from a stopped session (paused, deleted
        or failed), a turn's open and a step's snapshot among them, or a new
        request of the user's or a thread's put-back or landing from a
        deleted one, raises "This session was stopped" and records nothing.
        A caller that is stopped while it waits, or while its request is
        being recorded, closes its own operation unless its turn is detached,
        handed to another worker that carries the wait on, or it is
        *keep_open*: a request whose HTTP wait ended before its computer
        answered, which the same request joins again later.  The call
        returns, or raises, only after its wait's subscription is closed.
        """
        waiter: asyncio.Future | None = None
        try:
            operation_id, outcome = await self._while_database_recovers(
                "recording", lambda: self._record(request),
            )
            if outcome is not None:
                return outcome
            await self._announce(control_channel(request.device_id), f"op:{operation_id}")
            # One wait, never interrupted: a deadline here could cancel a query mid-flight.
            waiter = asyncio.ensure_future(self._wait_forever(operation_id))
            done, _ = await asyncio.wait({waiter}, timeout=WAIT_GRACE_S)
            if done:
                return waiter.result()
            # Still running, or the computer is away: the turn need not hold the worker meanwhile.
            async with turn_waiting(resumable=True):
                return await self._wait_watching(waiter, request)
        except asyncio.CancelledError:
            # Stopped, not handed to another worker: nothing will ask for it
            # again, so it must not run when the computer comes back.  Found by
            # the request's key: a stop during the record's commit leaves no
            # operation id to name, and the row may be committed.
            if not keep_open and not turn_detached():
                await self._cancel_own(request)
            raise
        finally:
            if waiter is not None:
                waiter.cancel()  # a no-op once it finished; stops it if this caller is cancelled
                # Its subscription is closed before this call returns.
                await asyncio.gather(waiter, return_exceptions=True)

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
                await self._notice.back(request, announce=not turn_detached())

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
                logger.warning(
                    "%s an operation: database unavailable (%s: %s); retrying", what, type(exc).__name__, exc,
                )
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

    async def bind(
        self, *, session_id: UUID, device_id: UUID, folder: str, nonce: str, history: UUID | None = None,
    ) -> None:
        """Ask the device to bind a new root session to *folder*, without waiting.

        The app answers once its user has confirmed that folder under *nonce*.
        With *history*, the thread the session is, the app binds it to a copy
        of the folder that the folder's history makes for that thread, never
        to the folder itself.  For an HTTP request, so a database outage
        fails it rather than holding it open.
        """
        operation_id, outcome = await self._record(OperationRequest(
            device_id=device_id,
            root_session_id=session_id,
            calling_session_id=session_id,
            invocation_id=BIND,
            ordinal=0,
            kind=BIND,
            args={"folder": folder, "nonce": nonce, **({"history": {"thread": str(history)}} if history is not None else {})},
        ))
        if outcome is None:
            await self._announce(control_channel(device_id), f"op:{operation_id}")

    async def claim(self, request: OperationRequest) -> None:
        """Record a change's ordinal 0, which names what it does, as done at once.

        It is never sent to the computer.  The same request id sent again with
        another change finds it, and is refused: OperationConflict.  For an
        HTTP request, so a database outage fails it rather than holding it open.
        """
        async with self._sf() as db:
            await db.execute(
                pg_insert(DeviceOperation)
                .values(
                    device_id=request.device_id,
                    root_session_id=request.root_session_id,
                    calling_session_id=request.calling_session_id,
                    invocation_id=request.invocation_id,
                    ordinal=request.ordinal,
                    kind=request.kind,
                    args=request.args,
                    digest=request.digest,
                    outcome=_CLAIMED,
                    completed_at=func.now(),
                )
                .on_conflict_do_nothing(constraint="uq_device_operations_invocation")
            )
            await db.commit()
            recorded = (await db.execute(
                select(DeviceOperation.digest).where(
                    DeviceOperation.calling_session_id == request.calling_session_id,
                    DeviceOperation.invocation_id == request.invocation_id,
                    DeviceOperation.ordinal == request.ordinal,
                )
            )).scalar_one()
        if recorded != request.digest:
            raise OperationConflict(f"{request.invocation_id} was sent before with another change")

    async def retire(self, *, session_id: UUID, device_id: UUID) -> None:
        """Ask the device to forget a deleted root session's folder, without waiting.

        Recorded like any operation, so a computer away now forgets it when
        it is back.  The folder itself is never touched.
        """
        operation_id, outcome = await self._record(OperationRequest(
            device_id=device_id,
            root_session_id=session_id,
            calling_session_id=session_id,
            invocation_id=RETIRE,
            ordinal=0,
            kind=RETIRE,
            args={},
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
        transfer = request.args.get("transfer")
        # Present is what counts, as for the link, which queues a write's data on the key alone.
        if ("transfer" in request.args) != (request.payload is not None) or (
            request.payload is not None
            and (not isinstance(transfer, dict) or transfer.get("size") != len(request.payload))
        ):
            # Recorded, the link would send an op whose data never comes, or that the computer finds damaged.
            raise ValueError("A write's data and the transfer its args name must come together, of one size")
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
            if request.lease_token is not None:
                # The lease before the session rows: emit_event takes them in that order.
                await _check_lease(db, request)
            if await _check_session(db, request, device):
                # A session stopped for this caller records nothing new.  One
                # it already recorded keeps its outcome, so a call resumed as
                # a pause lands still learns what the computer did; an open
                # one is completed by that pause's cancellation.
                existing = (await db.execute(
                    select(DeviceOperation.id, DeviceOperation.digest, DeviceOperation.outcome).where(
                        DeviceOperation.calling_session_id == request.calling_session_id,
                        DeviceOperation.invocation_id == request.invocation_id,
                        DeviceOperation.ordinal == request.ordinal,
                    )
                )).one_or_none()
                if existing is None:
                    raise DeviceOperationError("This session was stopped")
                if existing.digest != digest:
                    raise OperationConflict(
                        f"Operation {request.ordinal} of {request.invocation_id} was recorded "
                        "with a different request"
                    )
                return existing.id, existing.outcome
            # ponytail: the device row is locked FOR SHARE, so concurrent recorders can pass the
            # parked count together and that limit is soft by their number.
            # A request never parks: its HTTP wait is bounded, and the changes among them have their own cap.
            if request.invocation_id.startswith(REQUEST_PREFIX):
                if device.revoked_at is None:
                    await _refuse_too_many_requests(db, request, device)
            elif request.kind not in (BIND, RETIRE) and device.revoked_at is None:
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
            if inserted is not None and request.payload is not None and not refused:
                # Committed with the operation: the link may send the op as soon as it is.
                await _keep_payload(db, inserted, request.args["transfer"], request.payload)
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

    async def transfer_chunks(self, calling_session_id: UUID, invocation_id: str, ordinal: int) -> list[bytes]:
        """The chunks of the transfer an operation's outcome names, in order; none once it is gone."""
        async def fetch() -> list[bytes]:
            async with self._sf() as db:
                return list((await db.execute(
                    select(DeviceTransferChunk.data)
                    .join(DeviceTransfer, DeviceTransfer.operation_id == DeviceTransferChunk.operation_id)
                    .join(DeviceOperation, DeviceOperation.id == DeviceTransfer.operation_id)
                    .where(
                        DeviceOperation.calling_session_id == calling_session_id,
                        DeviceOperation.invocation_id == invocation_id,
                        DeviceOperation.ordinal == ordinal,
                        DeviceTransfer.received == DeviceTransfer.size,
                    )
                    .order_by(DeviceTransferChunk.seq)
                )).scalars())
        return await self._while_database_recovers("reading the data of", fetch)

    async def outgoing_chunk(self, device_id: UUID, generation: int, operation_id: UUID, seq: int) -> bytes | None:
        """Chunk *seq* of the data a write's args name, while the write is open.

        Only for the write's own device under its current credentials, as
        ``pending`` and ``complete`` are: None once the write is closed, or for
        a connection under rotated-out or revoked credentials.
        """
        async with self._sf() as db:
            return (await db.execute(
                select(DeviceTransferChunk.data)
                .join(DeviceOperation, DeviceOperation.id == DeviceTransferChunk.operation_id)
                .join(Device, Device.id == DeviceOperation.device_id)
                .where(
                    DeviceTransferChunk.operation_id == operation_id,
                    DeviceTransferChunk.seq == seq,
                    DeviceOperation.device_id == device_id,
                    DeviceOperation.completed_at.is_(None),
                    Device.credential_generation == generation,
                    Device.revoked_at.is_(None),
                )
            )).scalar_one_or_none()

    async def consume(self, calling_session_id: UUID, invocation_id: str) -> int:
        """Mark the transfers one tool call read as consumed: its result is committed."""
        async with self._sf() as db:
            marked = (await db.execute(
                update(DeviceTransfer)
                .where(
                    DeviceTransfer.consumed_at.is_(None),
                    DeviceTransfer.operation_id.in_(select(DeviceOperation.id).where(
                        DeviceOperation.calling_session_id == calling_session_id,
                        DeviceOperation.invocation_id == invocation_id,
                    )),
                )
                .values(consumed_at=func.now())
                .returning(DeviceTransfer.operation_id)
            )).all()
            await db.commit()
        return len(marked)

    async def joined(
        self, calling_session_id: UUID, invocation_id: str, kind: str,
    ) -> tuple[list[tuple[str, dict[str, Any]]], dict[str, Any]] | None:
        """A request sent again by what it changes alone, without its data: what it recorded, and how it ended.

        Its operations, in order, as (kind, args), and the outcome of its
        operation of *kind* once the computer answers it, however long that
        takes: its caller bounds the wait, which leaves the operation open.
        None when it recorded none of *kind*: the request must be sent whole.
        """
        async with self._sf() as db:
            rows = (await db.execute(
                select(DeviceOperation.id, DeviceOperation.kind, DeviceOperation.args)
                .where(
                    DeviceOperation.calling_session_id == calling_session_id,
                    DeviceOperation.invocation_id == invocation_id,
                )
                .order_by(DeviceOperation.ordinal)
            )).all()
        target = next((row.id for row in rows if row.kind == kind), None)
        if target is None:
            return None
        return [(row.kind, row.args) for row in rows], await self._wait_forever(target)

    async def recorded(self, calling_session_id: UUID, invocation_id: str) -> int:
        """How many operations of one invocation are in the journal."""
        async with self._sf() as db:
            return (await db.execute(
                select(func.count()).select_from(DeviceOperation).where(
                    DeviceOperation.calling_session_id == calling_session_id,
                    DeviceOperation.invocation_id == invocation_id,
                )
            )).scalar_one()

    async def opens(self, calling_session_id: UUID, turn: int) -> int:
        """How many times *calling_session_id*'s turn *turn* asked its computer to open its copy.

        The first asking is recorded under ``open:<turn>``, and each after a
        refusal under ``open:<turn>:<n>``, n counted from 1: so the count
        names the turn's last asking, and its next (surogates.devices.history).
        """
        first = f"{OPEN_PREFIX}{turn}"
        async with self._sf() as db:
            return (await db.execute(
                select(func.count()).select_from(DeviceOperation).where(
                    DeviceOperation.calling_session_id == calling_session_id,
                    DeviceOperation.kind == "history",
                    or_(DeviceOperation.invocation_id == first, DeviceOperation.invocation_id.startswith(f"{first}:", autoescape=True)),
                )
            )).scalar_one()

    async def complete(
        self,
        device_id: UUID,
        generation: int,
        operation_id: UUID,
        digest: str,
        outcome: dict[str, Any],
    ) -> Literal["completed", "duplicate", "gone", "rejected", "stale"]:
        """Record the device's outcome for one of its own operations.

        "duplicate": already recorded.  "gone": the journal no longer has it,
        as a request reaped an hour after it closed, so there is nothing to
        record.  "rejected": another device's, or asked for with a different
        digest.  "stale": the reply came under rotated-out or revoked
        credentials.

        The credential check is part of the write, not a step before it, so a
        reauthorization or revocation cannot land between the two.
        """
        async with self._sf() as db:
            if not await self._current(db, device_id, generation):
                return "stale"
            completed = (await db.execute(
                _completing(device_id, generation, operation_id, digest, outcome)
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
        if completed is None and row is None:
            return "gone"
        if completed is None and (row.device_id != device_id or row.digest != digest):
            return "rejected"
        # Announced for a duplicate too: its worker may have missed the first.
        await self._announce(operation_channel(operation_id), "completed")
        return "completed" if completed is not None else "duplicate"

    async def start_transfer(
        self,
        device_id: UUID,
        generation: int,
        holder: str,
        operation_id: UUID,
        digest: str,
        size: int,
        sha256: str,
    ) -> Literal["started", "unwanted", "rejected", "busy", "stale"]:
        """Get ready for the chunks of a read's result, sent by the connection *holder*.

        "unwanted": the operation is closed, or the journal no longer has it
        (a request reaped since), so its data is not needed.
        "rejected": another device's, asked for with a different digest, or
        of a kind whose result is never a transfer, closed or not: whether
        another device's operation is closed is not this one's to learn.
        "busy": another connection started this transfer at the same moment,
        or its last chunk landed meanwhile.
        "stale": rotated-out or revoked credentials.

        A device sends one transfer at a time, so what it left half-sent
        before, this operation's or another's, goes: a connection that ends
        mid-transfer sends it again whole on the next.
        """
        async with self._sf() as db:
            if not await self._current(db, device_id, generation):
                return "stale"
            row = (await db.execute(
                select(
                    DeviceOperation.device_id, DeviceOperation.digest, DeviceOperation.kind,
                    DeviceOperation.completed_at,
                ).where(DeviceOperation.id == operation_id)
            )).one_or_none()
            if row is None:
                return "unwanted"
            if row.device_id != device_id or row.digest != digest or row.kind not in RESULT_TRANSFERS:
                return "rejected"
            if row.completed_at is not None:
                return "unwanted"
            # Half-sent only: a whole one is a last chunk that landed since the check above.
            await db.execute(delete(DeviceTransfer).where(
                DeviceTransfer.received < DeviceTransfer.size,
                DeviceTransfer.operation_id.in_(
                    select(DeviceOperation.id).where(DeviceOperation.device_id == device_id)
                ),
            ))
            started = (await db.execute(
                pg_insert(DeviceTransfer)
                .values(operation_id=operation_id, size=size, sha256=sha256, holder=holder)
                .on_conflict_do_nothing(index_elements=[DeviceTransfer.operation_id])
                .returning(DeviceTransfer.operation_id)
            )).scalar_one_or_none()
            if started is None:
                await db.rollback()
                return "busy"
            await db.commit()
        return "started"

    async def store_chunk(
        self,
        device_id: UUID,
        generation: int,
        holder: str,
        operation_id: UUID,
        digest: str,
        seq: int,
        data: bytes,
        outcome: dict[str, Any] | None,
    ) -> Literal["stored", "completed", "unwanted", "lost", "stale"]:
        """Store chunk *seq* of the transfer *holder* started, and with the last one record *outcome*.

        "completed": that was the last chunk and the outcome is recorded, in
        the same transaction.  "unwanted": the operation closed meanwhile.
        "lost": this half-sent transfer was dropped since, by another
        connection's start of it or of another of the device's transfers.
        "stale": rotated-out or revoked credentials.
        """
        async with self._sf() as db:
            if not await self._current(db, device_id, generation):
                return "stale"
            closed = (await db.execute(
                select(DeviceOperation.completed_at).where(DeviceOperation.id == operation_id)
            )).scalar_one_or_none()
            if closed is not None:
                return "unwanted"
            moved = (await db.execute(
                update(DeviceTransfer)
                .where(
                    DeviceTransfer.operation_id == operation_id,
                    DeviceTransfer.holder == holder,
                    DeviceTransfer.received == seq * CHUNK_BYTES,
                )
                .values(received=DeviceTransfer.received + len(data))
                .returning(DeviceTransfer.operation_id)
            )).scalar_one_or_none()
            if moved is None:
                # Closed since the check above, and its transfer reaped; or taken over.
                closed = (await db.execute(
                    select(DeviceOperation.completed_at).where(DeviceOperation.id == operation_id)
                )).scalar_one_or_none()
                return "unwanted" if closed is not None else "lost"
            db.add(DeviceTransferChunk(operation_id=operation_id, seq=seq, data=data))
            if outcome is not None:
                completed = (await db.execute(
                    _completing(device_id, generation, operation_id, digest, outcome)
                )).scalar_one_or_none()
                if completed is None:
                    # Closed, or the credentials moved on, since the checks above.
                    await db.rollback()
                    return "unwanted" if await self._current(db, device_id, generation) else "stale"
                if transfer_of(outcome) is None:
                    # Recorded as damaged: bytes known to be wrong are not kept.
                    await db.execute(delete(DeviceTransfer).where(DeviceTransfer.operation_id == operation_id))
            await db.commit()
        if outcome is None:
            return "stored"
        await self._announce(operation_channel(operation_id), "completed")
        return "completed"

    async def cancel(self, calling_session_ids: Collection[UUID], *, bindings: bool = False) -> int:
        """Cancel the open operations of these sessions, and tell their computers.

        What a computer already reported keeps its outcome.  A binding and the
        user's own requests are cancelled only with *bindings*: deleting a chat
        ends its folder's set-up and the changes it waits on; pausing or failing
        it stops only the agent's work.  Returns how many were cancelled.
        """
        if not calling_session_ids:
            return 0
        conditions = [DeviceOperation.calling_session_id.in_(list(calling_session_ids))]
        if not bindings:
            conditions.append(DeviceOperation.kind != BIND)
            conditions.append(DeviceOperation.invocation_id.not_like(f"{REQUEST_PREFIX}%"))
        return await self._cancel_where(*conditions)

    async def cancel_invocation(self, calling_session_id: UUID, invocation_id: str) -> int:
        """Cancel the open operations of one tool call."""
        return await self._cancel_where(
            DeviceOperation.calling_session_id == calling_session_id,
            DeviceOperation.invocation_id == invocation_id,
        )

    async def closed_among(self, device_id: UUID, operation_ids: Collection[UUID]) -> list[UUID]:
        """Which of these operations of the device the server has closed.

        For the ids the app reports open, or was sent and has not answered:
        only the server can have closed those, by a cancellation or a
        revocation, so the app must be told to stop them.
        """
        if not operation_ids:
            return []
        # Ids only: an outcome can be a megabyte, and the ids may be as many
        # as a device's hello lists.
        async with self._sf() as db:
            return list((await db.execute(
                select(DeviceOperation.id).where(
                    DeviceOperation.device_id == device_id,
                    DeviceOperation.id.in_(list(operation_ids)),
                    DeviceOperation.completed_at.is_not(None),
                )
            )).scalars())

    async def _cancel_where(self, *conditions: Any, decided_for: UUID | None = None) -> int:
        async with self._sf() as db:
            if decided_for is not None:
                # A recording holds its device's row FOR SHARE until its commit is
                # decided: waited for here, so one whose commit is still on its
                # way is seen, and closed, rather than missed.
                await db.execute(
                    select(Device.id).where(Device.id == decided_for).with_for_update(key_share=True)
                )
            rows = (await db.execute(
                update(DeviceOperation)
                .where(DeviceOperation.completed_at.is_(None), *conditions)
                .values(outcome=CANCELLED_OUTCOME, completed_at=func.now())
                .returning(DeviceOperation.id, DeviceOperation.device_id)
            )).all()
            await db.commit()
        for operation_id, device_id in rows:
            await self._announce(operation_channel(operation_id), "completed")
            await self._announce(control_channel(device_id), f"cancel:{operation_id}")
        return len(rows)

    async def _cancel_own(self, request: OperationRequest) -> None:
        """Close a stopped call's operation, within _CANCEL_PATIENCE_S: past it, it stays open as before.

        Asked again while the database is out of reach: the stop that brought
        the call here may have ended the connection its recording was on, and
        the pool lends that connection to the next caller, which is this one.
        Giving up on it would leave an operation its user stopped open, for
        its computer to run.
        """
        async def close() -> None:
            try:
                async with asyncio.timeout(_CANCEL_PATIENCE_S):
                    while True:
                        try:
                            await self._cancel_where(
                                DeviceOperation.calling_session_id == request.calling_session_id,
                                DeviceOperation.invocation_id == request.invocation_id,
                                DeviceOperation.ordinal == request.ordinal,
                                decided_for=request.device_id,
                            )
                            return
                        except Exception as exc:
                            if not _database_unavailable(exc):
                                raise
                            await asyncio.sleep(_CANCEL_RETRY_S)
            except Exception:
                # Said here: once its caller was stopped again, nothing awaits this any more.
                logger.warning(
                    "could not cancel operation %s of %s; it stays open",
                    request.ordinal, request.invocation_id, exc_info=True,
                )

        # Shielded, the asking again with it: a second cancel of the
        # stopping task must not abandon the write half-way.
        await asyncio.shield(asyncio.ensure_future(close()))


async def reap_transfers(session_factory: async_sessionmaker[AsyncSession]) -> int:
    """Delete the transfers nothing will read again; their operations' rows stay.

    A write's data went to the computer: it goes once its operation is closed
    (answered, stopped or revoked), and never while it is open, however long
    the computer stays away.  A read's result came from the computer, and goes
    when it was consumed RETAIN_CONSUMED ago; when its operation closed without
    it, half-sent (cancelled, revoked); and when nothing consumed it for
    ORPHAN_AFTER (a tool call that never committed its result, or a session
    never resumed), which a replay then reports interrupted.
    """
    def of_operation(*conditions: Any) -> Any:
        return exists().where(DeviceOperation.id == DeviceTransfer.operation_id, *conditions)

    closed = DeviceOperation.completed_at.is_not(None)
    async with session_factory() as db:
        reaped = (await db.execute(
            delete(DeviceTransfer)
            .where(or_(
                of_operation(DeviceOperation.kind == "write", closed),
                and_(of_operation(DeviceOperation.kind.in_(RESULT_TRANSFERS)), or_(
                    DeviceTransfer.consumed_at < func.now() - RETAIN_CONSUMED,
                    and_(DeviceTransfer.received < DeviceTransfer.size, of_operation(closed)),
                    and_(DeviceTransfer.consumed_at.is_(None), DeviceTransfer.created_at < func.now() - ORPHAN_AFTER),
                )),
            ))
            .returning(DeviceTransfer.operation_id)
        )).all()
        await db.commit()
    return len(reaped)


async def reap_requests(session_factory: async_sessionmaker[AsyncSession]) -> int:
    """Delete the rows of the user's own requests REQUEST_RETENTION after the last of them closed.

    A request with an operation still open keeps every row: the same request
    sent again joins them.  Their transfers go with them.
    """
    # ponytail: scans the journal's request rows; a partial index on them when the table grows.
    closed = (
        select(DeviceOperation.calling_session_id, DeviceOperation.invocation_id)
        .where(DeviceOperation.invocation_id.startswith(REQUEST_PREFIX))
        .group_by(DeviceOperation.calling_session_id, DeviceOperation.invocation_id)
        .having(func.bool_and(DeviceOperation.completed_at.is_not(None)))
        .having(func.max(DeviceOperation.completed_at) < func.now() - REQUEST_RETENTION)
    )
    async with session_factory() as db:
        reaped = (await db.execute(
            delete(DeviceOperation)
            .where(tuple_(DeviceOperation.calling_session_id, DeviceOperation.invocation_id).in_(closed))
            .returning(DeviceOperation.id)
        )).all()
        await db.commit()
    return len(reaped)


def _whole(chunks: list[bytes], transfer: dict[str, Any]) -> bytes | None:
    """A transfer's data, joined, if it is all there and hashes to its name."""
    data = b"".join(chunks)
    if not chunks or len(data) != transfer["size"] or hashlib.sha256(data).hexdigest() != transfer["sha256"]:
        return None
    return data


class JournalRunner:
    """The operations of one tool call, numbered in the order its handler asks for them.

    A read or a screenshot answered with a transfer comes back as ``{"ok": <its bytes>}``.
    """

    def __init__(
        self,
        operations: DeviceOperations,
        *,
        device_id: UUID,
        root_session_id: UUID,
        calling_session_id: UUID,
        invocation_id: str,
        lease_token: str | None = None,
        keep_open: bool = False,
    ) -> None:
        self._operations = operations
        self._device_id = device_id
        self._root_session_id = root_session_id
        self._calling_session_id = calling_session_id
        self._invocation_id = invocation_id
        self._lease_token = lease_token
        self._keep_open = keep_open
        self._ordinal = 0
        self._conflicted = False

    async def run(self, kind: str, args: dict[str, Any], payload: bytes | None = None) -> dict[str, Any]:
        if self._conflicted:
            # A resumed call that took another path asks the computer for nothing more.
            raise OperationConflict(f"{self._invocation_id} took another path than its first run")
        self._ordinal += 1
        # This operation's own: another of the call's may be asked for while this one waits.
        ordinal = self._ordinal
        try:
            outcome = await self._operations.run(OperationRequest(
                device_id=self._device_id,
                root_session_id=self._root_session_id,
                calling_session_id=self._calling_session_id,
                invocation_id=self._invocation_id,
                ordinal=ordinal,
                kind=kind,
                args=args,
                lease_token=self._lease_token,
                payload=payload,
            ), keep_open=self._keep_open)
            transfer = transfer_of(outcome)
            if transfer is None:
                return outcome
            chunks = await self._operations.transfer_chunks(self._calling_session_id, self._invocation_id, ordinal)
            # Up to 50 MiB: joined and hashed off the event loop the worker's other sessions share.
            data = await asyncio.to_thread(_whole, chunks, transfer)
            if data is None:
                raise TransferGone(
                    "The computer's screenshot is no longer kept: take it again" if kind == "browser.screenshot"
                    else "The computer's answer to this read is no longer kept: read the file again"
                )
            return {"ok": data}
        except (OperationConflict, TransferGone):
            # A resumed call that cannot get what its first run got is reported interrupted.
            self._conflicted = True
            raise

    async def diverged(self) -> bool:
        """Whether this call, resumed, asked for other operations than the run it resumes."""
        if self._conflicted:
            return True
        return self._ordinal < await self._operations.recorded(
            self._calling_session_id, self._invocation_id,
        )

    async def close_open(self) -> int:
        """Cancel what this call's first run left open: a call reported interrupted must not act later."""
        return await self._operations.cancel_invocation(self._calling_session_id, self._invocation_id)

    async def consumed(self) -> int:
        """This call's result is committed: what it read in transfers may go on the retention schedule."""
        return await self._operations.consume(self._calling_session_id, self._invocation_id)
