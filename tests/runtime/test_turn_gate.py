"""Tests for TurnConcurrencyGate.

Per-tenant counter limiting how many sessions can
be in-flight simultaneously for a given (org_id, agent_id).  The
dispatcher dequeue path consults the gate before handing a session
to a worker; over-limit sessions are requeued with backoff.

The gate is a *concurrency* limit (live counter) — distinct from the
PerTenantRateLimiter which is a request-rate
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


# ---------------------------------------------------------------------------
# released_for_wait — the inverse of acquire(), for callers that are about to
# block on something external and should not hold a slot while they sleep.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_released_for_wait_frees_the_slot_inside_the_body():
    from surogates.runtime.turn_gate import TurnConcurrencyGate, released_for_wait

    redis = _FakeRedis()
    gate = TurnConcurrencyGate(redis, default_max=1)
    assert await gate.try_acquire("o-1", "a-1") is True

    async with released_for_wait(gate, "o-1", "a-1", context="test"):
        # The slot is free while the caller sleeps — that is the whole point.
        assert await gate.try_acquire("o-1", "a-1") is True
        await gate.release("o-1", "a-1")

    assert redis._store["surogates:turns:o-1:a-1"] == 1


@pytest.mark.asyncio
async def test_released_for_wait_is_a_noop_without_a_gate():
    from surogates.runtime.turn_gate import released_for_wait

    async with released_for_wait(None, "o-1", "a-1", context="test"):
        pass  # must not raise


@pytest.mark.asyncio
async def test_released_for_wait_retakes_the_slot_when_the_body_raises():
    """A panicking body must not leak the slot it gave up."""
    from surogates.runtime.turn_gate import TurnConcurrencyGate, released_for_wait

    redis = _FakeRedis()
    gate = TurnConcurrencyGate(redis, default_max=5)
    await gate.try_acquire("o-1", "a-1")

    with pytest.raises(RuntimeError):
        async with released_for_wait(gate, "o-1", "a-1", context="test"):
            raise RuntimeError("boom")

    assert redis._store["surogates:turns:o-1:a-1"] == 1


@pytest.mark.asyncio
async def test_released_for_wait_skips_reacquire_when_release_failed():
    """Re-taking a slot we never gave up would hand the tenant a free slot."""
    from surogates.runtime.turn_gate import released_for_wait

    class _FlakyRelease:
        def __init__(self) -> None:
            self.acquires = 0

        async def release(self, org_id: str, agent_id: str) -> None:
            raise RuntimeError("redis blip")

        async def try_acquire(self, org_id: str, agent_id: str) -> bool:
            self.acquires += 1
            return True

    gate = _FlakyRelease()
    async with released_for_wait(gate, "o-1", "a-1", context="test"):
        pass

    assert gate.acquires == 0


@pytest.mark.asyncio
async def test_reacquire_returns_true_when_slot_immediately_free():
    from surogates.runtime.turn_gate import (
        TurnConcurrencyGate,
        _reacquire_with_backoff,
    )

    redis = _FakeRedis()
    gate = TurnConcurrencyGate(redis, default_max=10)
    assert await _reacquire_with_backoff(gate, "org-1", "agent-A") is True
    assert redis._store["surogates:turns:org-1:agent-A"] == 1


@pytest.mark.asyncio
async def test_reacquire_returns_false_after_deadline_at_cap(
    monkeypatch: pytest.MonkeyPatch,
):
    """Saturated for the whole window: give up rather than block forever.

    The caller's return path matters more than perfect accounting.
    """
    import surogates.runtime.turn_gate as turn_gate_module
    from surogates.runtime.turn_gate import (
        TurnConcurrencyGate,
        _reacquire_with_backoff,
    )

    monkeypatch.setattr(turn_gate_module, "_REACQUIRE_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(turn_gate_module, "_REACQUIRE_BACKOFF_SECONDS", 0.01)

    redis = _FakeRedis()
    gate = TurnConcurrencyGate(redis, default_max=2)
    redis._store["surogates:turns:org-1:agent-A"] = 2

    assert await _reacquire_with_backoff(gate, "org-1", "agent-A") is False
    # Counter unchanged -- the helper aborted without acquiring.
    assert redis._store["surogates:turns:org-1:agent-A"] == 2
