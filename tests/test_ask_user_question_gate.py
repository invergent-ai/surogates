"""``ask_user_question`` gives the turn's slots back while a human deliberates.

The handler parks the worker for up to 30 minutes waiting for an answer. That
wait is idle -- it polls at 1 Hz and renews the lease; it consumes no worker
CPU. Counting it as an in-flight turn is a category error: a worker slot and a
tenant turn-gate slot (a cap of only 10 per (org, agent)) tracks active work,
not sleeping waiters. Ten agents waiting on questions saturate the tenant and
every unrelated session is requeued behind them.

The ask runs as a tool call of a dispatched turn and waits through the turn's
record: once the whole turn waits, it gives back both the worker's semaphore
slot and the tenant's slot, and takes them back before the answer is handled.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from surogates.runtime.turn_slots import current_turn
from surogates.session.events import EventType
from surogates.tools.builtin.ask_user_question import (
    _ask_user_question_handler,
)

from tests.test_ask_user_question import FakeSessionStore
from tests.test_turn_slots import CountingGate, held_turn


class _IdentifiedStore(FakeSessionStore):
    """FakeSessionStore whose sessions carry a tenant identity, as real ones do."""

    def __init__(self, *, status: str = "active") -> None:
        super().__init__(status=status)
        self.org_id = uuid4()
        self.agent_id = "agent-X"

    async def get_session(self, session_id: Any) -> Any:
        return SimpleNamespace(
            id=session_id,
            status=self.status,
            org_id=self.org_id,
            agent_id=self.agent_id,
        )


async def _answer_after(store: FakeSessionStore, session_id, tool_call_id) -> None:
    await asyncio.sleep(0.05)
    await store.emit_event(
        session_id,
        EventType.ASK_USER_QUESTION_RESPONSE,
        {
            "tool_call_id": tool_call_id,
            "responses": [{"question": "q", "answer": "A", "is_other": False}],
        },
    )


async def _invoke(store, session_id, tool_call_id, slots) -> str:
    """Run the handler as a tool call of a dispatched turn: inside the turn's activity."""
    token = current_turn.set(slots)
    try:
        async with slots.activity():
            return await _ask_user_question_handler(
                {"questions": [{"prompt": "q"}]},
                session_id=session_id,
                session_store=store,
                tool_call_id=tool_call_id,
                lease_token=uuid4(),
            )
    finally:
        current_turn.reset(token)


@pytest.mark.asyncio
async def test_slot_is_released_while_waiting_and_taken_back_after():
    session_id, tool_call_id = uuid4(), "call_1"
    store = _IdentifiedStore()
    slots, semaphore, gate = await held_turn()

    observed: list[tuple[bool, int]] = []

    async def observe_then_answer() -> None:
        await asyncio.sleep(0.05)
        observed.append((semaphore.locked(), gate.held))
        await _answer_after(store, session_id, tool_call_id)

    raw, _ = await asyncio.gather(
        _invoke(store, session_id, tool_call_id, slots),
        observe_then_answer(),
    )

    assert observed == [(False, 0)], (
        "both the worker's slot and the tenant's must be given back while it waits"
    )
    assert json.loads(raw)["cancelled"] is False
    assert semaphore.locked() and gate.held == 1, (
        "both slots must be taken again before the answer is handled"
    )


@pytest.mark.asyncio
async def test_a_waiting_ask_does_not_consume_the_tenant_cap():
    """The point of the change: waiters must not saturate the tenant.

    With the slot released, a full cap's worth of pending questions leaves
    room for other sessions to run.
    """
    session_id, tool_call_id = uuid4(), "call_2"
    store = _IdentifiedStore()
    slots, _semaphore, gate = await held_turn(gate=CountingGate(held=1, cap=1))

    observed: list[int] = []

    async def observe_then_answer() -> None:
        await asyncio.sleep(0.05)
        observed.append(gate.held)
        await store.emit_event(
            session_id,
            EventType.ASK_USER_QUESTION_RESPONSE,
            {"tool_call_id": tool_call_id, "responses": []},
        )

    await asyncio.gather(
        _invoke(store, session_id, tool_call_id, slots),
        observe_then_answer(),
    )

    assert observed == [0], (
        "while the handler waits the tenant counter must be free, not held"
    )


@pytest.mark.asyncio
async def test_slot_is_taken_back_on_the_cancelled_path():
    """A stopped chat must not leak the slots the handler gave up."""
    session_id, tool_call_id = uuid4(), "call_3"
    store = _IdentifiedStore()
    slots, semaphore, gate = await held_turn()

    async def pause_after() -> None:
        await asyncio.sleep(0.05)
        store.status = "paused"

    raw, _ = await asyncio.gather(
        _invoke(store, session_id, tool_call_id, slots),
        pause_after(),
    )

    assert json.loads(raw)["cancelled"] is True
    assert semaphore.locked() and gate.held == 1


@pytest.mark.asyncio
async def test_no_gate_configured_still_answers():
    """A turn record is optional; a call outside a dispatched turn must be unaffected."""
    session_id, tool_call_id = uuid4(), "call_5"
    store = _IdentifiedStore()

    raw, _ = await asyncio.gather(
        _ask_user_question_handler(
            {"questions": [{"prompt": "q"}]},
            session_id=session_id,
            session_store=store,
            tool_call_id=tool_call_id,
            lease_token=uuid4(),
        ),
        _answer_after(store, session_id, tool_call_id),
    )

    assert json.loads(raw)["cancelled"] is False


@pytest.mark.asyncio
async def test_release_failure_does_not_break_the_wait():
    """A Redis blip on release must not cost the user their question."""
    session_id, tool_call_id = uuid4(), "call_6"
    store = _IdentifiedStore()

    class _FlakyReleaseGate(CountingGate):
        async def release(self, org_id: str, agent_id: str) -> None:
            self.calls.append("release")
            raise RuntimeError("redis blip")

    slots, semaphore, gate = await held_turn(gate=_FlakyReleaseGate(held=1))

    raw, _ = await asyncio.gather(
        _invoke(store, session_id, tool_call_id, slots),
        _answer_after(store, session_id, tool_call_id),
    )

    assert json.loads(raw)["cancelled"] is False
    assert "try_acquire" not in gate.calls, (
        "a slot that was never given back must not be taken again -- that would "
        "hand the tenant a slot it never gave up"
    )
    assert semaphore.locked()
