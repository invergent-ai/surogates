"""Orchestrator -- pulls sessions from the Redis work queue and dispatches them to the harness.

The :class:`Orchestrator` is the long-running event loop that:

* Blocks on Redis ``BZPOPMIN`` to dequeue session IDs in priority order.
* Spawns bounded async tasks (controlled by a semaphore) that call
  ``AgentHarness.wake()`` for each session.
* Implements retry with exponential back-off for transient failures.
* Provides graceful shutdown: stops accepting new work and waits for
  in-flight tasks to drain.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import traceback
from typing import TYPE_CHECKING, Any, Callable
from uuid import UUID

from dataclasses import dataclass

from surogates.config import (
    INTERRUPT_CHANNEL_PREFIX,
    SHARED_WORK_QUEUE_KEY,
    enqueue_session,
    parse_queue_member,
)
from surogates.harness.error_classify import classify_harness_error
from surogates.session.events import EventType

if TYPE_CHECKING:
    from redis.asyncio import Redis

    from surogates.browser.pool import BrowserPool
    from surogates.session.store import SessionStore

logger = logging.getLogger(__name__)

# Maximum number of retry attempts for a single session.
_MAX_RETRIES: int = 3

# Base delay (seconds) for exponential back-off on retry.
_BASE_RETRY_DELAY: float = 1.0

# ── Crash-loop circuit breaker ────────────────────────────────────────
# The in-process retry counter resets on every re-enqueue, so a session
# whose conversation deterministically crashes the LLM call (e.g. a
# provider 400 on poisoned history) can be replayed indefinitely by
# mission continuations or other automatic re-wakes — burning a full
# context's worth of input tokens per attempt. Track consecutive
# crashes with an identical fingerprint in Redis (shared across worker
# replicas); at the threshold, fail the session terminally and refuse
# further wakes until an explicit human signal (a real user message or
# a user-initiated retry) arrives.
_CRASH_LOOP_THRESHOLD: int = 3
_CRASH_LOOP_KEY_PREFIX = "surogates:crash_loop:"
_CRASH_LOOP_TTL_SECONDS: int = 6 * 3600


def _crash_fingerprint(exc: BaseException) -> str:
    """Stable identity for a crash: error category + hashed detail."""
    info = classify_harness_error(exc)
    detail_hash = hashlib.sha256(
        (info.detail or "").encode("utf-8", "replace")
    ).hexdigest()[:16]
    return f"{info.category}:{detail_hash}"

# Orphan sweep cadence.  The sweep runs every ``_ORPHAN_SWEEP_INTERVAL``
# seconds and recovers any session whose lease has expired AND whose
# last event landed more than ``_ORPHAN_STALE_SECONDS`` ago.
#
# ``_ORPHAN_STALE_SECONDS`` only needs to exceed the lease TTL: a
# live worker's lease is continuously renewed by
# ``_renew_lease_forever`` and the orphan finder excludes any session
# with a valid lease (LEFT JOIN on ``LeaseRow.expires_at > now()``),
# so slow-but-alive turns are protected by the lease, not by this
# threshold.  Earlier versions used 300s as a redundant safety net,
# but that left dead-worker sessions in "Working on it…" state for
# 5+ minutes after a restart.  60s matches the lease TTL: lease
# expires → 60s later the session is past the threshold → next sweep
# (within ``_ORPHAN_SWEEP_INTERVAL``) recovers it.  Worst-case
# recovery time is now ~2 min instead of ~6 min.
_ORPHAN_SWEEP_INTERVAL: float = 60.0
_ORPHAN_STALE_SECONDS: int = 60

# Boot-time aggressive orphan sweep threshold.  Now identical to the
# steady-state ``_ORPHAN_STALE_SECONDS`` (both 60s, matching lease
# TTL); the boot sweep stays a distinct knob so we can tune the two
# independently if we later add a sub-lease-TTL aggressive recovery
# mode for restart scenarios.
_ORPHAN_BOOT_STALE: int = 60

# Subagent task layer tick cadence — promote ``todo`` Tasks whose parents
# have all completed, finalise ended worker Sessions, and atomically
# claim ``ready`` Tasks. 5s keeps the wake-after-parent-completion
# latency tight without burning DB cycles. See
# ``surogates.tasks.dispatcher.tasks_tick`` for what each tick does.
_TASKS_TICK_INTERVAL: float = 5.0

# A queued wake can race with an already-running harness that still owns
# the session lease, especially when the user interrupts and immediately
# sends a replacement message. Back off briefly before requeueing so the
# interrupted harness has time to release its lease.
_LEASE_BUSY_REQUEUE_DELAY: float = 0.25

# priority bump applied when a session is requeued
# because its tenant is over the TurnConcurrencyGate cap.  Larger
# numbers = later delivery, so the noisy tenant slips to the back of
# the queue without starving everyone else.  Tuned for ~30s deferral
# at typical pop rates.
_GATE_REQUEUE_BACKOFF: float = 30.0

# How long the dispatcher sleeps when dequeue_next_session returned
# None (either the queue was empty within ``timeout`` or the only
# popped session was gate-busy).  Keep this small so a freshly
# unblocked tenant gets served quickly.
_GATE_EMPTY_SLEEP_SECONDS: float = 0.1


@dataclass(frozen=True, slots=True)
class DequeuedSession:
    """A session popped off the shared queue + decoded tenant tuple."""

    org_id: str
    agent_id: str
    session_id: str
    priority: float


# Maximum number of gate-busy candidates dequeue_next_session will
# requeue-and-skip in a single call before giving up and returning
# None.  Prevents a fully-busy queue from busy-looping inside one
# call; the dispatcher's outer loop sleeps briefly and retries.
_MAX_GATE_BUSY_ATTEMPTS: int = 16


async def dequeue_next_session(
    redis,
    *,
    gate: "Any | None" = None,
    gate_limit: int | None = None,
    timeout: float = 0.0,
):
    """Pop the next session off the shared queue, gated per tenant.

    Walks the queue front-to-back, requeueing
    over-cap candidates with backoff, until either (a) an
    unblocked candidate is found and its gate slot is acquired,
    (b) the queue is empty, or (c) the queue cycles — i.e. the
    next pop yields a member we already requeued this call, which
    means every distinct tenant in the queue is at cap.  In case
    (c) the function returns ``None`` and the dispatcher loop
    sleeps briefly before retrying.

    When ``gate`` is ``None`` the gate is skipped — used in tests
    that don't wire a Redis gate.
    """
    requeued: set[str] = set()
    for _ in range(max(1, _MAX_GATE_BUSY_ATTEMPTS)):
        popped = await redis.bzpopmin(SHARED_WORK_QUEUE_KEY, timeout=timeout)
        if popped is None:
            return None
        _key, member, score = popped
        if isinstance(member, bytes):
            member = member.decode("utf-8")

        if member in requeued:
            # Queue cycled — every distinct tenant in the queue
            # was at cap.  Put this member back at its current
            # (post-backoff) score and stop scanning so the
            # dispatcher's outer loop can sleep briefly.
            await redis.zadd(SHARED_WORK_QUEUE_KEY, {member: score})
            return None

        org_id, agent_id, session_id = parse_queue_member(member)

        if gate is None:
            return DequeuedSession(
                org_id=org_id, agent_id=agent_id,
                session_id=session_id, priority=score,
            )

        acquired = await gate.try_acquire(
            org_id, agent_id, limit=gate_limit,
        )
        if acquired:
            return DequeuedSession(
                org_id=org_id, agent_id=agent_id,
                session_id=session_id, priority=score,
            )

        # Gate-busy candidate: requeue with backoff (no slot
        # consumed) and continue scanning the queue.
        await redis.zadd(
            SHARED_WORK_QUEUE_KEY,
            {member: score + _GATE_REQUEUE_BACKOFF},
        )
        requeued.add(member)
    return None


class Orchestrator:
    """Pulls session IDs from a Redis sorted-set and dispatches them to the agent harness.

    ``queue_key`` is the shared ``surogates:work_queue`` for every
    worker; per-tenant isolation is enforced by
    :class:`~surogates.runtime.TurnConcurrencyGate`, which the
    dispatcher consults on every dequeue.
    """

    def __init__(
        self,
        redis_client: Redis,
        session_store: SessionStore,
        harness_factory: Callable[..., Any],
        *,
        agent_id: str | None = None,
        queue_key: str,
        max_concurrent: int = 50,
        poll_timeout: int = 5,
        browser_pool: BrowserPool | None = None,
        session_factory: Any | None = None,
        tenant_for_task: Callable[[Any], Any] | None = None,
        turn_gate: Any | None = None,
        file_bundle_cache: Any | None = None,
    ) -> None:
        self.redis = redis_client
        self.session_store = session_store
        self.harness_factory = harness_factory
        self.semaphore = asyncio.Semaphore(max_concurrent)
        self._agent_id = agent_id
        self._queue_key = queue_key
        self._poll_timeout = poll_timeout
        self._browser_pool = browser_pool
        # Subagent task layer plumbing — both must be set for tasks_tick
        # to run. When either is None the loop logs a warning at startup
        # and stays disabled (the orchestrator still serves direct
        # spawn_worker / chat sessions normally).
        self._session_factory = session_factory
        self._tenant_for_task = tenant_for_task
        # Per-agent Hub bundle cache so the tick's task-spawn path can
        # resolve bundle-delivered sub-agents (e.g. arbor-executor).
        self._file_bundle_cache = file_bundle_cache
        # per-tenant TurnConcurrencyGate.  ``None`` in tests →
        # dequeue skips the gate.
        self._turn_gate = turn_gate
        self._running = True
        self._tasks: set[asyncio.Task] = set()
        # Active harnesses by session ID — for delivering interrupt signals.
        self._active_harnesses: dict[UUID, Any] = {}
        # Sessions that received an extra enqueue while their wake was
        # already in flight on this worker.  The active wake honours the
        # flag by enqueueing exactly once when it returns, instead of
        # spin-requeueing through the work queue at ~4 Hz for the entire
        # duration of the wake.
        self._rewake_pending: set[UUID] = set()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def interrupt_session(self, session_id: UUID, message: str | None = None) -> bool:
        """Interrupt a running harness for the given session.

        Returns ``True`` if the session was active and the interrupt was
        delivered, ``False`` if the session was not running on this worker.
        """
        harness = self._active_harnesses.get(session_id)
        if harness is None:
            logger.error(
                "Cannot interrupt session %s: no active harness on this worker "
                "(active sessions: %s)",
                session_id,
                list(self._active_harnesses.keys()),
            )
            return False
        harness.interrupt(message or "Session paused by user")
        logger.info("Interrupted harness for session %s", session_id)
        return True

    async def run(self) -> None:
        """Main worker loop.  Pull from queue, wake harness.  Runs forever.

        Uses ``BZPOPMIN`` with a timeout for blocking dequeue.  On each
        item, a bounded async task is spawned via the semaphore.

        Also starts a Redis pub/sub listener for interrupt signals so
        that the API server can pause/stop running sessions.
        """
        logger.info(
            "Orchestrator starting (queue=%s, poll_timeout=%ds)",
            self._queue_key,
            self._poll_timeout,
        )

        # Start interrupt listener in background.
        interrupt_task = asyncio.create_task(
            self._listen_for_interrupts(),
            name="interrupt-listener",
        )

        # Boot-time aggressive orphan sweep — fire-and-forget so dispatch
        # starts immediately.  Restarted workers would otherwise leave
        # any mid-flight session looking "Working on it…" for up to
        # ``_ORPHAN_STALE_SECONDS + _ORPHAN_SWEEP_INTERVAL`` (~6 min)
        # before the steady-state sweeper picks it up; the boot variant
        # uses a much tighter ``_ORPHAN_BOOT_STALE`` (60s, just above
        # lease TTL) so any session whose previous owner died ≥60s ago
        # is recovered within a fresh worker's first heartbeat instead.
        # Live workers' sessions are protected by the lease-validity
        # filter inside ``find_orphaned_sessions`` (see the LEFT JOIN
        # on ``LeaseRow.expires_at > now()``), so this is safe even
        # with 50 concurrent replicas all racing on the same boot.
        asyncio.create_task(
            self._sweep_orphans_on_boot(),
            name="orphan-sweeper-boot",
        )

        # Start the periodic orphan sweeper.  Self-heals sessions abandoned by a
        # dead worker (SIGKILL / OOM / debugger stop / pod eviction) —
        # those paths never hit the in-process exception handler, so
        # no ``HARNESS_CRASH`` or ``SESSION_FAIL`` lands naturally and
        # the UI would otherwise sit on "Working on it..." forever.
        orphan_sweeper_task = asyncio.create_task(
            self._sweep_orphans_forever(),
            name="orphan-sweeper",
        )

        # Start the subagent task layer tick.  Disabled when the caller
        # didn't provide ``session_factory`` and ``tenant_for_task`` —
        # the orchestrator still serves plain spawn_worker / chat
        # sessions, just without the durable Task abstraction.
        tasks_tick_task: asyncio.Task | None = None
        if self._session_factory is not None and self._tenant_for_task is not None:
            tasks_tick_task = asyncio.create_task(
                self._tasks_tick_forever(),
                name="tasks-tick",
            )
        else:
            logger.warning(
                "Orchestrator started without session_factory/tenant_for_task; "
                "tasks_tick is disabled. spawn_task tools will create rows but "
                "deferred (todo) tasks will not be promoted by this worker."
            )

        while self._running:
            try:
                # shared queue + per-tenant gate.
                # The dispatcher pops the next session whose tenant
                # has gate capacity; over-cap candidates are requeued
                # with backoff inside dequeue_next_session itself.
                dequeued = await dequeue_next_session(
                    self.redis,
                    gate=self._turn_gate,
                    gate_limit=None,
                    timeout=self._poll_timeout,
                )
            except asyncio.CancelledError:
                logger.info("Orchestrator cancelled during poll")
                break
            except Exception:
                logger.exception("Redis BZPOPMIN failed; retrying after delay")
                await asyncio.sleep(1.0)
                continue

            if dequeued is None:
                # Either the timeout elapsed with an empty queue or
                # every popped candidate was over-cap.  Sleep
                # briefly so a freshly-released slot gets picked up
                # quickly without busy-looping.
                await asyncio.sleep(_GATE_EMPTY_SLEEP_SECONDS)
                continue

            session_id_str = dequeued.session_id
            try:
                session_id = UUID(session_id_str)
            except ValueError:
                logger.error("Invalid session ID in work queue: %s", session_id_str)
                if self._turn_gate is not None:
                    # The gate slot was acquired but the session is
                    # bad — release immediately so the tenant isn't
                    # billed for a slot that produces no work.
                    await self._turn_gate.release(
                        dequeued.org_id, dequeued.agent_id,
                    )
                continue

            # Spawn a bounded task.
            await self.semaphore.acquire()
            task = asyncio.create_task(
                self._guarded_process(session_id, dequeued=dequeued),
                name=f"harness-{session_id_str[:8]}",
            )
            self._tasks.add(task)
            task.add_done_callback(self._task_done)

        # Stop background helpers.
        helpers = [interrupt_task, orphan_sweeper_task]
        if tasks_tick_task is not None:
            helpers.append(tasks_tick_task)
        for task in helpers:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        # Wait for in-flight tasks to drain.
        if self._tasks:
            logger.info(
                "Orchestrator shutting down; waiting for %d in-flight tasks",
                len(self._tasks),
            )
            await asyncio.gather(*self._tasks, return_exceptions=True)

        logger.info("Orchestrator stopped")

    async def shutdown(self) -> None:
        """Graceful shutdown -- stop accepting work, wait for in-flight tasks."""
        logger.info("Orchestrator shutdown requested")
        self._running = False

    # ------------------------------------------------------------------
    # Internal dispatch
    # ------------------------------------------------------------------

    async def _guarded_process(
        self,
        session_id: UUID,
        *,
        dequeued: DequeuedSession | None = None,
    ) -> None:
        """Wrapper that always releases the semaphore + TurnConcurrencyGate slot."""
        try:
            await self._process(session_id)
        finally:
            self.semaphore.release()
            # release the gate slot acquired in
            # the dispatch loop so the tenant's next queued session
            # can come off the queue.
            if dequeued is not None and self._turn_gate is not None:
                try:
                    await self._turn_gate.release(
                        dequeued.org_id, dequeued.agent_id,
                    )
                except Exception:
                    logger.warning(
                        "Failed to release TurnConcurrencyGate slot for "
                        "org=%s agent=%s; the counter is bounded at zero "
                        "by release() so this won't drive it negative",
                        dequeued.org_id, dequeued.agent_id,
                        exc_info=True,
                    )

    async def _requeue_busy_session(self, session_id: UUID) -> None:
        await asyncio.sleep(_LEASE_BUSY_REQUEUE_DELAY)
        # the shared queue needs the tenant tuple.
        # Look up the session row once; this is a cold path (only
        # taken when another worker holds the lease).
        session = await self.session_store.get_session(session_id)
        await enqueue_session(
            self.redis,
            org_id=str(session.org_id),
            agent_id=session.agent_id,
            session_id=session_id,
        )

    # ── Crash-loop breaker state (Redis, shared across replicas) ──────

    def _crash_loop_key(self, session_id: UUID) -> str:
        return f"{_CRASH_LOOP_KEY_PREFIX}{session_id}"

    async def _crash_loop_state(self, session_id: UUID) -> dict[str, Any] | None:
        try:
            raw = await self.redis.get(self._crash_loop_key(session_id))
        except Exception:
            logger.warning(
                "Failed to read crash-loop state for session %s",
                session_id, exc_info=True,
            )
            return None
        if not raw:
            return None
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", "replace")
        try:
            state = json.loads(raw)
        except (TypeError, ValueError):
            return None
        return state if isinstance(state, dict) else None

    async def _save_crash_loop_state(
        self, session_id: UUID, state: dict[str, Any],
    ) -> None:
        try:
            await self.redis.set(
                self._crash_loop_key(session_id),
                json.dumps(state),
                ex=_CRASH_LOOP_TTL_SECONDS,
            )
        except Exception:
            logger.warning(
                "Failed to save crash-loop state for session %s",
                session_id, exc_info=True,
            )

    async def _clear_crash_loop_state(self, session_id: UUID) -> None:
        try:
            await self.redis.delete(self._crash_loop_key(session_id))
        except Exception:
            logger.warning(
                "Failed to clear crash-loop state for session %s",
                session_id, exc_info=True,
            )

    async def _record_crash(self, session_id: UUID, fingerprint: str) -> int:
        """Bump the consecutive-identical-crash counter; reset on a new error."""
        state = await self._crash_loop_state(session_id) or {}
        if state.get("fingerprint") == fingerprint:
            count = int(state.get("count", 0)) + 1
        else:
            count = 1
        await self._save_crash_loop_state(session_id, {
            "fingerprint": fingerprint,
            "count": count,
            "tripped": bool(state.get("tripped")) if state.get("fingerprint") == fingerprint else False,
            "tripped_event_id": state.get("tripped_event_id"),
        })
        return count

    async def _has_user_signal_since(
        self, session_id: UUID, after_event_id: int | None,
    ) -> bool:
        """A real (non-synthetic) user message or an explicit user retry.

        Synthetic user messages — mission continuations in particular —
        must NOT count: they re-injected the same poisoned conversation
        every iteration in the original incident.
        """
        events = await self.session_store.get_events(
            session_id,
            after=after_event_id,
            types=[EventType.USER_MESSAGE, EventType.SESSION_RESUME],
        )
        for event in events:
            data = event.data or {}
            if (
                event.type == EventType.USER_MESSAGE.value
                and not data.get("synthetic")
            ):
                return True
            if (
                event.type == EventType.SESSION_RESUME.value
                and data.get("source") == "user_retry"
            ):
                return True
        return False

    async def _process(self, session_id: UUID, attempt: int = 0) -> None:
        """Process a single session.  Retry with exponential backoff on failure."""
        from surogates.trace import new_span, new_trace

        # First attempt gets a fresh trace; retries get child spans so
        # they stay linked to the original trace for correlation.
        if attempt == 0:
            new_trace()
        else:
            new_span()

        try:
            if session_id in self._active_harnesses:
                # A wake is already running for this session on this worker
                # (e.g. a child worker just emitted ``WORKER_COMPLETE`` and
                # enqueued the parent while the parent's coordinator wake is
                # mid-stream).  Defer to the active wake: it will enqueue
                # once when it returns.  Avoids a 4 Hz spin loop on Redis
                # for the entire lifetime of a long wake.
                self._rewake_pending.add(session_id)
                logger.debug(
                    "Session %s already active on this worker; deferring rewake",
                    session_id,
                )
                return

            if attempt == 0:
                state = await self._crash_loop_state(session_id)
                if state and state.get("tripped"):
                    if await self._has_user_signal_since(
                        session_id, state.get("tripped_event_id"),
                    ):
                        await self._clear_crash_loop_state(session_id)
                    else:
                        logger.warning(
                            "Session %s crash-loop breaker is open "
                            "(fingerprint %s); skipping wake until a real "
                            "user message or user retry arrives",
                            session_id,
                            state.get("fingerprint"),
                        )
                        return

            harness = self.harness_factory(session_id)
            # Support both sync and async factories.
            if hasattr(harness, "__await__"):
                harness = await harness
            # Track the active harness so interrupt signals can reach it.
            self._active_harnesses[session_id] = harness
            wake_result: str | None = None
            try:
                wake_result = await harness.wake(session_id)
            finally:
                self._active_harnesses.pop(session_id, None)
                # per-session SessionLLMClients
                # bundle.  Close its four connection pools so a
                # long-running worker process doesn't accumulate one
                # pool per processed session.
                bundle = getattr(harness, "_session_llm_bundle", None)
                if bundle is not None:
                    try:
                        await bundle.aclose()
                    except Exception:
                        logger.warning(
                            "Failed to aclose SessionLLMClients for "
                            "session %s", session_id, exc_info=True,
                        )

            # Wake finished without crashing — any crash streak is broken.
            # (Skip lease_held: no work actually ran on this worker.)
            if wake_result != "lease_held":
                await self._clear_crash_loop_state(session_id)

            # Slot is free; settle any follow-up enqueue.  ``lease_held``
            # (another worker owns the lease) backs off briefly first; a
            # deferred rewake fires immediately because the relevant wake
            # just finished.  Redis ZADD dedupes if both apply.
            rewake_pending = session_id in self._rewake_pending
            self._rewake_pending.discard(session_id)
            if wake_result == "lease_held":
                logger.info(
                    "Session %s lease is held; requeueing wake",
                    session_id,
                )
                await self._requeue_busy_session(session_id)
            elif rewake_pending:
                # tenant tuple required.
                rewake_session = await self.session_store.get_session(
                    session_id,
                )
                await enqueue_session(
                    self.redis,
                    org_id=str(rewake_session.org_id),
                    agent_id=rewake_session.agent_id,
                    session_id=session_id,
                )
        except Exception as exc:
            logger.exception(
                "Harness failed for session %s (attempt %d/%d)",
                session_id,
                attempt + 1,
                _MAX_RETRIES,
            )

            fingerprint = _crash_fingerprint(exc)
            crash_count = await self._record_crash(session_id, fingerprint)
            if crash_count >= _CRASH_LOOP_THRESHOLD:
                logger.error(
                    "Session %s crashed %d consecutive times with identical "
                    "fingerprint %s; tripping crash-loop breaker and failing "
                    "terminally",
                    session_id,
                    crash_count,
                    fingerprint,
                )
                info = classify_harness_error(exc)
                fail_event_id: int | None = None
                try:
                    fail_event_id = await self.session_store.emit_event(
                        session_id,
                        EventType.SESSION_FAIL,
                        {
                            "reason": "crash_loop_detected",
                            "error": str(exc),
                            "traceback": traceback.format_exc()[-2000:],
                            "attempts": crash_count,
                            "error_category": info.category,
                            "error_title": info.title,
                            "error_detail": info.detail,
                            "retryable": False,
                            "fingerprint": fingerprint,
                        },
                    )
                    await self.session_store.update_session_status(
                        session_id,
                        "failed",
                    )
                except Exception:
                    logger.exception(
                        "Failed to emit SESSION_FAIL for session %s",
                        session_id,
                    )
                await self._save_crash_loop_state(session_id, {
                    "fingerprint": fingerprint,
                    "count": crash_count,
                    "tripped": True,
                    "tripped_event_id": fail_event_id,
                })
                return

            if attempt + 1 < _MAX_RETRIES:
                delay = _BASE_RETRY_DELAY * (2**attempt)
                logger.info(
                    "Retrying session %s in %.1fs (attempt %d/%d)",
                    session_id,
                    delay,
                    attempt + 2,
                    _MAX_RETRIES,
                )
                await asyncio.sleep(delay)
                await self._process(session_id, attempt=attempt + 1)
            else:
                # All retries exhausted -- emit SESSION_FAIL.
                logger.error(
                    "All retries exhausted for session %s; emitting SESSION_FAIL",
                    session_id,
                )
                info = classify_harness_error(exc)
                try:
                    await self.session_store.emit_event(
                        session_id,
                        EventType.SESSION_FAIL,
                        {
                            "reason": "max_retries_exhausted",
                            "error": str(exc),
                            "traceback": traceback.format_exc()[-2000:],
                            "attempts": _MAX_RETRIES,
                            "error_category": info.category,
                            "error_title": info.title,
                            "error_detail": info.detail,
                            "retryable": info.retryable,
                        },
                    )
                    await self.session_store.update_session_status(
                        session_id,
                        "failed",
                    )
                except Exception:
                    logger.exception(
                        "Failed to emit SESSION_FAIL for session %s",
                        session_id,
                    )

    def _task_done(self, task: asyncio.Task) -> None:
        """Remove the task from the tracking set on completion."""
        self._tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("Task %s raised: %s", task.get_name(), exc)

    async def _listen_for_interrupts(self) -> None:
        """Subscribe to Redis pub/sub for session interrupt signals.

        The API server publishes to ``{INTERRUPT_CHANNEL_PREFIX}:{session_id}``
        when a session is paused.  This listener delivers the signal to
        the running harness on this worker.
        """
        import json as _json

        pubsub = self.redis.pubsub()
        await pubsub.psubscribe(f"{INTERRUPT_CHANNEL_PREFIX}:*")
        logger.info("Interrupt listener subscribed to %s:*", INTERRUPT_CHANNEL_PREFIX)

        try:
            async for message in pubsub.listen():
                # The worker Redis uses decode_responses=False, so
                # message fields (type, channel, data) are bytes.
                msg_type = message["type"]
                if isinstance(msg_type, bytes):
                    msg_type = msg_type.decode()
                if msg_type != "pmessage":
                    continue
                try:
                    channel = message["channel"]
                    if isinstance(channel, bytes):
                        channel = channel.decode()
                    # Channel format: surogates:interrupt:<session_id>
                    session_id_str = channel.rsplit(":", 1)[-1]
                    session_id = UUID(session_id_str)

                    data = message.get("data", b"{}")
                    if isinstance(data, bytes):
                        data = data.decode()
                    payload = _json.loads(data) if data else {}
                    reason = payload.get("reason", "interrupted")

                    await self._handle_interrupt_signal(session_id, reason)
                except Exception:
                    logger.warning(
                        "Failed to process interrupt message: %s",
                        message,
                        exc_info=True,
                    )
        except asyncio.CancelledError:
            pass
        finally:
            await pubsub.punsubscribe(f"{INTERRUPT_CHANNEL_PREFIX}:*")
            await pubsub.aclose()

    async def _handle_interrupt_signal(self, session_id: UUID, reason: str) -> None:
        delivered = self.interrupt_session(session_id, reason)
        if reason == "session deleted" and self._browser_pool is not None:
            try:
                await self._browser_pool.destroy_for_session(str(session_id))
            except Exception:
                logger.warning(
                    "Failed to destroy browser sandbox for deleted session %s",
                    session_id,
                    exc_info=True,
                )
        if not delivered:
            logger.warning(
                "Interrupt for session %s could not be delivered "
                "(no active harness on this worker)",
                session_id,
            )

    async def _sweep_orphans_once(self, *, stale_seconds: int, reason: str) -> int:
        """Run a single orphan-sweep pass; return the number recovered.

        Extracted from :meth:`_sweep_orphans_forever` so the boot-time
        aggressive sweep (:meth:`_sweep_orphans_on_boot`) can reuse the
        exact same recovery path with a tighter ``stale_seconds`` value.
        """
        recovered = 0
        try:
            orphans = await self.session_store.find_orphaned_sessions(
                stale_seconds=stale_seconds,
                agent_id=self._agent_id,
            )
        except Exception:
            logger.exception("Orphan sweep failed; continuing")
            return 0
        for session in orphans:
            # browser_setup sessions are interactive and have no agent loop to
            # recover; their lifecycle is the pod TTL + the capture/cancel
            # teardown. They sit ``active`` and leaseless by design, so the
            # sweeper would otherwise re-wake them every cycle and re-provision
            # the browser out from under the live view.
            if getattr(session, "channel", None) == "browser_setup":
                continue
            try:
                await self.session_store.emit_event(
                    session.id,
                    EventType.HARNESS_RECOVERED,
                    {
                        "recovered_by": reason,
                        "stale_seconds": stale_seconds,
                    },
                )
                await self.session_store.release_stale_lease(session.id)
                # Compensate the TurnConcurrencyGate.  A session that
                # reaches the orphan sweeper got there because its
                # previous owner died WITHOUT running the dispatcher's
                # finally branch -- which is the only path that
                # ``release()``s the gate slot.  Left unhandled, every
                # debugger-stop / OOM / pod-eviction leaks one slot
                # per in-flight session for this (org, agent); a few
                # cycles of that drives the counter to its cap and
                # every subsequent dequeue is rejected, leaving fresh
                # sessions stuck in an endless re-enqueue loop with
                # no diagnostic anywhere.
                #
                # Floor-at-zero in ``TurnGate.release()`` protects
                # against double-release if this recovery races a
                # late-arriving finally on the original owner (the
                # owner is by definition gone at this point, but the
                # floor keeps us honest).
                if self._turn_gate is not None:
                    try:
                        await self._turn_gate.release(
                            str(session.org_id), session.agent_id,
                        )
                    except Exception:
                        logger.warning(
                            "Failed to release turn gate slot for "
                            "recovered session %s (org=%s agent=%s)",
                            session.id, session.org_id, session.agent_id,
                            exc_info=True,
                        )
                await enqueue_session(
                    self.redis,
                    org_id=str(session.org_id),
                    agent_id=session.agent_id,
                    session_id=session.id,
                )
                recovered += 1
                logger.warning(
                    "Recovered orphaned session %s (%s, stale>=%ds) — re-enqueued",
                    session.id, reason, stale_seconds,
                )
            except Exception:
                logger.exception(
                    "Failed to recover orphaned session %s",
                    session.id,
                )
        return recovered

    async def _sweep_orphans_on_boot(self) -> None:
        """One-shot aggressive sweep right after worker start.

        A normal restart leaves any mid-flight session looking
        "running" in the UI for up to ``_ORPHAN_STALE_SECONDS + 60s``
        (5+ minutes) before the periodic sweeper re-enqueues it,
        because the steady-state threshold is set high to avoid racing
        with genuinely slow turns on healthy workers.

        On boot we know THIS process didn't own those leases (we just
        started), so we can recover much faster.  ``_ORPHAN_BOOT_STALE``
        is set just above the lease TTL so a turn that was actively
        being processed by another live worker on the cluster isn't
        falsely flagged — ``release_stale_lease`` only releases
        EXPIRED leases anyway, so a live worker's lease is safe.
        """
        try:
            count = await self._sweep_orphans_once(
                stale_seconds=_ORPHAN_BOOT_STALE,
                reason="orchestrator_boot_sweep",
            )
            if count > 0:
                logger.info(
                    "Boot-time orphan sweep recovered %d session(s)", count,
                )
        except asyncio.CancelledError:
            return

    async def _sweep_orphans_forever(self) -> None:
        """Periodically re-enqueue sessions abandoned by a dead worker.

        The in-process retry path in :meth:`_process` only catches
        exceptions raised *inside* a live harness.  If the worker itself
        is hard-killed mid-turn (SIGKILL, OOM, pod eviction, debugger
        stop), none of those handlers run: the lease expires naturally
        on its TTL, no ``HARNESS_CRASH`` is ever emitted, and the
        session sits at ``status='active'`` with a streaming last
        message — the UI reads this as "still working" and never
        escapes.

        Every replica runs this sweeper on its own agent's sessions.
        ``enqueue_session`` uses ``ZADD`` so concurrent replicas racing
        on the same orphan de-duplicate naturally; the harness's
        ``try_acquire_lease`` serializes actual execution.

        A small random offset staggers the first tick across replicas
        so a freshly-restarted cluster doesn't hit the DB in unison.
        """
        await asyncio.sleep(random.uniform(0, _ORPHAN_SWEEP_INTERVAL))

        while self._running:
            try:
                await self._sweep_orphans_once(
                    stale_seconds=_ORPHAN_STALE_SECONDS,
                    reason="orchestrator_sweeper",
                )
            except asyncio.CancelledError:
                raise

            try:
                await asyncio.sleep(_ORPHAN_SWEEP_INTERVAL)
            except asyncio.CancelledError:
                return

    async def _tasks_tick_forever(self) -> None:
        """Run one ``tasks_tick`` every ``_TASKS_TICK_INTERVAL`` seconds.

        Each tick:
        1. Promotes ``todo`` Tasks whose every parent has reached ``done``.
        2. Finalises Tasks whose worker Session ended (done / retry / failed).
        3. Atomically claims ``ready`` Tasks and enqueues their child Sessions.

        Errors in a single tick are logged and swallowed so a transient
        DB hiccup doesn't kill the loop. Cancellation (orchestrator
        shutdown) is propagated normally.

        Caller invariant: ``self._session_factory`` and
        ``self._tenant_for_task`` are non-None (checked at start in
        :meth:`run`).
        """
        from surogates.tasks.dispatcher import tasks_tick

        # Small random offset so replicas don't all tick in unison after
        # a fleet restart — keeps the lock contention bounded.
        await asyncio.sleep(random.uniform(0, _TASKS_TICK_INTERVAL))

        while self._running:
            try:
                counts = await tasks_tick(
                    session_factory=self._session_factory,
                    redis=self.redis,
                    session_store=self.session_store,
                    tenant_for_task=self._tenant_for_task,
                    file_bundle_cache=self._file_bundle_cache,
                )
                if any(counts.values()):
                    logger.debug(
                        "tasks_tick: promoted=%d finalized=%d enqueued=%d",
                        counts["promoted"], counts["finalized"], counts["enqueued"],
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("tasks_tick failed; continuing")

            try:
                await asyncio.sleep(_TASKS_TICK_INTERVAL)
            except asyncio.CancelledError:
                return
