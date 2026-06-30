"""Leader-locked ticker that fires due ambient schedules.

Mirrors surogates.scheduled.platform_ticker: acquire leader lock, claim due
rows, materialize each (isolating per-row failures), sleep, repeat.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)


class AmbientTicker:
    def __init__(
        self,
        store: Any,
        *,
        redis: Any,
        materialize: Callable[[Any], Awaitable[None]],
        worker_id: str,
        leader_lock: Any = None,
        tick_interval_seconds: float = 30.0,
        claim_limit: int = 50,
    ) -> None:
        self._store = store
        self._redis = redis
        self._materialize = materialize
        self._worker_id = worker_id
        self._lock = leader_lock
        self._interval = tick_interval_seconds
        self._claim_limit = claim_limit
        self._stop = asyncio.Event()

    def request_stop(self) -> None:
        self._stop.set()

    async def tick_once(self) -> None:
        rows = await self._store.claim_due(
            worker_id=self._worker_id, limit=self._claim_limit,
        )
        for row in rows:
            try:
                await self._materialize(row)
            except Exception:
                logger.exception(
                    "ambient ticker failed to materialize channel %s",
                    getattr(row, "channel_id", "?"),
                )

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                if self._lock is None or await self._lock.acquire():
                    try:
                        await self.tick_once()
                    finally:
                        if self._lock is not None:
                            await self._lock.release()
            except Exception:
                logger.exception("ambient ticker tick failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            except asyncio.TimeoutError:
                pass
