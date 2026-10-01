"""``delegate_task`` gives the parent turn's slots back during the child-polling window.

The parent's slots are meant to track *active worker consumption*.
A parent that's sleeping inside ``_poll_child_completion`` waiting
for its child to finish is not consuming worker CPU -- it's idle.
Counting it as holding a worker slot and a tenant slot during that window is
a category error and causes deep delegation chains to self-saturate the
per-tenant cap and the worker.  The wait runs through the turn's record
(``current_turn``).  This module pins:

  * Both the worker's semaphore slot and the tenant's gate slot are given
    back while the parent waits for its children.
  * Both are taken back when delegation returns, so a happy-path turn
    doesn't leak or overcount.
  * They are taken back even if ``asyncio.gather`` raises.
  * A call outside a dispatched turn (no turn record) completes normally.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

import surogates.tools.builtin.delegate as delegate_module
from surogates.harness.budget import IterationBudget
from surogates.runtime.turn_slots import TurnSlots, current_turn
from surogates.session.events import EventType
from surogates.session.models import Event, Session

from tests.test_turn_slots import held_turn


pytestmark = pytest.mark.asyncio


def _parent_session() -> Session:
    now = datetime.now(timezone.utc)
    return Session(
        id=uuid4(),
        user_id=uuid4(),
        org_id=uuid4(),
        agent_id="agent-A",
        channel="web",
        status="active",
        config={},
        created_at=now,
        updated_at=now,
    )


class _CompletingChildStore:
    """Session store whose child immediately reports SESSION_COMPLETE
    so the polling loop exits on the first tick.  Lets the test
    exercise the give-back/take-back pair without spinning up a real
    child harness."""

    def __init__(self, parent: Session) -> None:
        self._parent = parent
        self._child_id: UUID | None = None
        self.parent_emitted: list[tuple[EventType, dict[str, Any]]] = []
        # Called the first time the child is polled, i.e. while the parent waits.
        self.on_first_poll: Callable[[], None] | None = None

    def set_child_id(self, child_id: UUID) -> None:
        self._child_id = child_id

    async def get_session(self, session_id: UUID) -> Session:
        if session_id == self._parent.id:
            return self._parent
        raise KeyError(session_id)

    async def emit_event(
        self, session_id: UUID, event_type: EventType, data: dict[str, Any],
    ) -> int:
        if session_id == self._parent.id:
            self.parent_emitted.append((event_type, data))
        return 1

    async def get_events(self, session_id: UUID) -> list[Event]:
        if self.on_first_poll is not None:
            self.on_first_poll()
            self.on_first_poll = None
        # Single LLM_RESPONSE + SESSION_COMPLETE on the child --
        # _poll_child_completion exits on the first poll tick.
        return [
            Event(
                session_id=session_id,
                type=EventType.LLM_RESPONSE.value,
                data={"message": {"role": "assistant", "content": "ok"}},
                created_at=datetime.now(timezone.utc),
            ),
            Event(
                session_id=session_id,
                type=EventType.SESSION_COMPLETE.value,
                data={},
                created_at=datetime.now(timezone.utc),
            ),
        ]

    async def update_session_config_key(
        self, session_id: UUID, key: str, value: Any,
    ) -> None:
        # Mirrors SessionStore: board group formation writes the parent's
        # context_group_id at spawn time.
        if session_id == self._parent.id:
            config = dict(self._parent.config or {})
            config[key] = value
            self._parent.config = config


def _install_child_session_stub(
    monkeypatch: pytest.MonkeyPatch, store: _CompletingChildStore,
) -> None:
    """Patch ``create_child_session`` so the test doesn't hit the
    real session-provisioning code path."""
    from surogates.session import provisioning as provisioning_module

    async def _stub(*, store, parent, channel, model, config, **_kwargs):
        child_id = uuid4()
        store.set_child_id(child_id)
        return Session(
            id=child_id,
            user_id=parent.user_id,
            org_id=parent.org_id,
            agent_id=parent.agent_id,
            channel=channel,
            status="active",
            model=model or parent.model,
            config={**(parent.config or {}), **(config or {})},
            parent_id=parent.id,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )

    monkeypatch.setattr(provisioning_module, "create_child_session", _stub)


def _install_resolver_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch resolve_agent_by_name so the agent_type check inside
    _run_single_delegation doesn't go through the loader/Hub."""
    import surogates.harness.agent_resolver as resolver_module

    class _StubAgentDef:
        tools: Any = None
        disallowed_tools: Any = None
        model: Any = None
        max_iterations: Any = None

    async def _stub(name, tenant, *, session_factory=None, bundle=None):  # noqa: ARG001
        return _StubAgentDef()

    monkeypatch.setattr(resolver_module, "resolve_agent_by_name", _stub)


def _install_enqueue_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "surogates.tools.builtin.delegate.enqueue_session",
        AsyncMock(),
    )


async def _delegate_in_turn(slots: TurnSlots, store: _CompletingChildStore, parent: Session) -> str:
    """Run the handler as a tool call of a dispatched turn: inside the turn's activity."""
    token = current_turn.set(slots)
    try:
        async with slots.activity():
            return await delegate_module._delegate_handler(
                {"goal": "x", "agent_type": "engineer"},
                session_store=store,
                redis=None,
                tenant=object(),
                session_id=str(parent.id),
                budget=IterationBudget(max_total=10),
            )
    finally:
        current_turn.reset(token)


async def test_delegate_releases_and_reacquires_gate_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: both slots are given back while the parent waits for
    its child, and held again when delegation returns."""
    parent = _parent_session()
    store = _CompletingChildStore(parent)
    _install_child_session_stub(monkeypatch, store)
    _install_resolver_stub(monkeypatch)
    _install_enqueue_stub(monkeypatch)
    # Short poll so the test doesn't actually wait 1s.
    monkeypatch.setattr(
        delegate_module, "_POLL_INTERVAL_SECONDS", 0.01,
    )

    slots, semaphore, gate = await held_turn()
    observed: list[tuple[bool, int]] = []
    store.on_first_poll = lambda: observed.append((semaphore.locked(), gate.held))

    await _delegate_in_turn(slots, store, parent)

    assert observed == [(False, 0)], (
        "both the worker's slot and the tenant's must be given back while "
        "the parent waits for its child"
    )
    assert semaphore.locked() and gate.held == 1, (
        "both slots must be taken again when delegation returns"
    )


async def test_release_fires_even_when_no_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The turn record is optional; standalone tests / single-tenant
    deployments run outside a dispatched turn and must still complete
    delegation normally."""
    parent = _parent_session()
    store = _CompletingChildStore(parent)
    _install_child_session_stub(monkeypatch, store)
    _install_resolver_stub(monkeypatch)
    _install_enqueue_stub(monkeypatch)
    monkeypatch.setattr(
        delegate_module, "_POLL_INTERVAL_SECONDS", 0.01,
    )

    result = await delegate_module._delegate_handler(
        {"goal": "x", "agent_type": "engineer"},
        session_store=store,
        redis=None,
        tenant=object(),
        session_id=str(parent.id),
        budget=IterationBudget(max_total=10),
        # No turn record.
    )
    # Single-goal path returns the child's text directly, not JSON.
    assert "Delegation failed" not in result


async def test_release_fires_in_finally_when_gather_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exception inside the delegation must not leak the slots the
    parent gave up: the turn takes them back on the way out."""
    parent = _parent_session()
    store = _CompletingChildStore(parent)
    _install_resolver_stub(monkeypatch)
    _install_enqueue_stub(monkeypatch)

    async def _failing_child_session(*_args, **_kwargs):
        raise RuntimeError("child provisioning blew up")

    from surogates.session import provisioning as provisioning_module
    monkeypatch.setattr(
        provisioning_module, "create_child_session", _failing_child_session,
    )

    slots, semaphore, gate = await held_turn()

    result = await _delegate_in_turn(slots, store, parent)

    # The handler catches and converts to an error envelope.
    parsed = json.loads(result)
    assert "error" in parsed
    # Despite the crash, both slots are held again.
    assert semaphore.locked() and gate.held == 1, (
        "the turn must take its slots back even when asyncio.gather raises; "
        f"semaphore.locked() = {semaphore.locked()}, gate.held = {gate.held}"
    )
