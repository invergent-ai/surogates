"""Tests for TurnConcurrencyGate.

Plan 2 / Task 11.  Per-tenant counter limiting how many sessions can
be in-flight simultaneously for a given (org_id, agent_id).  The
dispatcher dequeue path consults the gate before handing a session
to a worker; over-limit sessions are requeued with backoff.

The gate is a *concurrency* limit (live counter) — distinct from the
PerTenantRateLimiter (Plan 1b Task 13) which is a request-rate
limit (per-minute window).
"""

from __future__ import annotations

from collections import defaultdict

import pytest


class _FakeRedis:
    def __init__(self) -> None:
        self._store: dict[str, int] = defaultdict(int)

    async def incr(self, key: str) -> int:
        self._store[key] += 1
        return self._store[key]

    async def decr(self, key: str) -> int:
        self._store[key] = max(0, self._store[key] - 1)
        return self._store[key]

    async def get(self, key: str) -> str | None:
        v = self._store.get(key, 0)
        return str(v) if v else None


@pytest.mark.asyncio
async def test_turn_gate_acquire_under_limit():
    from surogates.runtime import TurnConcurrencyGate

    gate = TurnConcurrencyGate(_FakeRedis(), default_max=5)
    acquired = await gate.try_acquire("o-1", "a-1", limit=5)
    assert acquired is True


@pytest.mark.asyncio
async def test_turn_gate_rejects_over_limit():
    from surogates.runtime import TurnConcurrencyGate

    gate = TurnConcurrencyGate(_FakeRedis(), default_max=2)
    assert await gate.try_acquire("o-1", "a-1", limit=2) is True
    assert await gate.try_acquire("o-1", "a-1", limit=2) is True
    assert await gate.try_acquire("o-1", "a-1", limit=2) is False


@pytest.mark.asyncio
async def test_turn_gate_release_decrements_counter():
    from surogates.runtime import TurnConcurrencyGate

    gate = TurnConcurrencyGate(_FakeRedis(), default_max=2)
    await gate.try_acquire("o-1", "a-1", limit=2)
    await gate.try_acquire("o-1", "a-1", limit=2)
    assert await gate.try_acquire("o-1", "a-1", limit=2) is False

    await gate.release("o-1", "a-1")
    assert await gate.try_acquire("o-1", "a-1", limit=2) is True


@pytest.mark.asyncio
async def test_turn_gate_release_floor_zero():
    """A stuck release (more releases than acquires — e.g. crash
    recovery double-releasing) must not drive the counter negative,
    or a future acquire would silently exceed the limit by however
    many spurious releases happened."""
    from surogates.runtime import TurnConcurrencyGate

    gate = TurnConcurrencyGate(_FakeRedis(), default_max=2)
    await gate.release("o-1", "a-1")
    await gate.release("o-1", "a-1")
    assert await gate.try_acquire("o-1", "a-1", limit=2) is True
    assert await gate.try_acquire("o-1", "a-1", limit=2) is True
    assert await gate.try_acquire("o-1", "a-1", limit=2) is False


@pytest.mark.asyncio
async def test_turn_gate_isolates_tenants():
    from surogates.runtime import TurnConcurrencyGate

    gate = TurnConcurrencyGate(_FakeRedis(), default_max=1)
    assert await gate.try_acquire("o-A", "a-1", limit=1) is True
    assert await gate.try_acquire("o-A", "a-1", limit=1) is False
    assert await gate.try_acquire("o-B", "a-1", limit=1) is True
    assert await gate.try_acquire("o-A", "a-2", limit=1) is True


@pytest.mark.asyncio
async def test_turn_gate_acquire_context_manager_releases_on_exit():
    """The async-context-manager form is the dispatcher-facing API;
    it must release even if the body raises so a panicking
    handler doesn't permanently consume a slot."""
    from surogates.runtime import TurnConcurrencyGate

    gate = TurnConcurrencyGate(_FakeRedis(), default_max=1)
    with pytest.raises(RuntimeError):
        async with gate.acquire("o-1", "a-1", limit=1):
            assert (
                await gate.try_acquire("o-1", "a-1", limit=1)
            ) is False
            raise RuntimeError("oops")
    assert await gate.try_acquire("o-1", "a-1", limit=1) is True


@pytest.mark.asyncio
async def test_turn_gate_acquire_context_manager_raises_when_over_limit():
    from surogates.runtime import TurnConcurrencyGate, TurnGateBusy

    gate = TurnConcurrencyGate(_FakeRedis(), default_max=1)
    async with gate.acquire("o-1", "a-1", limit=1):
        with pytest.raises(TurnGateBusy):
            async with gate.acquire("o-1", "a-1", limit=1):
                pass  # pragma: no cover
