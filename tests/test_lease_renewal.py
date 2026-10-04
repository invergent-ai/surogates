"""A worker whose session lease another worker took stops the turn where it stands."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from surogates.runtime.turn_slots import TurnSlots, current_turn
from surogates.session.store import LeaseNotHeldError
from tests.test_steer_loop import _make_loop_harness

pytestmark = pytest.mark.asyncio


def _harness_losing_its_lease(monkeypatch):
    monkeypatch.setattr("surogates.harness.loop._LEASE_RENEWAL_INTERVAL_SECONDS", 0)
    store = AsyncMock()
    store.renew_lease = AsyncMock(side_effect=LeaseNotHeldError("taken"))
    harness = _make_loop_harness(session_store=store)
    monkeypatch.setattr(harness, "interrupt", MagicMock())
    return harness


async def test_a_lost_lease_detaches_the_turn_without_pausing_it(monkeypatch):
    harness = _harness_losing_its_lease(monkeypatch)
    turn: list[TurnSlots] = []

    async def run_turn() -> None:
        slots = TurnSlots(
            semaphore=asyncio.Semaphore(1), gate=None, org_id="", agent_id="",
            gate_held=False, task=asyncio.current_task(),
        )
        turn.append(slots)
        current_turn.set(slots)
        renewal = asyncio.create_task(harness._renew_lease_forever(uuid4(), uuid4()))
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.gather(renewal, return_exceptions=True)

    task = asyncio.create_task(run_turn())
    await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5.0)
    assert task.cancelled() and turn[0].detached
    harness.interrupt.assert_not_called()


async def test_a_lost_lease_outside_a_dispatched_turn_interrupts_the_loop(monkeypatch):
    harness = _harness_losing_its_lease(monkeypatch)
    await asyncio.wait_for(harness._renew_lease_forever(uuid4(), uuid4()), 5.0)
    harness.interrupt.assert_called_once()


@pytest.mark.parametrize(
    ("status", "reason"),
    [("archived", "session deleted"), ("paused", "paused by user")],
)
async def test_a_deleted_or_paused_session_stops_the_turn_without_a_signal(
    monkeypatch, status, reason,
):
    """The pub/sub interrupt can be lost; the renewal tick reads the status too."""
    monkeypatch.setattr("surogates.harness.loop._LEASE_RENEWAL_INTERVAL_SECONDS", 0)
    store = AsyncMock()
    store.renew_lease = AsyncMock(return_value=status)
    harness = _make_loop_harness(session_store=store)
    slots = TurnSlots(
        semaphore=asyncio.Semaphore(1), gate=None, org_id="", agent_id="",
        gate_held=False,
    )
    current_turn.set(slots)

    renewal = asyncio.create_task(harness._renew_lease_forever(uuid4(), uuid4()))
    try:
        async with asyncio.timeout(5.0):
            while not harness._check_interrupt():
                await asyncio.sleep(0)
    finally:
        renewal.cancel()

    assert harness._interrupt_message == reason
    assert slots._stop_waiting.is_set()


async def test_an_active_session_keeps_running(monkeypatch):
    monkeypatch.setattr("surogates.harness.loop._LEASE_RENEWAL_INTERVAL_SECONDS", 0)
    store = AsyncMock()
    store.renew_lease = AsyncMock(return_value="active")
    harness = _make_loop_harness(session_store=store)

    renewal = asyncio.create_task(harness._renew_lease_forever(uuid4(), uuid4()))
    while store.renew_lease.await_count < 3:
        await asyncio.sleep(0)
    renewal.cancel()

    assert not harness._check_interrupt()
