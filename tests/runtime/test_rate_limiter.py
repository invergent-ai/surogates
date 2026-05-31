"""Tests for PerTenantRateLimiter and rate_limit_dep.

Fixed-window counter keyed on
``(org_id, agent_id)`` backed by Redis.  Per-tenant isolation is the
core guarantee: one tenant exhausting its budget must not affect
another's.  The dep gates user-input routes with HTTP 429 when the
window is full.

The Redis stub is intentionally minimal — INCR + EXPIRE are the only
commands the limiter uses, and the tests reset state per case so
fake-TTL is sufficient.  Wiring this against a real Redis is left to
the integration suite (which the surogates test fixtures already
provision via testcontainers).
"""

from __future__ import annotations

from collections import defaultdict

import pytest


class _FakeRedis:
    """Minimal redis.asyncio-style stub supporting INCR + EXPIRE."""

    def __init__(self) -> None:
        self._store: dict[str, int] = defaultdict(int)
        self._ttls: dict[str, int] = {}

    async def incr(self, key: str) -> int:
        self._store[key] += 1
        return self._store[key]

    async def expire(self, key: str, seconds: int) -> bool:
        self._ttls[key] = seconds
        return True

    async def get(self, key: str) -> int | None:
        return self._store.get(key)


@pytest.mark.asyncio
async def test_rate_limiter_allows_under_limit():
    from surogates.runtime import PerTenantRateLimiter

    limiter = PerTenantRateLimiter(_FakeRedis(), default_rpm=10)
    for _ in range(5):
        ok = await limiter.try_consume("o-1", "a-1", rpm=10)
        assert ok is True


@pytest.mark.asyncio
async def test_rate_limiter_rejects_over_limit():
    from surogates.runtime import PerTenantRateLimiter

    limiter = PerTenantRateLimiter(_FakeRedis(), default_rpm=3)
    for _ in range(3):
        assert await limiter.try_consume("o-1", "a-1", rpm=3) is True
    assert await limiter.try_consume("o-1", "a-1", rpm=3) is False


@pytest.mark.asyncio
async def test_rate_limiter_isolates_tenants():
    """One tenant exhausting its budget does not block another."""
    from surogates.runtime import PerTenantRateLimiter

    limiter = PerTenantRateLimiter(_FakeRedis(), default_rpm=2)
    for _ in range(2):
        assert await limiter.try_consume("o-1", "a-1", rpm=2) is True
    assert await limiter.try_consume("o-1", "a-1", rpm=2) is False
    assert await limiter.try_consume("o-1", "a-2", rpm=2) is True
    assert await limiter.try_consume("o-2", "a-1", rpm=2) is True


@pytest.mark.asyncio
async def test_rate_limiter_isolates_orgs_with_same_agent_id():
    """The key includes org_id so two orgs that happen to share an
    agent_id collision"""
    from surogates.runtime import PerTenantRateLimiter

    limiter = PerTenantRateLimiter(_FakeRedis(), default_rpm=1)
    assert await limiter.try_consume("o-A", "a-shared", rpm=1) is True
    assert await limiter.try_consume("o-A", "a-shared", rpm=1) is False
    assert await limiter.try_consume("o-B", "a-shared", rpm=1) is True


@pytest.mark.asyncio
async def test_rate_limiter_rpm_zero_blocks_everything():
    """rpm=0 is an admin kill-switch for the tenant — every request
    must be rejected, even the very first one in a window."""
    from surogates.runtime import PerTenantRateLimiter

    limiter = PerTenantRateLimiter(_FakeRedis(), default_rpm=100)
    assert await limiter.try_consume("o-1", "a-1", rpm=0) is False


@pytest.mark.asyncio
async def test_rate_limiter_first_request_sets_expiry():
    """The fixed window only resets if EXPIRE was called on the
    first INCR — otherwise the counter would persist forever and
    the tenant would be locked out the moment they hit their cap."""
    from surogates.runtime import PerTenantRateLimiter

    redis = _FakeRedis()
    limiter = PerTenantRateLimiter(redis, default_rpm=5)
    await limiter.try_consume("o-1", "a-1", rpm=5)
    key = "surogates:rate:o-1:a-1"
    assert key in redis._ttls
    assert redis._ttls[key] == 60


@pytest.mark.asyncio
async def test_rate_limit_dep_raises_429_when_over_limit():
    """rate_limit_dep enforces the tenant cap and
    raises HTTP 429 once the window is full."""
    from fastapi import Depends, FastAPI
    from fastapi.testclient import TestClient

    from surogates.runtime import (
        AgentRuntimeContext,
        PerTenantRateLimiter,
        rate_limit_dep,
    )

    limiter = PerTenantRateLimiter(_FakeRedis(), default_rpm=2)

    app = FastAPI()
    app.state.rate_limiter = limiter

    def _ctx() -> AgentRuntimeContext:
        return AgentRuntimeContext(
            agent_id="a-1",
            org_id="o-1",
            project_id="p-1",
            enabled=True,
            config_version=1,
            storage_key_prefix="p/a",
        )

    # rate_limit_dep depends on agent_runtime_context_dep at import
    # time; override that dep with our fixed context.
    from surogates.runtime.resolver import agent_runtime_context_dep
    app.dependency_overrides[agent_runtime_context_dep] = _ctx

    @app.get("/lim")
    async def lim(_: None = Depends(rate_limit_dep)):
        return {"ok": True}

    with TestClient(app) as c:
        assert c.get("/lim").status_code == 200
        assert c.get("/lim").status_code == 200
        r = c.get("/lim")
    assert r.status_code == 429
    assert "rate limit" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_rate_limit_dep_uses_governance_rate_limit_rpm():
    """The per-tenant cap can be overridden by the runtime config
    ``governance.rate_limit_rpm`` field so admins can pin a noisy
    tenant lower without redeploying."""
    from fastapi import Depends, FastAPI
    from fastapi.testclient import TestClient

    from surogates.runtime import (
        AgentRuntimeContext,
        PerTenantRateLimiter,
        rate_limit_dep,
    )
    from surogates.runtime.resolver import agent_runtime_context_dep

    limiter = PerTenantRateLimiter(_FakeRedis(), default_rpm=100)

    def _ctx() -> AgentRuntimeContext:
        return AgentRuntimeContext(
            agent_id="a-1",
            org_id="o-1",
            project_id="p-1",
            enabled=True,
            config_version=1,
            storage_key_prefix="p/a",
            governance={"rate_limit_rpm": 1},
        )

    app = FastAPI()
    app.state.rate_limiter = limiter
    app.dependency_overrides[agent_runtime_context_dep] = _ctx

    @app.get("/lim")
    async def lim(_: None = Depends(rate_limit_dep)):
        return {"ok": True}

    with TestClient(app) as c:
        assert c.get("/lim").status_code == 200
        assert c.get("/lim").status_code == 429
