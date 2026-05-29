"""Platform-level scheduled-work ticker.

Plan 8 / Task 5.  Replaces the per-tenant
:class:`surogates.scheduled.runner.ScheduledSessionRunner`
(Plan 2 artifact running inside every worker pod) with a single
platform-level process that:

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
:func:`main` (Task 7) so this class stays signal-agnostic.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

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
