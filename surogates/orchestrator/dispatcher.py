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
import traceback
from typing import TYPE_CHECKING, Any, Callable
from uuid import UUID

from surogates.config import INTERRUPT_CHANNEL_PREFIX
from surogates.session.events import EventType

if TYPE_CHECKING:
    from redis.asyncio import Redis

    from surogates.session.store import SessionStore

logger = logging.getLogger(__name__)

# Maximum number of retry attempts for a single session.
_MAX_RETRIES: int = 3

# Base delay (seconds) for exponential back-off on retry.
_BASE_RETRY_DELAY: float = 1.0


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
        queue_key: str,
        max_concurrent: int = 50,
        poll_timeout: int = 5,
    ) -> None:
        self.redis = redis_client
        self.session_store = session_store
        self.harness_factory = harness_factory
        self.semaphore = asyncio.Semaphore(max_concurrent)
        self._queue_key = queue_key
        self._poll_timeout = poll_timeout
        self._running = True
        self._tasks: set[asyncio.Task] = set()
        # Active harnesses by session ID — for delivering interrupt signals.
        self._active_harnesses: dict[UUID, Any] = {}

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

        while self._running:
            try:
                # BZPOPMIN returns (key, member, score) or None on timeout.
                result = await self.redis.bzpopmin(
                    self._queue_key, timeout=self._poll_timeout,
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
            session_id_str = member.decode() if isinstance(member, bytes) else str(member)

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

        # Stop interrupt listener.
        interrupt_task.cancel()
        try:
            await interrupt_task
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
            harness = self.harness_factory(session_id)
            # Support both sync and async factories.
            if hasattr(harness, "__await__"):
                harness = await harness
            # Track the active harness so interrupt signals can reach it.
            self._active_harnesses[session_id] = harness
            try:
                await harness.wake(session_id)
            finally:
                self._active_harnesses.pop(session_id, None)
        except Exception as exc:
            logger.exception(
                "Harness failed for session %s (attempt %d/%d)",
                session_id,
                attempt + 1,
                _MAX_RETRIES,
            )

            if attempt + 1 < _MAX_RETRIES:
                delay = _BASE_RETRY_DELAY * (2 ** attempt)
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
                try:
                    await self.session_store.emit_event(
                        session_id,
                        EventType.SESSION_FAIL,
                        {
                            "reason": "max_retries_exhausted",
                            "error": str(exc),
                            "traceback": traceback.format_exc()[-2000:],
                            "attempts": _MAX_RETRIES,
                        },
                    )
                    await self.session_store.update_session_status(
                        session_id, "failed",
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

                    delivered = self.interrupt_session(session_id, reason)
                    if not delivered:
                        logger.warning(
                            "Interrupt for session %s could not be delivered "
                            "(no active harness on this worker)",
                            session_id,
                        )
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
