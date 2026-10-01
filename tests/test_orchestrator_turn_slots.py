"""The dispatcher releases a turn's slots once, whatever state the turn ended in."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from surogates.orchestrator.dispatcher import DequeuedSession, Orchestrator
from surogates.runtime.turn_slots import current_turn, turn_activity, turn_joining, turn_waiting
from tests.test_turn_slots import CountingGate

pytestmark = pytest.mark.asyncio


def orchestrator_with(gate: CountingGate) -> Orchestrator:
    return Orchestrator(
        redis_client=object(),
        session_store=object(),
        harness_factory=lambda _sid: None,
        agent_id="agent",
        queue_key="surogates:work_queue:agent",
        max_concurrent=1,
        turn_gate=gate,
    )


def dequeued() -> DequeuedSession:
    return DequeuedSession(org_id="org", agent_id="agent", session_id=str(uuid4()), priority=0.0)


async def test_a_finished_turn_gives_both_slots_back(monkeypatch):
    gate = CountingGate(held=1)
    orchestrator = orchestrator_with(gate)
    await orchestrator.semaphore.acquire()  # as the dispatch loop does
    seen = []

    async def process(session_id, *args, **kwargs):
        seen.append(current_turn.get())

    monkeypatch.setattr(orchestrator, "_process", process)
    await orchestrator._guarded_process(uuid4(), dequeued=dequeued())
    assert seen and seen[0] is not None
    assert not orchestrator.semaphore.locked() and gate.held == 0
    assert gate.calls.count("release") == 1
    assert current_turn.get() is None


async def test_a_turn_cancelled_while_it_waits_releases_each_slot_once(monkeypatch):
    gate = CountingGate(held=1)
    orchestrator = orchestrator_with(gate)
    await orchestrator.semaphore.acquire()
    waiting = asyncio.Event()

    async def process(session_id, *args, **kwargs):
        # The loop, waiting for a tool call that waits on the user's computer.
        async with turn_joining():
            async with turn_activity():
                async with turn_waiting():
                    waiting.set()
                    await asyncio.Event().wait()

    monkeypatch.setattr(orchestrator, "_process", process)
    task = asyncio.create_task(orchestrator._guarded_process(uuid4(), dequeued=dequeued()))
    await waiting.wait()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert gate.calls.count("release") == 1, "the dispatcher released the tenant slot twice"
    await orchestrator.semaphore.acquire()
    assert orchestrator.semaphore.locked(), "the dispatcher released the semaphore slot twice"


async def test_a_turn_without_a_dequeued_slot_releases_no_gate_slot(monkeypatch):
    gate = CountingGate(held=0)
    orchestrator = orchestrator_with(gate)
    await orchestrator.semaphore.acquire()

    async def process(session_id, *args, **kwargs):
        return None

    monkeypatch.setattr(orchestrator, "_process", process)
    await orchestrator._guarded_process(uuid4())
    assert gate.calls == []
    assert not orchestrator.semaphore.locked()
