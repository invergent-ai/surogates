"""Plan 8 / Task 3 integration test.

Three RedisLeaderLock instances race on the same key.  Exactly
one acquires.  Killing the holder (release) lets the next
contender win within the TTL window.

Uses the same _FakeRedis as Task 1 so the test stays at the
unit tier; the real-Redis equivalent is the operations smoke
test that runs against a kind cluster (out of scope for Plan 8).
"""

from __future__ import annotations

import pytest

from tests.runtime.test_leader_lock import _FakeRedis  # noqa: E402


@pytest.mark.asyncio
async def test_three_replicas_only_one_acquires():
    from surogates.runtime.leader_lock import RedisLeaderLock

    redis = _FakeRedis()
    locks = [
        RedisLeaderLock(
            redis, key="k", ttl_seconds=10, holder_id=f"r-{i}",
        )
        for i in range(3)
    ]
    acquires = [await lock.acquire() for lock in locks]
    assert sum(acquires) == 1
    winner_index = acquires.index(True)
    assert acquires == [
        i == winner_index for i in range(3)
    ]


@pytest.mark.asyncio
async def test_replica_kill_lets_next_contender_win():
    """Plan 8 risk #13 mitigation: killing the holder mid-tick
    must let the next contender win within the TTL window.

    We simulate "kill" as a clean release here; the actual
    kill-9 scenario is covered by TTL expiry, which the unit
    fake doesn't model -- the operations smoke test on a real
    Redis covers that path."""
    from surogates.runtime.leader_lock import RedisLeaderLock

    redis = _FakeRedis()
    a = RedisLeaderLock(redis, key="k", ttl_seconds=10, holder_id="a")
    b = RedisLeaderLock(redis, key="k", ttl_seconds=10, holder_id="b")
    c = RedisLeaderLock(redis, key="k", ttl_seconds=10, holder_id="c")

    assert await a.acquire() is True
    assert await b.acquire() is False
    assert await c.acquire() is False

    await a.release()

    # b acquires first; c stays the loser.
    assert await b.acquire() is True
    assert await c.acquire() is False


@pytest.mark.asyncio
async def test_holder_heartbeat_blocks_contenders():
    """While the holder heartbeats successfully, contenders
    continue to see acquire=False -- no flapping."""
    from surogates.runtime.leader_lock import RedisLeaderLock

    redis = _FakeRedis()
    a = RedisLeaderLock(redis, key="k", ttl_seconds=10, holder_id="a")
    b = RedisLeaderLock(redis, key="k", ttl_seconds=10, holder_id="b")

    await a.acquire()
    for _ in range(5):
        assert await a.heartbeat() is True
        assert await b.acquire() is False


@pytest.mark.asyncio
async def test_stale_release_after_lease_loss_is_safe():
    """Plan 8 risk #13 mitigation, expressed as a test.

    Scenario: replica-a's tick takes longer than the TTL.  The
    lease expires; replica-b acquires.  Replica-a finally
    finishes its tick and calls release().  The release MUST
    NOT delete replica-b's new lock.  Without this guarantee,
    replica-c would then acquire mid-tick of replica-b and
    duplicate-fire."""
    from surogates.runtime.leader_lock import RedisLeaderLock

    redis = _FakeRedis()
    a = RedisLeaderLock(redis, key="k", ttl_seconds=10, holder_id="a")
    b = RedisLeaderLock(redis, key="k", ttl_seconds=10, holder_id="b")

    await a.acquire()
    # Simulate the TTL elapsing (the fake doesn't enforce
    # expiry; we just drop the key).
    redis._values.pop("k")
    # b acquires.
    assert await b.acquire() is True
    # a's stale release fires; must be a no-op against b's lock.
    await a.release()
    assert await redis.get("k") == b"b"
