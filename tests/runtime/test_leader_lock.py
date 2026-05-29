"""Tests for RedisLeaderLock.

Plan 8 / Task 1.  SET NX EX primitive that the platform ticker
(Task 5) uses to ensure only one replica fires at a time across
N replicas.  Loss-of-lock detection (Task 2's heartbeat returning
False) means the holder must stop dispatching mid-tick to avoid
double-fire.
"""

from __future__ import annotations

import pytest


class _FakeRedis:
    """In-memory stand-in for ``redis.asyncio.Redis`` covering
    the SET NX EX / SET XX EX / GET / DELETE surface the lock
    uses."""

    def __init__(self) -> None:
        self._values: dict[str, tuple[bytes, float | None]] = {}
        self.set_calls: list[dict] = []

    async def set(
        self, key: str, value: bytes, *,
        nx: bool = False, xx: bool = False, ex: int | None = None,
    ) -> bool:
        self.set_calls.append({
            "key": key, "value": value, "nx": nx, "xx": xx, "ex": ex,
        })
        existing = self._values.get(key)
        if nx and existing is not None:
            return False
        if xx and existing is None:
            return False
        self._values[key] = (value, ex)
        return True

    async def get(self, key: str) -> bytes | None:
        v = self._values.get(key)
        return v[0] if v else None

    async def delete(self, key: str) -> int:
        return 1 if self._values.pop(key, None) else 0


@pytest.mark.asyncio
async def test_acquire_returns_true_on_first_holder():
    from surogates.runtime.leader_lock import RedisLeaderLock

    redis = _FakeRedis()
    lock = RedisLeaderLock(
        redis, key="surogates:scheduled_ticker:leader",
        ttl_seconds=10, holder_id="replica-a",
    )
    assert await lock.acquire() is True
    assert redis.set_calls[0]["nx"] is True
    assert redis.set_calls[0]["ex"] == 10
    assert redis.set_calls[0]["value"] == b"replica-a"


@pytest.mark.asyncio
async def test_acquire_returns_false_when_lock_held_by_another():
    from surogates.runtime.leader_lock import RedisLeaderLock

    redis = _FakeRedis()
    held = RedisLeaderLock(
        redis, key="k", ttl_seconds=10, holder_id="replica-a",
    )
    contender = RedisLeaderLock(
        redis, key="k", ttl_seconds=10, holder_id="replica-b",
    )
    assert await held.acquire() is True
    assert await contender.acquire() is False


@pytest.mark.asyncio
async def test_release_drops_the_lock():
    from surogates.runtime.leader_lock import RedisLeaderLock

    redis = _FakeRedis()
    lock = RedisLeaderLock(
        redis, key="k", ttl_seconds=10, holder_id="replica-a",
    )
    await lock.acquire()
    await lock.release()
    contender = RedisLeaderLock(
        redis, key="k", ttl_seconds=10, holder_id="replica-b",
    )
    assert await contender.acquire() is True


@pytest.mark.asyncio
async def test_release_only_drops_if_we_hold_it():
    """Risk #13 mitigation: a stale release call (e.g. after a
    delayed shutdown that already lost the lock to a new leader)
    MUST NOT delete the new leader's lock.  The release checks
    GET first and only DELETEs when the value matches our
    holder_id."""
    from surogates.runtime.leader_lock import RedisLeaderLock

    redis = _FakeRedis()
    a = RedisLeaderLock(
        redis, key="k", ttl_seconds=10, holder_id="replica-a",
    )
    await a.acquire()

    # Simulate replica-b stealing the lock (e.g. after replica-a's
    # TTL expired without us noticing).
    redis._values["k"] = (b"replica-b", 10)

    await a.release()
    # b's lock is still there.
    assert await redis.get("k") == b"replica-b"
