"""The dispatcher releases a turn's slots once, whatever state the turn ended in."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from uuid import UUID, uuid4

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
    session_id = str(uuid4())
    return DequeuedSession(
        org_id="org", agent_id="agent", session_id=session_id, priority=0.0,
        gate_holder=f"{session_id}:turn",
    )


async def test_a_finished_turn_gives_both_slots_back(monkeypatch):
    gate = CountingGate(held=1)
    orchestrator = orchestrator_with(gate)
    await orchestrator.semaphore.acquire()  # as the dispatch loop does
    seen = []

    async def process(session_id, *args, **kwargs):
        seen.append(current_turn.get())

    monkeypatch.setattr(orchestrator, "_process", process)
    turn = dequeued()
    await orchestrator._guarded_process(uuid4(), dequeued=turn)
    assert seen and seen[0] is not None
    assert not orchestrator.semaphore.locked() and gate.held == 0
    assert gate.calls.count("release") == 1
    assert gate.holders == [turn.gate_holder], "the turn gave back its slot under another holder"
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


async def test_a_tenant_slot_that_cannot_be_given_back_is_logged_with_its_session(monkeypatch, caplog):
    gate = CountingGate(held=1, fail_release=1)
    orchestrator = orchestrator_with(gate)
    await orchestrator.semaphore.acquire()

    async def process(session_id, *args, **kwargs):
        return None

    monkeypatch.setattr(orchestrator, "_process", process)
    session_id = uuid4()
    await orchestrator._guarded_process(session_id, dequeued=dequeued())
    leaks = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(leaks) == 1 and str(session_id) in leaks[0].getMessage()


async def _start_turn(orchestrator, monkeypatch, process) -> tuple[asyncio.Task, DequeuedSession]:
    """A turn started as the dispatch loop starts one."""
    monkeypatch.setattr(orchestrator, "_process", process)
    turn = dequeued()
    task = asyncio.create_task(orchestrator._guarded_process(UUID(turn.session_id), dequeued=turn))
    orchestrator._tasks.add(task)
    task.add_done_callback(orchestrator._task_done)
    return task, turn


def _recording_enqueue(monkeypatch) -> list[UUID]:
    enqueued: list[UUID] = []

    async def enqueue(redis, *, org_id, agent_id, session_id):
        enqueued.append(session_id)

    monkeypatch.setattr("surogates.orchestrator.dispatcher.enqueue_session", enqueue)
    return enqueued


async def test_shutdown_hands_a_turn_waiting_on_a_computer_to_another_worker(monkeypatch):
    gate = CountingGate(held=1)
    orchestrator = orchestrator_with(gate)
    await orchestrator.semaphore.acquire()
    waiting = asyncio.Event()
    enqueued = _recording_enqueue(monkeypatch)

    async def process(session_id, *args, **kwargs):
        # The loop, waiting for a tool call that waits on the user's computer.
        async with turn_joining():
            async with turn_activity():
                async with turn_waiting(resumable=True):
                    waiting.set()
                    await asyncio.Event().wait()

    task, turn = await _start_turn(orchestrator, monkeypatch, process)
    await asyncio.wait_for(waiting.wait(), 5.0)
    await asyncio.wait_for(orchestrator._drain_turns(), 5.0)
    assert task.cancelled()
    assert enqueued == [UUID(turn.session_id)]
    assert gate.calls.count("release") == 1
    await orchestrator.semaphore.acquire()
    assert orchestrator.semaphore.locked(), "the semaphore slot was released twice"


async def test_shutdown_does_not_hand_over_a_turn_the_loss_of_its_lease_already_detached(monkeypatch):
    orchestrator = orchestrator_with(CountingGate(held=1))
    await orchestrator.semaphore.acquire()
    waiting = asyncio.Event()
    enqueued = _recording_enqueue(monkeypatch)

    async def process(session_id, *args, **kwargs):
        async with turn_joining():
            async with turn_activity():
                async with turn_waiting(resumable=True):
                    waiting.set()
                    await asyncio.Event().wait()

    task, _turn = await _start_turn(orchestrator, monkeypatch, process)
    await asyncio.wait_for(waiting.wait(), 5.0)
    slots, _dequeued = orchestrator._turns[task]
    slots.detach()  # as the loss of its lease does: the worker that took the session resumes it
    await asyncio.wait_for(orchestrator._drain_turns(), 5.0)
    assert task.cancelled()
    assert enqueued == []


async def test_shutdown_lets_a_working_turn_finish(monkeypatch):
    orchestrator = orchestrator_with(CountingGate(held=1))
    await orchestrator.semaphore.acquire()
    enqueued = _recording_enqueue(monkeypatch)

    async def process(session_id, *args, **kwargs):
        await asyncio.sleep(0.2)

    task, _turn = await _start_turn(orchestrator, monkeypatch, process)
    await asyncio.wait_for(orchestrator._drain_turns(), 5.0)
    assert task.done() and not task.cancelled()
    assert enqueued == []


async def test_shutdown_lets_a_turn_waiting_on_a_person_finish(monkeypatch):
    orchestrator = orchestrator_with(CountingGate(held=1))
    await orchestrator.semaphore.acquire()
    enqueued = _recording_enqueue(monkeypatch)

    async def process(session_id, *args, **kwargs):
        # The loop, waiting for an ask_user_question that is answered soon.
        async with turn_joining():
            async with turn_activity():
                async with turn_waiting():
                    await asyncio.sleep(1.5)

    task, _turn = await _start_turn(orchestrator, monkeypatch, process)
    await asyncio.wait_for(orchestrator._drain_turns(), 5.0)
    assert task.done() and not task.cancelled()
    assert enqueued == []


async def test_an_interrupt_signal_reaches_the_turns_slots(monkeypatch):
    orchestrator = orchestrator_with(CountingGate(held=1))
    await orchestrator.semaphore.acquire()
    started = asyncio.Event()
    interrupted: list[bool] = []

    async def process(session_id, *args, **kwargs):
        slots = current_turn.get()
        slots.interrupt = lambda: interrupted.append(True)
        started.set()
        await asyncio.sleep(0.2)

    task, turn = await _start_turn(orchestrator, monkeypatch, process)
    await asyncio.wait_for(started.wait(), 5.0)
    # The interrupt reaches the turn only through its harness.
    orchestrator._active_harnesses[UUID(turn.session_id)] = SimpleNamespace(interrupt=lambda message: None)
    await orchestrator._handle_interrupt_signal(UUID(turn.session_id), "paused by user")
    await asyncio.gather(task, return_exceptions=True)
    assert interrupted == [True]
