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
import logging
import random
import traceback
from typing import TYPE_CHECKING, Any, Callable
from uuid import UUID

from surogates.config import INTERRUPT_CHANNEL_PREFIX, enqueue_session
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

# Orphan sweep cadence.  Must comfortably exceed the lease TTL (60s) and
# the stream-stale timeout (180s) so slow-but-alive turns aren't
# misidentified — 300s leaves a safe margin.  The sweep itself runs
# every ``_ORPHAN_SWEEP_INTERVAL``; a session has to be idle for the
# full ``_ORPHAN_STALE_SECONDS`` before it's considered orphaned.
_ORPHAN_SWEEP_INTERVAL: float = 60.0
_ORPHAN_STALE_SECONDS: int = 300

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


class Orchestrator:
    """Pulls session IDs from a Redis sorted-set and dispatches them to the agent harness.

    The ``queue_key`` is per-agent (``surogates:work_queue:<agent_id>``) so that
    multiple agents sharing a single Redis do not compete for each other's
    sessions.  Callers build the key via :func:`surogates.config.agent_queue_key`.
    """

    def __init__(
        self,
        redis_client: Redis,
        session_store: SessionStore,
        harness_factory: Callable[..., Any],
        *,
        agent_id: str,
        queue_key: str,
        max_concurrent: int = 50,
        poll_timeout: int = 5,
        browser_pool: BrowserPool | None = None,
        session_factory: Any | None = None,
        tenant_for_task: Callable[[Any], Any] | None = None,
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

        # Start the orphan sweeper.  Self-heals sessions abandoned by a
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
                # BZPOPMIN returns (key, member, score) or None on timeout.
                result = await self.redis.bzpopmin(
                    self._queue_key,
                    timeout=self._poll_timeout,
                )
            except asyncio.CancelledError:
                logger.info("Orchestrator cancelled during poll")
                break
            except Exception:
                logger.exception("Redis BZPOPMIN failed; retrying after delay")
                await asyncio.sleep(1.0)
                continue

            if result is None:
                # Timeout -- no work available.
                continue

            # result is (key, member, score) for redis-py >= 5.
            _key, member, _score = result
            session_id_str = (
                member.decode() if isinstance(member, bytes) else str(member)
            )

            try:
                session_id = UUID(session_id_str)
            except ValueError:
                logger.error("Invalid session ID in work queue: %s", session_id_str)
                continue

            # Spawn a bounded task.
            await self.semaphore.acquire()
            task = asyncio.create_task(
                self._guarded_process(session_id),
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

    async def _guarded_process(self, session_id: UUID) -> None:
        """Wrapper that always releases the semaphore."""
        try:
            await self._process(session_id)
        finally:
            self.semaphore.release()

    async def _requeue_busy_session(self, session_id: UUID) -> None:
        await asyncio.sleep(_LEASE_BUSY_REQUEUE_DELAY)
        await enqueue_session(self.redis, self._agent_id, session_id)

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
                # Plan 2 / Task 7 — per-session SessionLLMClients
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
                await enqueue_session(self.redis, self._agent_id, session_id)
        except Exception as exc:
            logger.exception(
                "Harness failed for session %s (attempt %d/%d)",
                session_id,
                attempt + 1,
                _MAX_RETRIES,
            )

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
                orphans = await self.session_store.find_orphaned_sessions(
                    stale_seconds=_ORPHAN_STALE_SECONDS,
                    agent_id=self._agent_id,
                )
                for session in orphans:
                    try:
                        await self.session_store.emit_event(
                            session.id,
                            EventType.HARNESS_RECOVERED,
                            {
                                "recovered_by": "orchestrator_sweeper",
                                "stale_seconds": _ORPHAN_STALE_SECONDS,
                            },
                        )
                        await self.session_store.release_stale_lease(session.id)
                        await enqueue_session(
                            self.redis,
                            session.agent_id,
                            session.id,
                        )
                        logger.warning(
                            "Recovered orphaned session %s — re-enqueued",
                            session.id,
                        )
                    except Exception:
                        logger.exception(
                            "Failed to recover orphaned session %s",
                            session.id,
                        )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Orphan sweep failed; continuing")

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
