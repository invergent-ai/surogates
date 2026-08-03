"""Per-tenant concurrency limiter for in-flight turns.

The dispatcher consults the gate before handing a
dequeued session to a worker; tenants that have already hit their
max-concurrent-turns budget have their session requeued so a noisy
tenant cannot drain the worker pool.

Distinct from :class:`PerTenantRateLimiter` which
is a request-rate limit (per-minute window).  The gate is a live
counter — exactly tracks how many sessions are currently being
processed for the tenant — and decrements when the dispatcher
retires the session.

Keys: ``surogates:turns:<org_id>:<agent_id>``.  INCR on acquire,
DECR on release; the counter is bounded at zero on release to
survive a stuck-release scenario (e.g. crash recovery double-
releasing).

If the worker pool crashes mid-session and never DECRs, the counter
sticks high until a manual reset. lifecycle adds an admin
``reset_turn_counters`` CLI; today an admin can ``DEL`` the key.
A heartbeat / TTL-based variant is intentionally deferred because
the simple counter is enough for the canary deploy.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

__all__ = ["TurnConcurrencyGate", "TurnGateBusy", "released_for_wait"]

logger = logging.getLogger(__name__)

# Re-acquiring on the way out is best-effort.  If the cap is genuinely
# saturated when the wait ends we proceed without a slot and let the
# dispatcher's outer release fall on the floor-at-zero guard.  Slight
# under-count is acceptable — the cap is a guideline, not a correctness
# constraint — whereas blocking forever waiting for a slot would expire the
# session lease and produce an orphan re-enqueue cycle, a far worse failure.
_REACQUIRE_TIMEOUT_SECONDS: float = 30.0
_REACQUIRE_BACKOFF_SECONDS: float = 0.5


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


async def _reacquire_with_backoff(
    turn_gate: Any, org_id: str, agent_id: str,
) -> bool:
    """Re-acquire a slot, retrying briefly while the tenant is at cap.

    Returns ``True`` on success, ``False`` once
    :data:`_REACQUIRE_TIMEOUT_SECONDS` of failed attempts have elapsed.
    """
    if turn_gate is None:
        return False
    deadline = time.monotonic() + _REACQUIRE_TIMEOUT_SECONDS
    while True:
        try:
            if await turn_gate.try_acquire(org_id, agent_id):
                return True
        except Exception:
            logger.debug(
                "Transient try_acquire failure during re-acquire "
                "(org=%s agent=%s)", org_id, agent_id, exc_info=True,
            )
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(_REACQUIRE_BACKOFF_SECONDS)


@asynccontextmanager
async def released_for_wait(
    turn_gate: Any | None,
    org_id: str,
    agent_id: str,
    *,
    context: str,
) -> AsyncIterator[None]:
    """Hand the tenant's slot back while the caller sleeps; take it again after.

    The gate counts *active work*.  A caller blocked on something external —
    a delegated child session, a human answering a question — consumes no
    worker CPU, so holding its slot for the duration is a category error: a
    handful of waiters saturate the per-tenant cap and every unrelated
    session is requeued behind sleepers.

    Both halves are best-effort and never raise into the body:

    * If the release fails the body still runs, and no re-acquire is
      attempted — re-taking a slot that was never given up would hand the
      tenant a slot it does not own.
    * If the re-acquire fails the body's result still stands.  Losing the
      work a caller already completed to satisfy a soft cap is the wrong
      trade.

    ``context`` names the caller in log lines.
    """
    released = False
    if turn_gate is not None:
        try:
            await turn_gate.release(org_id, agent_id)
            released = True
        except Exception:
            logger.warning(
                "%s: failed to release gate slot (org=%s agent=%s); "
                "proceeding without", context, org_id, agent_id, exc_info=True,
            )
    try:
        yield
    finally:
        if released and not await _reacquire_with_backoff(
            turn_gate, org_id, agent_id,
        ):
            logger.warning(
                "%s: could not re-acquire gate slot within %.0fs "
                "(org=%s agent=%s); continuing without",
                context, _REACQUIRE_TIMEOUT_SECONDS, org_id, agent_id,
            )
