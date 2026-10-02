"""A turn gives its worker slots back while all of it waits, and takes them back before it works."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator

import pytest

from surogates.runtime import turn_slots as turn_slots_module
from surogates.runtime.turn_slots import (
    TurnSlots,
    current_turn,
    turn_activity,
    turn_joining,
    turn_waiting,
)

pytestmark = pytest.mark.asyncio

# The turn's holder in the tenant gate.
TURN_HOLDER = "sess-1:turn-a"


class CountingGate:
    """A tenant gate with a cap that records its calls in order, and can fail on demand."""

    def __init__(
        self, *, held: int = 1, cap: int = 10, order: list[str] | None = None, fail_release: int = 0,
    ) -> None:
        self.held = held
        self.cap = cap
        self.calls: list[str] = []
        self.order = order if order is not None else []
        self.fail_release = fail_release
        self.fail_acquire = False
        # The holder each call named, in order.
        self.holders: list[str] = []

    async def release(self, org_id: str, agent_id: str, *, holder: str) -> bool:
        self.calls.append("release")
        self.holders.append(holder)
        if self.fail_release > 0:
            self.fail_release -= 1
            raise RuntimeError("redis blip")
        self.held = max(0, self.held - 1)
        return True

    async def try_acquire(self, org_id: str, agent_id: str, *, holder: str, limit: int | None = None) -> bool:
        self.calls.append("try_acquire")
        self.holders.append(holder)
        self.order.append("gate")
        if self.fail_acquire:
            raise RuntimeError("redis down")
        if self.held >= (limit if limit is not None else self.cap):
            return False
        self.held += 1
        return True


async def held_turn(
    *, gate: CountingGate | None = None, session_id: str = "",
) -> tuple[TurnSlots, asyncio.Semaphore, CountingGate]:
    """A turn as the dispatcher starts one: its tenant slot and the worker's only semaphore slot taken."""
    semaphore = asyncio.Semaphore(1)
    await semaphore.acquire()
    gate = gate if gate is not None else CountingGate(held=1)
    slots = TurnSlots(
        semaphore=semaphore, gate=gate, org_id="org", agent_id="agent", gate_held=True,
        gate_holder=TURN_HOLDER, session_id=session_id,
    )
    return slots, semaphore, gate


@contextlib.asynccontextmanager
async def as_tool_call(slots: TurnSlots) -> AsyncIterator[None]:
    """A tool call as production runs it: the turn's activity, the loop joining, the tool's own activity."""
    async with slots.activity():
        async with slots.joining():
            async with slots.activity():
                yield


async def test_a_lone_wait_gives_both_slots_back():
    slots, semaphore, gate = await held_turn()
    async with slots.activity():
        async with slots.waiting():
            assert not semaphore.locked()
            assert gate.held == 0
        assert semaphore.locked()
        assert gate.held == 1


async def test_the_turn_gives_back_and_takes_back_under_its_own_holder():
    slots, semaphore, gate = await held_turn()
    async with slots.activity():
        async with slots.waiting():
            pass
    await slots.release_owned()
    assert gate.calls == ["release", "try_acquire", "release"]
    assert gate.holders == [TURN_HOLDER] * 3


async def test_a_wait_keeps_the_slots_while_a_sibling_still_runs():
    slots, semaphore, gate = await held_turn()
    sibling_running = asyncio.Event()
    waiting = asyncio.Event()
    finish = asyncio.Event()

    async def waiter() -> None:
        await sibling_running.wait()
        async with slots.activity():
            async with slots.waiting():
                waiting.set()
                await finish.wait()
                await asyncio.sleep(0.01)

    async def sibling() -> None:
        async with slots.activity():
            sibling_running.set()
            await waiting.wait()
            # The sibling still works, so the turn kept its slots all along.
            assert semaphore.locked() and gate.held == 1
            assert gate.calls == []
            finish.set()
        # Only the wait is left: the whole turn waits.
        assert not semaphore.locked() and gate.held == 0

    await asyncio.gather(waiter(), sibling())
    assert semaphore.locked() and gate.held == 1


async def test_nested_waits_count_once():
    slots, semaphore, gate = await held_turn()
    sibling_running = asyncio.Event()
    nested = asyncio.Event()
    finish = asyncio.Event()

    async def waiter() -> None:
        await sibling_running.wait()
        async with slots.activity():
            async with slots.waiting():
                async with slots.waiting():
                    nested.set()
                    await finish.wait()

    async def sibling() -> None:
        async with slots.activity():
            sibling_running.set()
            await nested.wait()
            # One activity waits (twice over) and one runs: the turn keeps its slots.
            assert semaphore.locked() and gate.calls == []
            finish.set()

    await asyncio.gather(waiter(), sibling())


async def test_resuming_takes_the_tenant_slot_before_the_semaphore():
    order: list[str] = []

    class RecordingSemaphore(asyncio.Semaphore):
        async def acquire(self) -> bool:
            order.append("semaphore")
            return await super().acquire()

    semaphore = RecordingSemaphore(1)
    await semaphore.acquire()
    order.clear()
    gate = CountingGate(held=1, order=order)
    slots = TurnSlots(semaphore=semaphore, gate=gate, org_id="org", agent_id="agent", gate_held=True)
    async with slots.activity():
        async with slots.waiting():
            pass
    assert order == ["gate", "semaphore"]


async def test_resuming_waits_for_a_free_worker_slot():
    slots, semaphore, gate = await held_turn()
    other_done = asyncio.Event()

    async def other_session() -> None:
        await semaphore.acquire()  # dequeued into the slot the waiting turn gave back
        await other_done.wait()
        semaphore.release()

    async def turn() -> asyncio.Task:
        async with slots.activity():
            async with slots.waiting():
                other = asyncio.create_task(other_session())
                await asyncio.sleep(0.01)
            # Reached only once the other session freed the worker's only slot.
            assert other_done.is_set()
        return other

    running = asyncio.create_task(turn())
    await asyncio.sleep(0.05)
    assert not running.done()
    other_done.set()
    other = await asyncio.wait_for(running, 1.0)
    await other


async def test_a_resumed_turn_takes_its_tenant_slot_even_at_the_cap():
    slots, semaphore, gate = await held_turn(gate=CountingGate(held=1, cap=1))
    async with slots.activity():
        async with slots.waiting():
            gate.held = 1  # another session of the tenant took the freed slot
        # Admitted already: the cap governs new turns, not this one.
        assert semaphore.locked()
        assert gate.held == 2


async def test_a_gate_outage_on_resume_counts_the_slot_as_taken():
    slots, semaphore, gate = await held_turn()
    async with slots.activity():
        async with slots.waiting():
            gate.fail_acquire = True
        assert semaphore.locked()  # the turn runs on
    await slots.release_owned()
    # The first release is the original give-back; the second is the end of the
    # turn giving back the slot the failed take-back may have taken.
    assert gate.calls.count("release") == 2


async def test_a_take_back_that_times_out_is_given_back_at_the_end(monkeypatch):
    monkeypatch.setattr(turn_slots_module, "GATE_CALL_TIMEOUT_S", 0.05)

    class LateReplyGate(CountingGate):
        async def try_acquire(self, org_id: str, agent_id: str, *, holder: str, limit: int | None = None) -> bool:
            self.calls.append("try_acquire")
            self.held += 1  # the holder was added
            await asyncio.sleep(0.2)  # the reply comes after the timeout
            return True

    slots, semaphore, gate = await held_turn(gate=LateReplyGate(held=1))
    async with slots.activity():
        async with slots.waiting():
            pass
        assert semaphore.locked()  # the turn runs on
    await slots.release_owned()
    assert gate.held == 0, "a tenant slot that was taken and never given back stays counted"


async def test_a_cancelled_take_back_is_given_back_at_the_end():
    class BlockedGate(CountingGate):
        def __init__(self, **kwargs) -> None:
            super().__init__(**kwargs)
            self.in_take_back = asyncio.Event()

        async def try_acquire(self, org_id: str, agent_id: str, *, holder: str, limit: int | None = None) -> bool:
            self.calls.append("try_acquire")
            self.held += 1  # the holder was added
            self.in_take_back.set()
            await asyncio.Event().wait()  # the reply never comes
            return True

    slots, semaphore, gate = await held_turn(gate=BlockedGate(held=1))

    async def turn() -> None:
        async with slots.activity():
            async with slots.waiting():
                pass  # leaving the wait starts the take-back

    task = asyncio.create_task(turn())
    await asyncio.wait_for(gate.in_take_back.wait(), 1.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await slots.release_owned()
    assert gate.held == 0, "a tenant slot that was taken and never given back stays counted"


async def test_a_cancelled_give_back_counts_as_given_back():
    class BlockedGate(CountingGate):
        def __init__(self, **kwargs) -> None:
            super().__init__(**kwargs)
            self.in_give_back = asyncio.Event()

        async def release(self, org_id: str, agent_id: str, *, holder: str) -> bool:
            self.calls.append("release")
            self.held = max(0, self.held - 1)  # the holder was removed
            if self.calls.count("release") == 1:
                self.in_give_back.set()
                await asyncio.Event().wait()  # the reply never comes
            return True

    slots, semaphore, gate = await held_turn(gate=BlockedGate(held=1))

    async def turn() -> None:
        async with slots.activity():
            async with slots.waiting():  # entering the wait starts the give-back
                pass

    task = asyncio.create_task(turn())
    await asyncio.wait_for(gate.in_give_back.wait(), 1.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await slots.release_owned()
    # Giving back a slot twice would take another session's.
    assert gate.calls.count("release") == 1


async def test_a_failed_give_back_keeps_the_slot_for_the_turns_end(caplog):
    slots, semaphore, gate = await held_turn(gate=CountingGate(held=1, fail_release=1))
    async with slots.activity():
        async with slots.waiting():
            assert not semaphore.locked()  # the worker slot went back
            assert gate.held == 1  # the tenant slot could not
        assert "try_acquire" not in gate.calls  # still held: nothing to take back
    await slots.release_owned()
    assert gate.held == 0
    assert gate.calls.count("release") == 2
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR], "a slot given back at the end did not leak"


async def test_a_tenant_slot_that_cannot_be_given_back_at_the_end_is_logged_as_a_leak(caplog):
    # The mid-turn give-back fails and so does the end of the turn: the gate has no TTL.
    slots, semaphore, gate = await held_turn(gate=CountingGate(held=1, fail_release=2), session_id="sess-42")
    async with slots.activity():
        async with slots.waiting():
            pass
    await slots.release_owned()
    assert gate.held == 1
    leaks = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(leaks) == 1
    message = leaks[0].getMessage()
    assert "leaked" in message and "sess-42" in message and "org=org" in message and "agent=agent" in message


async def test_a_new_activity_takes_the_slots_back_first():
    slots, semaphore, gate = await held_turn()
    waiting = asyncio.Event()
    finish = asyncio.Event()

    async def waiter() -> None:
        async with slots.activity():
            async with slots.waiting():
                waiting.set()
                await finish.wait()

    running = asyncio.create_task(waiter())
    await waiting.wait()
    assert not semaphore.locked()
    async with slots.activity():
        assert semaphore.locked() and gate.held == 1
        finish.set()
    await running


async def test_an_error_leaving_a_wait_takes_the_slots_back():
    # A device operation that fails ends its wait with an error, not a cancellation.
    slots, semaphore, gate = await held_turn()
    async with slots.activity():
        with pytest.raises(RuntimeError):
            async with slots.waiting():
                assert not semaphore.locked() and gate.held == 0
                raise RuntimeError("the operation failed")
        assert semaphore.locked() and gate.held == 1


async def test_an_error_leaving_a_join_takes_the_slots_back():
    slots, semaphore, gate = await held_turn()
    waiting = asyncio.Event()

    async def tool() -> None:
        async with slots.activity():
            async with slots.waiting():
                waiting.set()
                await asyncio.Event().wait()

    async with slots.activity():
        task = asyncio.create_task(tool())
        with pytest.raises(RuntimeError):
            async with slots.joining():
                await waiting.wait()
                assert not semaphore.locked() and gate.held == 0  # the whole turn waits
                raise RuntimeError("the turn failed while it waited")
        # The turn goes on, so it holds its slots again.
        assert semaphore.locked() and gate.held == 1
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_the_turn_stops_counting_while_it_waits_for_its_tools():
    slots, semaphore, gate = await held_turn()
    async with slots.activity():  # the turn's own work
        async with slots.joining():  # it waits for its tool calls
            async with slots.activity():  # a tool call
                async with slots.waiting():
                    assert not semaphore.locked() and gate.held == 0
        assert semaphore.locked() and gate.held == 1


async def test_a_cancelled_wait_leaves_the_slots_for_the_turn_to_take_back():
    slots, semaphore, gate = await held_turn()
    waiting = asyncio.Event()

    async def tool() -> None:
        async with slots.activity():
            async with slots.waiting():
                waiting.set()
                await asyncio.Event().wait()

    async with slots.activity():
        async with slots.joining():
            task = asyncio.create_task(tool())
            await waiting.wait()
            task.cancel()  # discarded, as an interrupt or a stream retry does
            await asyncio.gather(task, return_exceptions=True)
            assert not semaphore.locked()  # a cancelled call takes nothing back
        # The turn takes the slots back before it does anything else.
        assert semaphore.locked() and gate.held == 1


async def test_cleanup_gives_back_only_what_the_turn_still_holds():
    slots, semaphore, gate = await held_turn()
    waiting = asyncio.Event()
    answer = asyncio.Event()

    async def turn() -> None:
        async with slots.activity():
            async with slots.waiting():
                waiting.set()
                await answer.wait()

    running = asyncio.create_task(turn())
    await waiting.wait()
    await slots.release_owned()  # the turn ended while it waited
    answer.set()  # the answer arrives anyway
    await asyncio.wait_for(running, 1.0)
    assert gate.calls.count("try_acquire") == 0, "a slot was taken back after the turn ended"
    assert gate.calls.count("release") == 1, "the tenant slot was given back twice"
    await semaphore.acquire()
    assert semaphore.locked(), "the semaphore slot was given back twice"


async def test_a_turn_without_a_tenant_slot_never_takes_one():
    semaphore = asyncio.Semaphore(1)
    await semaphore.acquire()
    gate = CountingGate(held=0)
    slots = TurnSlots(semaphore=semaphore, gate=gate, org_id="org", agent_id="agent", gate_held=False)
    async with slots.activity():
        async with slots.waiting():
            pass
    assert gate.calls == []
    assert semaphore.locked()


async def test_without_a_turn_the_helpers_do_nothing():
    assert current_turn.get() is None
    async with turn_activity():
        async with turn_joining():
            async with turn_waiting():
                pass


async def test_detaching_a_turn_cancels_it_and_it_takes_no_slot_again():
    semaphore = asyncio.Semaphore(1)
    await semaphore.acquire()
    gate = CountingGate(held=1)
    turn: list[TurnSlots] = []
    waiting = asyncio.Event()

    async def run_turn() -> None:
        slots = TurnSlots(
            semaphore=semaphore, gate=gate, org_id="org", agent_id="agent",
            gate_held=True, task=asyncio.current_task(),
        )
        turn.append(slots)
        try:
            async with slots.activity(), slots.joining(), slots.activity(), slots.waiting():
                waiting.set()
                await asyncio.Event().wait()
        finally:
            await slots.release_owned()

    task = asyncio.create_task(run_turn())
    await asyncio.wait_for(waiting.wait(), 5.0)
    assert turn[0].all_waiting
    turn[0].detach()
    await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled() and turn[0].detached
    assert gate.held == 0 and gate.calls.count("release") == 1
    await semaphore.acquire()
    assert semaphore.locked(), "the semaphore slot was given back twice"


async def _a_tool_call_blocked_taking_its_slot_back(slots: TurnSlots, semaphore: asyncio.Semaphore) -> asyncio.Task:
    """A tool call waits, the turn gives its slot back, another turn takes it, and the call's wait ends."""
    answered = asyncio.Event()

    async def tool_call() -> None:
        async with slots.activity():
            async with slots.waiting():
                await answered.wait()

    call = asyncio.create_task(tool_call())
    await asyncio.sleep(0.05)
    assert not semaphore.locked(), "all of the turn waits, so its slot went back"
    await semaphore.acquire()  # another turn takes it
    answered.set()
    await asyncio.sleep(0.05)
    assert not call.done(), "the worker is full, so the take-back waits"
    return call


def _unstick(call: asyncio.Task, semaphore: asyncio.Semaphore) -> None:
    """A take-back still blocked means the test failed: release it so the test ends instead of hanging."""
    if not call.done():
        call.cancel()
        semaphore.release()


async def test_an_interrupted_turn_stops_waiting_for_a_slot_it_was_taking_back():
    semaphore = asyncio.Semaphore(1)
    await semaphore.acquire()  # the dispatch loop's, for this turn
    slots = TurnSlots(semaphore=semaphore, gate=None, org_id="", agent_id="", gate_held=False)
    async with slots.activity(), slots.joining():
        call = await _a_tool_call_blocked_taking_its_slot_back(slots, semaphore)
        try:
            slots.interrupt()
            await asyncio.wait_for(call, 1.0)
        finally:
            _unstick(call, semaphore)
    await slots.release_owned()
    assert semaphore.locked(), "a slot the turn never took back was released"
    semaphore.release()  # the other turn ends
    await asyncio.sleep(0)
    assert not semaphore.locked(), "a stopped take-back swallowed the next free slot"


async def test_ending_a_turn_does_not_wait_behind_a_blocked_take_back():
    semaphore = asyncio.Semaphore(1)
    await semaphore.acquire()
    slots = TurnSlots(semaphore=semaphore, gate=None, org_id="", agent_id="", gate_held=False)
    async with slots.activity(), slots.joining():
        call = await _a_tool_call_blocked_taking_its_slot_back(slots, semaphore)
        try:
            await asyncio.wait_for(slots.release_owned(), 1.0)
            await asyncio.wait_for(call, 1.0)
        finally:
            _unstick(call, semaphore)
    assert semaphore.locked(), "a slot the turn never took back was released"


async def test_a_take_back_stopped_as_its_slot_arrives_keeps_no_slot():
    semaphore = asyncio.Semaphore(1)
    await semaphore.acquire()
    slots = TurnSlots(semaphore=semaphore, gate=None, org_id="", agent_id="", gate_held=False)
    slots._semaphore_held = False  # it gave the slot back while it waited
    taking = asyncio.create_task(slots._acquire_semaphore())
    await asyncio.sleep(0.05)
    semaphore.release()     # the slot comes:
    await asyncio.sleep(0)  # the inner acquire takes it ...
    taking.cancel()         # ... just before the take-back is stopped
    await asyncio.gather(taking, return_exceptions=True)
    assert not semaphore.locked(), "the slot that came as the take-back was stopped was lost"
