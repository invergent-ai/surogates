"""Per-tenant concurrency limiter for in-flight turns.

The dispatcher consults the gate before handing a
dequeued session to a worker; tenants that have already hit their
max-concurrent-turns budget have their session requeued so a noisy
tenant cannot drain the worker pool.

Distinct from :class:`PerTenantRateLimiter` which
is a request-rate limit (per-minute window).  The gate counts the turns
currently holding the tenant's slots, and a turn gives its slot back when
the dispatcher retires it, or while it waits on something outside the worker.

Keys are ``surogates:turn_holders:<org_id>:<agent_id>``, a set of turn
holders, ``<session_id>:<turn>``.  Taking a slot adds the holder, and
releasing removes it, so a release by a holder that holds nothing frees
nothing.  A holder whose worker died stays counted until recovery releases
its session's holders.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

__all__ = ["TurnConcurrencyGate", "TurnGateBusy"]


class TurnGateBusy(RuntimeError):
    """Raised by :meth:`TurnConcurrencyGate.acquire` when the tenant
    is already at its max-concurrent-turns budget.  The dispatcher
    catches this and requeues the session with backoff."""


class TurnConcurrencyGate:
    """The turns holding a tenant's turn slots, capped at ``limit``."""

    def __init__(self, redis: Any, *, default_max: int = 10) -> None:
        self._redis = redis
        self._default = default_max

    async def try_acquire(
        self,
        org_id: str,
        agent_id: str,
        *,
        holder: str,
        limit: int | None = None,
    ) -> bool:
        """Count *holder* as holding a slot; True if the tenant is under its cap.

        A holder already counted keeps its one slot.  ``limit=0`` (kill-switch)
        and negative limits reject without touching Redis.
        """
        cap = limit if limit is not None else self._default
        if cap <= 0:
            return False
        key = self._key(org_id, agent_id)
        async with self._redis.pipeline(transaction=True) as pipe:
            added, count = await pipe.sadd(key, holder).scard(key).execute()
        if added and count > cap:
            await self._redis.srem(key, holder)
            return False
        return True

    async def release(self, org_id: str, agent_id: str, *, holder: str) -> bool:
        """Free *holder*'s slot; False if it held none, so nothing else is freed."""
        return bool(await self._redis.srem(self._key(org_id, agent_id), holder))

    async def release_session(self, org_id: str, agent_id: str, session_id: str) -> int:
        """Free every slot a session's turns hold: recovery's release for a dead owner.

        Every holder of the session goes, whichever turn it belongs to.  A live
        turn caught between its dequeue and taking its lease, or in a double
        sweep, therefore loses its holder and is undercounted for the rest of
        that turn.
        """
        key = self._key(org_id, agent_id)
        cursor, freed = 0, 0
        while True:
            cursor, members = await self._redis.sscan(key, cursor, match=f"{session_id}:*", count=100)
            if members:
                freed += await self._redis.srem(key, *members)
            if not cursor:
                return freed

    @asynccontextmanager
    async def acquire(
        self,
        org_id: str,
        agent_id: str,
        *,
        holder: str,
        limit: int | None = None,
    ) -> AsyncIterator[None]:
        """Async context manager: acquires on entry, releases on exit.

        Raises :class:`TurnGateBusy` on entry if the tenant is at its cap.
        """
        if not await self.try_acquire(org_id, agent_id, holder=holder, limit=limit):
            raise TurnGateBusy(
                f"agent {agent_id} (org {org_id}) at max-concurrent-turns",
            )
        try:
            yield
        finally:
            await self.release(org_id, agent_id, holder=holder)

    def _key(self, org_id: str, agent_id: str) -> str:
        return f"surogates:turn_holders:{org_id}:{agent_id}"
