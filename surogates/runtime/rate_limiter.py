"""Per-tenant Redis-backed rate limiter.

Plan 1b / Tasks 13–14.  Fixed-window counter keyed on
``(org_id, agent_id)`` with a 60-second window:

* :class:`PerTenantRateLimiter` — the storage layer.  ``try_consume``
  returns ``True`` when the request fits in the current window,
  ``False`` otherwise.
* :func:`rate_limit_dep` — the FastAPI dependency that resolves the
  per-request :class:`AgentRuntimeContext` and gates user-input
  handlers with HTTP 429 when the window is full.

The limiter is the simplest possible thing that gives per-tenant
isolation; Plan 6 / Plan 7 can swap in a sliding window or token
bucket without a route-side change because the call shape stays the
same.

Why a fixed window over a sliding one: at 60 s the worst-case burst
behaviour at the boundary is 2 × rpm in a 1-minute span, which is
acceptable for the user-input routes this gates (sessions, prompts,
website chat).  Sliding-window in Redis costs O(N) ops or a sorted
set per call — not worth it at this stage.

The limiter is wired on ``app.state.rate_limiter``.  If unset, the
dep is a pass-through so helm-mode pods (and tests that don't wire a
limiter) keep working unchanged.
"""

from __future__ import annotations

from typing import Any

from fastapi import Depends, HTTPException, Request, status

from surogates.runtime.context import AgentRuntimeContext
from surogates.runtime.resolver import agent_runtime_context_dep

__all__ = ["PerTenantRateLimiter", "rate_limit_dep"]


_WINDOW_SECONDS = 60


class PerTenantRateLimiter:
    """Fixed-window per-tenant counter backed by Redis.

    Keys: ``surogates:rate:<org_id>:<agent_id>``.  On the first INCR
    of a window we set EXPIRE=60s so the counter rolls over without
    a separate sweep.  Subsequent INCRs hit the same key until the
    TTL fires.

    ``default_rpm`` is the platform-wide ceiling used when the caller
    does not supply a per-request ``rpm`` override.  The
    :func:`rate_limit_dep` extracts the override from
    ``ctx.governance['rate_limit_rpm']`` so admins can pin a noisy
    tenant lower without redeploying.
    """

    def __init__(self, redis: Any, *, default_rpm: int = 300) -> None:
        self._redis = redis
        self._default = default_rpm

    async def try_consume(
        self,
        org_id: str,
        agent_id: str,
        *,
        rpm: int | None = None,
    ) -> bool:
        """Increment the tenant's window counter; return True if under cap.

        ``rpm`` of ``0`` (or any non-positive value) is an admin
        kill-switch: every call is rejected without touching the
        counter at all.  Negative values fall into the same branch
        as a defensive measure — a misconfigured governance.rpm of
        ``-1`` should not silently behave like "unlimited".
        """
        cap = rpm if rpm is not None else self._default
        if cap <= 0:
            return False
        key = f"surogates:rate:{org_id}:{agent_id}"
        count = await self._redis.incr(key)
        if count == 1:
            await self._redis.expire(key, _WINDOW_SECONDS)
        return count <= cap


async def rate_limit_dep(
    request: Request,
    ctx: AgentRuntimeContext = Depends(agent_runtime_context_dep),
) -> None:
    """Enforce the per-tenant rate limit; raises 429 when over.

    Reads the limiter off ``request.app.state.rate_limiter``.  If
    unset (helm-mode pods, or tests that have not wired one), the
    dep silently passes through — this is intentional so the
    surogates harness stays usable without a Redis-backed limiter.

    The per-tenant override is pulled from
    ``ctx.governance['rate_limit_rpm']`` when present, falling back
    to the limiter's ``default_rpm`` otherwise.  Plan 6 will surface
    this field directly on the runtime-config payload; today it just
    lives in the free-form governance dict.
    """
    limiter = getattr(request.app.state, "rate_limiter", None)
    if limiter is None:
        return
    rpm: int | None = None
    if ctx.governance:
        raw = ctx.governance.get("rate_limit_rpm")
        if isinstance(raw, int):
            rpm = raw
    allowed = await limiter.try_consume(
        ctx.org_id, ctx.agent_id, rpm=rpm,
    )
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="per-tenant rate limit exceeded",
        )
