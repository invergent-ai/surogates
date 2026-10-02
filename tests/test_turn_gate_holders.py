"""The turn gate counts the turns holding a tenant's slots, so recovery frees only a slot really held."""

from __future__ import annotations

import fnmatch

import pytest

from surogates.runtime.turn_gate import TurnConcurrencyGate

pytestmark = pytest.mark.asyncio


class FakeRedis:
    """The calls the gate makes: SADD and SCARD in a transaction pipeline, SREM, SSCAN."""

    def __init__(self) -> None:
        self.sets: dict[str, set[str]] = {}

    def pipeline(self, transaction: bool = True) -> "FakePipeline":
        return FakePipeline(self)

    async def srem(self, key: str, *members: str) -> int:
        held = self.sets.setdefault(key, set())
        removed = [m for m in members if m in held]
        held.difference_update(removed)
        return len(removed)

    async def sscan(self, key: str, cursor: int = 0, match: str | None = None, count: int | None = None):
        members = [m for m in self.sets.get(key, set()) if match is None or fnmatch.fnmatchcase(m, match)]
        return 0, members


class FakePipeline:
    def __init__(self, redis: FakeRedis) -> None:
        self._redis = redis
        self._ops: list[tuple[str, str, str]] = []

    async def __aenter__(self) -> "FakePipeline":
        return self

    async def __aexit__(self, *exc) -> None:
        return None

    def sadd(self, key: str, member: str) -> "FakePipeline":
        self._ops.append(("sadd", key, member))
        return self

    def scard(self, key: str) -> "FakePipeline":
        self._ops.append(("scard", key, ""))
        return self

    async def execute(self) -> list[int]:
        results = []
        for op, key, member in self._ops:
            held = self._redis.sets.setdefault(key, set())
            if op == "sadd":
                results.append(0 if member in held else 1)
                held.add(member)
            else:
                results.append(len(held))
        return results


async def test_a_holder_takes_one_slot_however_often_it_asks():
    gate = TurnConcurrencyGate(FakeRedis(), default_max=2)
    assert await gate.try_acquire("org", "agent", holder="s1:a")
    assert await gate.try_acquire("org", "agent", holder="s1:a")
    assert await gate.try_acquire("org", "agent", holder="s2:a")
    assert not await gate.try_acquire("org", "agent", holder="s3:a")


async def test_a_second_turn_of_a_session_frees_only_its_own_slot():
    gate = TurnConcurrencyGate(FakeRedis(), default_max=2)
    assert await gate.try_acquire("org", "agent", holder="s1:first")
    assert await gate.try_acquire("org", "agent", holder="s1:second")  # re-enqueued mid-turn
    assert await gate.release("org", "agent", holder="s1:second")      # it ends at once
    assert await gate.try_acquire("org", "agent", holder="s2:a")
    assert not await gate.try_acquire("org", "agent", holder="s3:a"), "s1's running turn still holds its slot"


async def test_recovery_frees_only_what_a_dead_sessions_turns_held():
    gate = TurnConcurrencyGate(FakeRedis(), default_max=1)
    assert await gate.try_acquire("org", "agent", holder="dead:a")
    assert await gate.release("org", "agent", holder="dead:a")  # given back while it waited
    assert await gate.try_acquire("org", "agent", holder="live:a")
    assert await gate.release_session("org", "agent", "dead") == 0
    assert not await gate.try_acquire("org", "agent", holder="another:a")


async def test_each_dequeue_takes_its_slot_under_its_own_holder():
    from surogates.config import encode_queue_member
    from surogates.orchestrator.dispatcher import dequeue_next_session

    class QueueRedis(FakeRedis):
        def __init__(self, members: list[str]) -> None:
            super().__init__()
            self.members = members

        async def bzpopmin(self, key: str, timeout: float = 0):
            return (key, self.members.pop(0), 0.0) if self.members else None

    member = encode_queue_member(org_id="org", agent_id="agent", session_id="s1")
    redis = QueueRedis([member, member])  # the session was enqueued again mid-turn
    gate = TurnConcurrencyGate(redis, default_max=2)

    first = await dequeue_next_session(redis, gate=gate)
    second = await dequeue_next_session(redis, gate=gate)

    assert first.gate_holder.startswith("s1:") and second.gate_holder.startswith("s1:")
    assert first.gate_holder != second.gate_holder
    assert redis.sets["surogates:turn_holders:org:agent"] == {first.gate_holder, second.gate_holder}


async def test_a_dequeue_without_a_gate_has_no_holder():
    from surogates.config import encode_queue_member
    from surogates.orchestrator.dispatcher import dequeue_next_session

    class QueueRedis(FakeRedis):
        async def bzpopmin(self, key: str, timeout: float = 0):
            return key, encode_queue_member(org_id="org", agent_id="agent", session_id="s1"), 0.0

    dequeued = await dequeue_next_session(QueueRedis())

    assert dequeued.gate_holder == ""
