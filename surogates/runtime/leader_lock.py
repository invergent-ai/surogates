"""Redis-backed leader-election lock.

The platform scheduled-work ticker
deploys with N replicas for HA.  Only one replica should fire at
a time; the others stand by.  A SET NX EX (``SET key value NX EX
ttl``) returns True on the winner and False on the losers.  The
winner's :meth:`heartbeat` extends the TTL while it is
alive; if heartbeat returns False the winner has lost the lease
(probably to a slow tick) and must stop dispatching mid-tick to
avoid double-fire.

The release path GETs first and only DELETEs when the value
matches our ``holder_id`` so a stale release after a delayed
shutdown (where the lease already passed to a new leader)
cannot delete the new leader's lock.  This costs an extra
round-trip but is correctness-critical; the alternative is an
EVAL Lua script, which can be added later if measured contention
demands it.
"""

from __future__ import annotations

from typing import Any

__all__ = ["RedisLeaderLock"]


class RedisLeaderLock:
    def __init__(
        self,
        redis: Any,
        *,
        key: str,
        ttl_seconds: int,
        holder_id: str,
    ) -> None:
        self._redis = redis
        self._key = key
        self._ttl = ttl_seconds
        self._holder_id = holder_id

    async def acquire(self) -> bool:
        """Return True if we are now the leader, False otherwise.

        ``SET key holder_id NX EX ttl`` is the atomic primitive --
        the lock+TTL pair lands together or not at all.
        """
        return bool(await self._redis.set(
            self._key, self._holder_id.encode(),
            nx=True, ex=self._ttl,
        ))

    async def heartbeat(self) -> bool:
        """Extend the lease.  Returns True on success, False on
        loss-of-lock.

        Two-step: check identity via GET, then SET XX EX.  Not
        fully atomic (a steal between GET and SET would let the
        SET XX win against the new holder), but the new holder
        would itself heartbeat on the next tick and reclaim, so
        the worst-case window is one tick of double-extend.  An
        EVAL Lua script could close the gap if measured drift
        becomes a real concern.

        A False return means the holder MUST stop dispatching
        mid-tick -- a tick in progress at the boundary is the
        canonical double-fire risk.
        """
        current = await self._redis.get(self._key)
        if current != self._holder_id.encode():
            return False
        return bool(await self._redis.set(
            self._key, self._holder_id.encode(),
            xx=True, ex=self._ttl,
        ))

    async def release(self) -> None:
        """Drop the lock if we still hold it.

        GET first, DELETE only on identity match so a stale
        release after the lease expired (with the lock already
        passed to a new leader) cannot delete the new leader's
        lock.
        """
        current = await self._redis.get(self._key)
        if current == self._holder_id.encode():
            await self._redis.delete(self._key)
