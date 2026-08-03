"""``ask_user_question`` must not hold a turn slot while a human deliberates.

The handler parks the worker for up to 30 minutes waiting for an answer. That
wait is idle — it polls at 1 Hz and renews the lease; it consumes no worker
CPU. Counting it as an in-flight turn is a category error: the
``TurnConcurrencyGate`` tracks active work, not sleeping waiters, and its cap
is only 10 per (org, agent). Ten agents waiting on questions saturate the
tenant and every unrelated session is requeued behind them.

``delegate_task`` already solved exactly this for its own blocking wait
(``surogates/tools/builtin/delegate.py``). These tests pin the same contract
for the ask path.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from surogates.session.events import EventType
from surogates.tools.builtin.ask_user_question import (
    _ask_user_question_handler,
)

from tests.test_ask_user_question import FakeSessionStore


class _RecordingGate:
    """Records the release/acquire sequence and models the cap."""

    def __init__(self, *, held: int = 1, cap: int = 10) -> None:
        self.held = held
        self.cap = cap
        self.calls: list[str] = []
        self.acquire_failures = 0

    async def release(self, org_id: str, agent_id: str) -> None:
        self.calls.append("release")
        self.held = max(0, self.held - 1)

    async def try_acquire(self, org_id: str, agent_id: str) -> bool:
        self.calls.append("try_acquire")
        if self.acquire_failures > 0:
            self.acquire_failures -= 1
            return False
        if self.held >= self.cap:
            return False
        self.held += 1
        return True


class _IdentifiedStore(FakeSessionStore):
    """FakeSessionStore whose sessions carry the tenant identity the gate needs."""

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


async def _invoke(store, session_id, tool_call_id, gate) -> str:
    return await _ask_user_question_handler(
        {"questions": [{"prompt": "q"}]},
        session_id=session_id,
        session_store=store,
        tool_call_id=tool_call_id,
        lease_token=uuid4(),
        turn_gate=gate,
    )


@pytest.mark.asyncio
async def test_slot_is_released_while_waiting_and_taken_back_after():
    session_id, tool_call_id = uuid4(), "call_1"
    store = _IdentifiedStore()
    gate = _RecordingGate(held=1)

    raw, _ = await asyncio.gather(
        _invoke(store, session_id, tool_call_id, gate),
        _answer_after(store, session_id, tool_call_id),
    )

    assert json.loads(raw)["cancelled"] is False
    assert gate.calls == ["release", "try_acquire"], (
        "the slot must be given back before the wait and taken again after"
    )
    assert gate.held == 1, "net slot usage across the call must be unchanged"


@pytest.mark.asyncio
async def test_a_waiting_ask_does_not_consume_the_tenant_cap():
    """The point of the change: waiters must not saturate the tenant.

    With the slot released, a full cap's worth of pending questions leaves
    room for other sessions to run.
    """
    session_id, tool_call_id = uuid4(), "call_2"
    store = _IdentifiedStore()
    gate = _RecordingGate(held=1, cap=1)

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
        _invoke(store, session_id, tool_call_id, gate),
        observe_then_answer(),
    )

    assert observed == [0], (
        "while the handler waits the tenant counter must be free, not held"
    )


@pytest.mark.asyncio
async def test_slot_is_taken_back_on_the_cancelled_path():
    """A stopped chat must not leak the slot the handler gave up."""
    session_id, tool_call_id = uuid4(), "call_3"
    store = _IdentifiedStore()
    gate = _RecordingGate(held=1)

    async def pause_after() -> None:
        await asyncio.sleep(0.05)
        store.status = "paused"

    raw, _ = await asyncio.gather(
        _invoke(store, session_id, tool_call_id, gate),
        pause_after(),
    )

    assert json.loads(raw)["cancelled"] is True
    assert gate.calls == ["release", "try_acquire"]
    assert gate.held == 1


@pytest.mark.asyncio
async def test_answer_is_still_returned_when_the_slot_cannot_be_retaken(
    monkeypatch: pytest.MonkeyPatch,
):
    """A saturated cap on the way out must not lose the user's answer.

    Under-counting the gate is acceptable; discarding an answer the human
    already gave is not.
    """
    monkeypatch.setattr(
        "surogates.runtime.turn_gate._REACQUIRE_TIMEOUT_SECONDS", 0.05,
    )
    monkeypatch.setattr(
        "surogates.runtime.turn_gate._REACQUIRE_BACKOFF_SECONDS", 0.01,
    )
    session_id, tool_call_id = uuid4(), "call_4"
    store = _IdentifiedStore()
    gate = _RecordingGate(held=1)
    gate.acquire_failures = 10_000  # never re-acquires

    raw, _ = await asyncio.gather(
        _invoke(store, session_id, tool_call_id, gate),
        _answer_after(store, session_id, tool_call_id),
    )

    result = json.loads(raw)
    assert result["cancelled"] is False
    assert result["responses"][0]["answer"] == "A"


@pytest.mark.asyncio
async def test_no_gate_configured_still_answers():
    """The gate is optional; deployments without one must be unaffected."""
    session_id, tool_call_id = uuid4(), "call_5"
    store = _IdentifiedStore()

    raw, _ = await asyncio.gather(
        _invoke(store, session_id, tool_call_id, None),
        _answer_after(store, session_id, tool_call_id),
    )

    assert json.loads(raw)["cancelled"] is False


@pytest.mark.asyncio
async def test_release_failure_does_not_break_the_wait():
    """A Redis blip on release must not cost the user their question."""
    session_id, tool_call_id = uuid4(), "call_6"
    store = _IdentifiedStore()

    class _FlakyReleaseGate(_RecordingGate):
        async def release(self, org_id: str, agent_id: str) -> None:
            self.calls.append("release")
            raise RuntimeError("redis blip")

    gate = _FlakyReleaseGate(held=1)

    raw, _ = await asyncio.gather(
        _invoke(store, session_id, tool_call_id, gate),
        _answer_after(store, session_id, tool_call_id),
    )

    assert json.loads(raw)["cancelled"] is False
    assert "try_acquire" not in gate.calls, (
        "a slot that was never released must not be re-acquired — that would "
        "hand the tenant a slot it never gave up"
    )


@pytest.mark.asyncio
async def test_unresolvable_session_identity_skips_the_gate_dance():
    """Without org/agent there is no slot to release; the ask must still work."""
    session_id, tool_call_id = uuid4(), "call_7"
    store = FakeSessionStore()  # get_session returns no org_id/agent_id
    gate = _RecordingGate(held=1)

    raw, _ = await asyncio.gather(
        _invoke(store, session_id, tool_call_id, gate),
        _answer_after(store, session_id, tool_call_id),
    )

    assert json.loads(raw)["cancelled"] is False
    assert gate.calls == []
