"""Per-tenant concurrency limiter for in-flight turns.

Plan 2 / Task 11.  The dispatcher consults the gate before handing a
dequeued session to a worker; tenants that have already hit their
max-concurrent-turns budget have their session requeued so a noisy
tenant cannot drain the worker pool.

Distinct from :class:`PerTenantRateLimiter` (Plan 1b Task 13) which
is a request-rate limit (per-minute window).  The gate is a live
counter — exactly tracks how many sessions are currently being
processed for the tenant — and decrements when the dispatcher
retires the session.

Keys: ``surogates:turns:<org_id>:<agent_id>``.  INCR on acquire,
DECR on release; the counter is bounded at zero on release to
survive a stuck-release scenario (e.g. crash recovery double-
releasing).

If the worker pool crashes mid-session and never DECRs, the counter
sticks high until a manual reset.  Plan 7 lifecycle adds an admin
``reset_turn_counters`` CLI; today an admin can ``DEL`` the key.
A heartbeat / TTL-based variant is intentionally deferred because
the simple counter is enough for the canary deploy.
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
    """Live per-(org_id, agent_id) counter capped at ``limit``."""

    def __init__(self, redis: Any, *, default_max: int = 10) -> None:
        self._redis = redis
        self._default = default_max

    async def try_acquire(
        self,
        org_id: str,
        agent_id: str,
        *,
        limit: int | None = None,
    ) -> bool:
        """Increment the tenant counter; return True if under the cap.

        If the increment lands over the cap, immediately DECR so the
        counter reflects only acquired slots.  ``limit=0`` (kill-
        switch) and negative limits reject without touching Redis."""
        cap = limit if limit is not None else self._default
        if cap <= 0:
            return False
        key = self._key(org_id, agent_id)
        count = await self._redis.incr(key)
        if count > cap:
            await self._redis.decr(key)
            return False
        return True

    async def release(self, org_id: str, agent_id: str) -> None:
        """Decrement the tenant counter, floor at zero.

        Floor protects against stuck-release scenarios — a crash-
        recovery handler that double-releases must not drive the
        counter negative, or a future acquire would silently exceed
        the cap by however many spurious releases happened."""
        key = self._key(org_id, agent_id)
        new = await self._redis.decr(key)
        if new < 0:
            await self._redis.incr(key)

    @asynccontextmanager
    async def acquire(
        self,
        org_id: str,
        agent_id: str,
        *,
        limit: int | None = None,
    ) -> AsyncIterator[None]:
        """Async context manager: acquires on entry, releases on exit.

        Releases even if the body raises so a panicking handler does
        not permanently consume a slot.  Raises :class:`TurnGateBusy`
        on the entry path if the tenant is over the cap so the
        dispatcher can pick its requeue strategy."""
        ok = await self.try_acquire(org_id, agent_id, limit=limit)
        if not ok:
            raise TurnGateBusy(
                f"agent {agent_id} (org {org_id}) at max-concurrent-turns",
            )
        try:
            yield
        finally:
            await self.release(org_id, agent_id)

    def _key(self, org_id: str, agent_id: str) -> str:
        return f"surogates:turns:{org_id}:{agent_id}"
