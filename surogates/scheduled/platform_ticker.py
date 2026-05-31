"""Platform-level scheduled-work ticker.

Replaces the per-tenant
:class:`surogates.scheduled.runner.ScheduledSessionRunner`
 with a single platform-level process that:

* Acquires a Redis leader lock (:class:`RedisLeaderLock`, Task
  1+2) so only one replica fires at a time across N replicas.
* Polls multi-tenant due rows in one DB query
  (:meth:`ScheduledSessionStore.find_due_across_tenants`, Task
  4).
* Enqueues each row via ``enqueue_session``.
* Sleeps ``tick_interval_seconds`` and loops.

Loss-of-lock semantics: a heartbeat returning False means we
have lost the lease (probably to a slow tick) and another
replica is now leader.  We stop dispatching this tick
immediately to avoid the canonical double-fire risk where our
slow tick races with the new leader's tick on the same
already-claimed rows.

Two heartbeats per tick: one before the (potentially expensive)
DB query, one before the enqueue loop.  The second catches a
slow DB read pushing us past the TTL boundary.

Shutdown: :meth:`request_stop` sets an event that the loop
checks between sleep boundaries; the next iteration releases
the lock and exits.  SIGTERM / SIGINT plumbing lives in
:func:`main` so this class stays signal-agnostic.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from surogates.runtime.leader_lock import RedisLeaderLock

logger = logging.getLogger(__name__)


EnqueueFn = Callable[..., Awaitable[None]]


class PlatformTicker:
    def __init__(
        self,
        *,
        lock: Any,
        store: Any,
        enqueue: EnqueueFn,
        tick_interval_seconds: float,
        worker_id: str,
        claim_limit: int = 100,
        claim_lease_seconds: int = 30,
    ) -> None:
        self._lock = lock
        self._store = store
        self._enqueue = enqueue
        self._tick_interval = tick_interval_seconds
        self._worker_id = worker_id
        self._claim_limit = claim_limit
        self._claim_lease_seconds = claim_lease_seconds
        self._stop = asyncio.Event()

    def request_stop(self) -> None:
        """Signal the loop to exit cleanly on the next iteration."""
        self._stop.set()

    async def run(self) -> None:
        """Main loop.  Acquire -> tick -> release -> sleep ->
        repeat.  Exits cleanly when :meth:`request_stop` fires."""
        try:
            while not self._stop.is_set():
                acquired = await self._lock.acquire()
                if not acquired:
                    # Another replica is leader; sleep and retry.
                    # Skip the DB query entirely -- saves a
                    # round-trip when we're not the leader.
                    await self._sleep_or_stop()
                    continue

                try:
                    await self._tick_once()
                finally:
                    await self._lock.release()

                await self._sleep_or_stop()
        finally:
            # Defensive: ensure release runs even if the loop
            # exits via cancellation.
            try:
                await self._lock.release()
            except Exception:  # noqa: BLE001 — best-effort
                pass

    async def _tick_once(self) -> None:
        # Heartbeat once to confirm we still hold the lock
        # before the (potentially expensive) DB query.
        if not await self._lock.heartbeat():
            logger.warning(
                "platform_ticker lost lease before tick start",
            )
            return

        rows = await self._store.find_due_across_tenants(
            worker_id=self._worker_id,
            limit=self._claim_limit,
            lease_seconds=self._claim_lease_seconds,
        )

        # Heartbeat again before enqueue so a slow DB read
        # cannot push us past the TTL boundary.
        if not await self._lock.heartbeat():
            logger.warning(
                "platform_ticker lost lease after DB read; %d "
                "rows dropped this tick",
                len(rows),
            )
            return

        for row in rows:
            await self._enqueue(
                org_id=_field(row, "org_id"),
                agent_id=_field(row, "agent_id"),
                session_id=_field(row, "id"),
            )

    async def _sleep_or_stop(self) -> None:
        try:
            await asyncio.wait_for(
                self._stop.wait(), timeout=self._tick_interval,
            )
        except asyncio.TimeoutError:
            pass


def _field(row: Any, name: str) -> Any:
    """Accept either a dict (test fakes) or a Pydantic model
    (the production ``ScheduledSession``)."""
    if isinstance(row, dict):
        return row[name]
    return getattr(row, name)


async def main(
    *,
    settings: Any,
    redis_factory: Callable[[str], Awaitable[Any]] | None = None,
    store_factory: Callable[[Any], Awaitable[Any]] | None = None,
    enqueue: EnqueueFn | None = None,
    install_signal_handlers: bool = True,
) -> None:
    """CLI entry for ``python -m surogates.scheduled.platform_ticker``.

    Wires:

    * Redis client from ``settings.redis.url``
    * :class:`ScheduledSessionStore` from ``settings.db``
    * ``enqueue_session`` closure bound to the Redis client
    * :class:`RedisLeaderLock` with ``holder_id``
      ``{worker_id}-{pid}`` so a restart of the same K8s pod
      acquires a fresh holder identity (avoids the
      release-checks-identity edge where a restarted pod's
      release would no-op against itself)

    Dependency-injection seams (``redis_factory``,
    ``store_factory``, ``enqueue``) let tests substitute fakes
    without standing up real Redis + Postgres.  Production
    callers pass None and the defaults wire the real
    implementations.
    """
    import os
    import signal

    if redis_factory is None:
        async def _real_redis(url):  # pragma: no cover - prod path
            from redis.asyncio import Redis
            return Redis.from_url(url, decode_responses=False)
        redis_factory = _real_redis
    if store_factory is None:
        async def _real_store(s):  # pragma: no cover - prod path
            from surogates.db.engine import (
                async_engine_from_settings, async_session_factory,
            )
            from surogates.scheduled.store import ScheduledSessionStore
            engine = async_engine_from_settings(s.db)
            return ScheduledSessionStore(async_session_factory(engine))
        store_factory = _real_store

    redis = await redis_factory(settings.redis.url)
    store = await store_factory(settings)

    if enqueue is None:
        from surogates.config import enqueue_session as _enq

        async def enqueue(*, org_id, agent_id, session_id, priority=0):
            await _enq(
                redis, org_id=org_id, agent_id=agent_id,
                session_id=session_id, priority=priority,
            )

    sched = settings.scheduled_sessions
    lock_key = getattr(
        sched, "leader_lock_key", "surogates:scheduled_ticker:leader",
    )
    lock_ttl = int(getattr(sched, "leader_ttl_seconds", 10))
    tick_interval = float(
        getattr(sched, "tick_interval_seconds", 5),
    )

    worker_id = getattr(settings, "worker_id", "platform-ticker")
    holder_id = f"{worker_id}-{os.getpid()}"

    lock = RedisLeaderLock(
        redis, key=lock_key, ttl_seconds=lock_ttl, holder_id=holder_id,
    )
    ticker = PlatformTicker(
        lock=lock, store=store, enqueue=enqueue,
        tick_interval_seconds=tick_interval, worker_id=worker_id,
    )

    if install_signal_handlers:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, ticker.request_stop)
            except NotImplementedError:  # pragma: no cover - Windows
                pass

    try:
        await ticker.run()
    finally:
        close = getattr(redis, "aclose", None) or getattr(
            redis, "close", None,
        )
        if close is not None:
            try:
                await close()
            except Exception:  # noqa: BLE001 — best-effort
                pass


if __name__ == "__main__":  # pragma: no cover - CLI path
    from surogates.config import load_settings
    asyncio.run(main(settings=load_settings()))
