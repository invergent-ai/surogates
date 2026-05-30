"""Tests for the dispatcher's per-tenant dequeue gate.

Plan 2 / Task 13.  When the dispatcher pops a session off the shared
queue, it acquires the tenant's TurnConcurrencyGate slot before
handing the session to a worker.  Over-the-cap sessions are
requeued with backoff so a noisy tenant cannot drain the pool.

Tests use a tiny fake of the BZPOPMIN contract; the real Redis
integration is covered by tests/integration/.
"""

from __future__ import annotations

from collections import defaultdict, deque

import pytest


class _FakeRedis:
    """Supports the BZPOPMIN + ZADD + INCR/DECR subset used here."""

    def __init__(self) -> None:
        self._zsets: dict[str, deque[tuple[str, float]]] = defaultdict(deque)
        self._counters: dict[str, int] = defaultdict(int)

    async def zadd(self, key: str, mapping: dict[str, float]) -> int:
        added = 0
        for member, score in mapping.items():
            self._zsets[key].append((member, score))
            added += 1
        self._zsets[key] = deque(
            sorted(self._zsets[key], key=lambda t: t[1]),
        )
        return added

    async def bzpopmin(self, keys, timeout: float = 0.0):
        key = keys if isinstance(keys, str) else keys[0]
        if not self._zsets[key]:
            return None
        member, score = self._zsets[key].popleft()
        return (key, member, score)

    async def incr(self, key: str) -> int:
        self._counters[key] += 1
        return self._counters[key]

    async def decr(self, key: str) -> int:
        self._counters[key] = max(0, self._counters[key] - 1)
        return self._counters[key]


@pytest.mark.asyncio
async def test_dispatcher_dequeue_pops_oldest_priority_first():
    from surogates.config import enqueue_session
    from surogates.orchestrator.dispatcher import dequeue_next_session

    r = _FakeRedis()
    await enqueue_session(
        r, org_id="o-1", agent_id="a-1", session_id="s-A", priority=5,
    )
    await enqueue_session(
        r, org_id="o-1", agent_id="a-2", session_id="s-B", priority=1,
    )
    result = await dequeue_next_session(r)
    assert result is not None
    assert result.session_id == "s-B"  # lower priority value wins


@pytest.mark.asyncio
async def test_dispatcher_acquires_gate_on_dequeue():
    """Over-the-cap tenants get their session requeued; the next pop
    sees the unblocked tenants."""
    from surogates.config import enqueue_session
    from surogates.orchestrator.dispatcher import dequeue_next_session
    from surogates.runtime import TurnConcurrencyGate

    r = _FakeRedis()
    gate = TurnConcurrencyGate(r, default_max=1)
    # Hot tenant already has 1 in-flight turn → at cap.
    await gate.try_acquire("hot", "a-1", limit=1)
    await enqueue_session(
        r, org_id="hot", agent_id="a-1", session_id="s-A", priority=0,
    )
    await enqueue_session(
        r, org_id="cold", agent_id="a-2", session_id="s-B", priority=1,
    )

    delivered = await dequeue_next_session(r, gate=gate, gate_limit=1)
    # The hot tenant's session was requeued; the cold tenant's
    # session got delivered.
    assert delivered is not None
    assert delivered.session_id == "s-B"
    assert delivered.org_id == "cold"


@pytest.mark.asyncio
async def test_dispatcher_requeues_with_backoff_on_gate_busy():
    """A requeued session must come back later — not be lost — and
    its priority must reflect the backoff so a hot tenant doesn't
    starve everyone else."""
    from surogates.config import (
        SHARED_WORK_QUEUE_KEY, enqueue_session,
    )
    from surogates.orchestrator.dispatcher import (
        _GATE_REQUEUE_BACKOFF, dequeue_next_session,
    )
    from surogates.runtime import TurnConcurrencyGate

    r = _FakeRedis()
    gate = TurnConcurrencyGate(r, default_max=1)
    await gate.try_acquire("hot", "a-1", limit=1)
    await enqueue_session(
        r, org_id="hot", agent_id="a-1", session_id="s-A", priority=10,
    )

    delivered = await dequeue_next_session(r, gate=gate, gate_limit=1)
    assert delivered is None
    remaining = list(r._zsets[SHARED_WORK_QUEUE_KEY])
    assert len(remaining) == 1
    member, score = remaining[0]
    assert member == "hot|a-1|s-A"
    assert score == 10 + _GATE_REQUEUE_BACKOFF
