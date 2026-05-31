"""PerTenantRateLimiter gates inbound channel
events per (org_id, agent_id) so a noisy tenant cannot starve
other tenants of adapter capacity."""

from __future__ import annotations

import pytest


class _FakeLimiter:
    def __init__(self, allowed_first_n: int) -> None:
        self._budget = allowed_first_n
        self.calls = 0

    async def try_consume(self, *, org_id, agent_id, rpm=None):
        self.calls += 1
        if self._budget <= 0:
            return False
        self._budget -= 1
        return True


@pytest.mark.asyncio
async def test_inbound_gate_drops_when_over_cap():
    from surogates.channels.rate_gate import check_inbound_rate_limit

    limiter = _FakeLimiter(allowed_first_n=2)
    routing = {"org_id": "o-1", "agent_id": "a-1"}

    assert await check_inbound_rate_limit(limiter, routing) is True
    assert await check_inbound_rate_limit(limiter, routing) is True
    assert await check_inbound_rate_limit(limiter, routing) is False
    assert limiter.calls == 3


@pytest.mark.asyncio
async def test_inbound_gate_no_limiter_wired_passes_through():
    """Helm-mode pods don't wire a PerTenantRateLimiter; the gate
    must pass-through (return True) rather than raising so the
    legacy path keeps working."""
    from surogates.channels.rate_gate import check_inbound_rate_limit

    assert await check_inbound_rate_limit(
        None, {"org_id": "o-1", "agent_id": "a-1"},
    ) is True


@pytest.mark.asyncio
async def test_inbound_gate_isolates_tenants():
    """Tenant A's budget exhaustion must not affect tenant B's
    rate-limit decisions -- the limiter keys on (org_id,
    agent_id) so the two tenants have independent buckets."""

    class _PerTenantLimiter:
        def __init__(self):
            self._budgets = {("o-1", "a-1"): 0, ("o-2", "a-2"): 10}

        async def try_consume(self, *, org_id, agent_id, rpm=None):
            key = (org_id, agent_id)
            if self._budgets.get(key, 0) <= 0:
                return False
            self._budgets[key] -= 1
            return True

    from surogates.channels.rate_gate import check_inbound_rate_limit

    limiter = _PerTenantLimiter()
    assert await check_inbound_rate_limit(
        limiter, {"org_id": "o-1", "agent_id": "a-1"},
    ) is False
    assert await check_inbound_rate_limit(
        limiter, {"org_id": "o-2", "agent_id": "a-2"},
    ) is True
