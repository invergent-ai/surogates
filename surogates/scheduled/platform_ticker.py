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


RunOneFn = Callable[[Any], Awaitable[None]]
RecoverFn = Callable[[], Awaitable[None]]


class PlatformTicker:
    def __init__(
        self,
        *,
        lock: Any,
        store: Any,
        run_one: RunOneFn,
        tick_interval_seconds: float,
        worker_id: str,
        recover: RecoverFn | None = None,
        claim_limit: int = 100,
        claim_lease_seconds: int = 30,
    ) -> None:
        self._lock = lock
        self._store = store
        # ``run_one`` materializes a run session for one claimed schedule
        # row (create the session, emit its prompt, enqueue the *session*
        # id, and advance the schedule via ``mark_run_created``).  Without
        # it the ticker would enqueue the schedule row id itself, which no
        # session exists for — the dispatcher then fails every tick with
        # SessionNotFoundError and no loop run is ever produced.
        self._run_one = run_one
        # ``recover`` (optional) requeues/recovers stalled dynamic loops
        # across tenants once per tick.  Best-effort: a failure is logged
        # and does not abort the tick.
        self._recover = recover
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

        # Recover stalled dynamic loops before claiming new work.  Runs
        # under the lock; best-effort so a recovery error never drops the
        # rest of the tick.
        if self._recover is not None:
            try:
                await self._recover()
            except Exception:  # noqa: BLE001 — best-effort
                logger.exception("platform_ticker recovery sweep failed")

        rows = await self._store.find_due_across_tenants(
            worker_id=self._worker_id,
            limit=self._claim_limit,
            lease_seconds=self._claim_lease_seconds,
        )

        # Heartbeat again before materialization so a slow DB read
        # cannot push us past the TTL boundary.
        if not await self._lock.heartbeat():
            logger.warning(
                "platform_ticker lost lease after DB read; %d "
                "rows dropped this tick",
                len(rows),
            )
            return

        for row in rows:
            try:
                await self._run_one(row)
            except Exception as exc:  # noqa: BLE001 — one bad row
                # Record the failure so the schedule releases its claim
                # and reschedules; one row's failure must not abort the
                # rest of the tick.
                logger.exception(
                    "platform_ticker failed to materialize run for "
                    "schedule %s",
                    _field(row, "id"),
                )
                try:
                    await self._store.mark_run_failed(row, error=str(exc))
                except Exception:  # noqa: BLE001 — best-effort
                    logger.exception(
                        "platform_ticker could not mark schedule %s "
                        "as failed",
                        _field(row, "id"),
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
    session_store_factory: Callable[[Any], Awaitable[Any]] | None = None,
    storage_factory: Callable[[Any], Awaitable[Any]] | None = None,
    run_one: RunOneFn | None = None,
    recover: RecoverFn | None = None,
    install_signal_handlers: bool = True,
) -> None:
    """CLI entry for ``python -m surogates.scheduled.platform_ticker``.

    Wires:

    * Redis client from ``settings.redis.url``
    * :class:`ScheduledSessionStore` + :class:`SessionStore` sharing one
      engine/session_factory built from ``settings.db``
    * Workspace storage backend from ``settings``
    * ``run_one`` = :func:`materialize_scheduled_run` bound to the above
      (creates the run session, emits its prompt, enqueues the session
      id, advances the schedule)
    * ``recover`` = :func:`recover_stalled_loops` bound to the store
    * :class:`RedisLeaderLock` with ``holder_id``
      ``{worker_id}-{pid}`` so a restart of the same K8s pod
      acquires a fresh holder identity (avoids the
      release-checks-identity edge where a restarted pod's
      release would no-op against itself)

    Dependency-injection seams (``redis_factory``, ``store_factory``,
    ``session_store_factory``, ``storage_factory``, ``run_one``,
    ``recover``) let tests substitute fakes without standing up real
    Redis + Postgres.  Production callers pass None and the defaults wire
    the real implementations.
    """
    import os
    import signal

    if redis_factory is None:
        async def _real_redis(url):  # pragma: no cover - prod path
            from redis.asyncio import Redis
            return Redis.from_url(url, decode_responses=False)
        redis_factory = _real_redis

    # Build the engine/session_factory lazily and share it between the
    # scheduled store and the session store so the ticker uses one pool.
    _shared: dict[str, Any] = {}

    def _session_factory():  # pragma: no cover - prod path
        if "sf" not in _shared:
            from surogates.db.engine import (
                async_engine_from_settings, async_session_factory,
            )
            _shared["sf"] = async_session_factory(
                async_engine_from_settings(settings.db),
            )
        return _shared["sf"]

    if store_factory is None:
        async def _real_store(s):  # pragma: no cover - prod path
            from surogates.scheduled.store import ScheduledSessionStore
            return ScheduledSessionStore(_session_factory())
        store_factory = _real_store

    redis = await redis_factory(settings.redis.url)
    store = await store_factory(settings)

    # The ambient sibling ticker only starts on the fully-internal prod path
    # (where session_store/storage/run_one are all built here).  When a caller
    # injects run_one (tests), the ambient ticker is skipped.
    _run_one_injected = run_one is not None

    if run_one is None:
        if session_store_factory is None:
            async def _real_session_store(s):  # pragma: no cover - prod path
                from surogates.session.store import SessionStore
                return SessionStore(_session_factory(), redis=redis)
            session_store_factory = _real_session_store
        if storage_factory is None:
            async def _real_storage(s):  # pragma: no cover - prod path
                from surogates.storage.backend import create_backend
                return create_backend(s)
            storage_factory = _real_storage

        session_store = await session_store_factory(settings)
        storage = await storage_factory(settings)

        from surogates.scheduled.materialize import materialize_scheduled_run

        async def run_one(row):  # pragma: no cover - prod path
            await materialize_scheduled_run(
                row,
                session_store=session_store,
                scheduled_store=store,
                storage=storage,
                settings=settings,
                redis=redis,
            )

    sched = settings.scheduled_sessions
    lock_key = getattr(
        sched, "leader_lock_key", "surogates:scheduled_ticker:leader",
    )
    lock_ttl = int(getattr(sched, "leader_ttl_seconds", 10))
    tick_interval = float(
        getattr(sched, "tick_interval_seconds", 5),
    )
    claim_limit = int(getattr(sched, "claim_limit", 100))

    if recover is None:
        from surogates.scheduled.materialize import recover_stalled_loops

        async def recover():  # pragma: no cover - prod path
            await recover_stalled_loops(
                scheduled_store=store, redis=redis, limit=claim_limit,
            )

    worker_id = getattr(settings, "worker_id", "platform-ticker")
    holder_id = f"{worker_id}-{os.getpid()}"

    lock = RedisLeaderLock(
        redis, key=lock_key, ttl_seconds=lock_ttl, holder_id=holder_id,
    )
    ticker = PlatformTicker(
        lock=lock, store=store, run_one=run_one, recover=recover,
        tick_interval_seconds=tick_interval, worker_id=worker_id,
        claim_limit=claim_limit,
    )

    # Ambient engine: a sibling ticker (distinct leader lock) fires due
    # ambient-review schedules into dedicated ambient sessions.  Shares this
    # process's redis / session store / settings.
    ambient_ticker = None
    if not _run_one_injected:  # prod path only (session_store built above)
        from surogates.ambient.store import AmbientScheduleStore
        from surogates.ambient.ticker import AmbientTicker
        from surogates.ambient.materialize import materialize_ambient_tick

        ambient_store = AmbientScheduleStore(_session_factory())

        async def _ambient_run_one(row):  # pragma: no cover - prod path
            await materialize_ambient_tick(
                row,
                session_store=session_store,
                ambient_store=ambient_store,
                session_factory=_session_factory(),
                settings=settings,
                redis=redis,
            )

        ambient_lock = RedisLeaderLock(
            redis, key="surogates:ambient_ticker:leader",
            ttl_seconds=lock_ttl, holder_id=holder_id,
        )
        ambient_ticker = AmbientTicker(
            ambient_store, redis=redis, materialize=_ambient_run_one,
            worker_id=worker_id, leader_lock=ambient_lock,
            tick_interval_seconds=tick_interval, claim_limit=claim_limit,
        )

    if install_signal_handlers:
        loop = asyncio.get_running_loop()

        def _stop_all() -> None:
            ticker.request_stop()
            if ambient_ticker is not None:
                ambient_ticker.request_stop()

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, _stop_all)
            except NotImplementedError:  # pragma: no cover - Windows
                pass

    try:
        if ambient_ticker is not None:
            await asyncio.gather(ticker.run(), ambient_ticker.run())
        else:
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
