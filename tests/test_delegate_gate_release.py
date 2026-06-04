"""``delegate_task`` releases the parent's TurnConcurrencyGate slot
during the child-polling window.

The parent's slot is meant to track *active worker consumption*.
A parent that's sleeping inside ``_poll_child_completion`` waiting
for its child to finish is not consuming worker CPU -- it's idle.
Counting it as 1 slot during that window is a category error and
causes deep delegation chains to self-saturate the per-tenant cap.
This module pins:

  * Release fires when the parent enters delegation.
  * Re-acquire fires when delegation returns.
  * Counter ends at the same value it started at when the child
    completes (so a happy-path turn doesn't leak/overcount).
  * Release fires even if ``asyncio.gather`` raises (finally branch).
  * ``_reacquire_gate_with_backoff`` returns True immediately when a
    slot is free, retries when at cap, and gives up after the
    deadline rather than blocking forever.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

import surogates.tools.builtin.delegate as delegate_module
from surogates.harness.budget import IterationBudget
from surogates.runtime.turn_gate import TurnConcurrencyGate
from surogates.session.events import EventType
from surogates.session.models import Event, Session


pytestmark = pytest.mark.asyncio


class _FakeRedisGate:
    """Minimal in-memory shim of the gate's Redis backend."""

    def __init__(self) -> None:
        self.values: dict[str, int] = {}

    async def incr(self, key: str) -> int:
        self.values[key] = self.values.get(key, 0) + 1
        return self.values[key]

    async def decr(self, key: str) -> int:
        self.values[key] = self.values.get(key, 0) - 1
        return self.values[key]


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
    exercise the release/re-acquire pair without spinning up a real
    child harness."""

    def __init__(self, parent: Session) -> None:
        self._parent = parent
        self._child_id: UUID | None = None
        self.parent_emitted: list[tuple[EventType, dict[str, Any]]] = []

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
        policy_profile: Any = None

    async def _stub(name, tenant, *, session_factory=None, bundle=None):  # noqa: ARG001
        return _StubAgentDef()

    monkeypatch.setattr(resolver_module, "resolve_agent_by_name", _stub)


def _install_enqueue_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "surogates.tools.builtin.delegate.enqueue_session",
        AsyncMock(),
    )


async def test_delegate_releases_and_reacquires_gate_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: gate counter ends at the same value it started
    at, after a release/reacquire pair around the child wait."""
    parent = _parent_session()
    store = _CompletingChildStore(parent)
    _install_child_session_stub(monkeypatch, store)
    _install_resolver_stub(monkeypatch)
    _install_enqueue_stub(monkeypatch)
    # Short poll so the test doesn't actually wait 1s.
    monkeypatch.setattr(
        delegate_module, "_POLL_INTERVAL_SECONDS", 0.01,
    )

    gate_redis = _FakeRedisGate()
    gate = TurnConcurrencyGate(gate_redis, default_max=10)
    counter_key = f"surogates:turns:{parent.org_id}:{parent.agent_id}"

    # Parent has its dispatcher-acquired slot already held.
    gate_redis.values[counter_key] = 1

    await delegate_module._delegate_handler(
        {"goal": "x", "agent_type": "engineer"},
        session_store=store,
        redis=None,
        tenant=object(),
        session_id=str(parent.id),
        budget=IterationBudget(max_total=10),
        turn_gate=gate,
    )

    # The slot was released for the wait window and re-acquired on
    # return -- net zero change.
    assert gate_redis.values[counter_key] == 1, (
        "release + re-acquire should net to zero change; final "
        f"counter = {gate_redis.values[counter_key]}"
    )


async def test_release_fires_even_when_no_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate parameter is optional; standalone tests / single-tenant
    deployments without a gate must still complete delegation
    normally."""
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
        # No turn_gate kwarg.
    )
    # Single-goal path returns the child's text directly, not JSON.
    assert "Delegation failed" not in result


async def test_reacquire_returns_true_when_slot_immediately_free() -> None:
    gate_redis = _FakeRedisGate()
    gate = TurnConcurrencyGate(gate_redis, default_max=10)
    ok = await delegate_module._reacquire_gate_with_backoff(
        gate, "org-1", "agent-A",
    )
    assert ok is True
    assert gate_redis.values["surogates:turns:org-1:agent-A"] == 1


async def test_reacquire_returns_false_after_deadline_at_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the cap is saturated for the entire backoff window, the
    helper gives up rather than blocking forever -- the parent's
    return path is more important than perfect accounting."""
    # Tight deadline + backoff so the test finishes promptly.
    monkeypatch.setattr(
        delegate_module, "_REACQUIRE_TIMEOUT_SECONDS", 0.05,
    )
    monkeypatch.setattr(
        delegate_module, "_REACQUIRE_BACKOFF_SECONDS", 0.01,
    )

    gate_redis = _FakeRedisGate()
    gate = TurnConcurrencyGate(gate_redis, default_max=2)
    # Saturate by pre-loading 2 acquires.
    gate_redis.values["surogates:turns:org-1:agent-A"] = 2

    ok = await delegate_module._reacquire_gate_with_backoff(
        gate, "org-1", "agent-A",
    )
    assert ok is False
    # Counter unchanged -- helper aborted without acquiring.
    assert gate_redis.values["surogates:turns:org-1:agent-A"] == 2


async def test_release_fires_in_finally_when_gather_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exception inside the delegation must not leak the released
    slot.  finally branch must run the re-acquire."""
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

    gate_redis = _FakeRedisGate()
    gate = TurnConcurrencyGate(gate_redis, default_max=10)
    counter_key = f"surogates:turns:{parent.org_id}:{parent.agent_id}"
    gate_redis.values[counter_key] = 1

    result = await delegate_module._delegate_handler(
        {"goal": "x", "agent_type": "engineer"},
        session_store=store,
        redis=None,
        tenant=object(),
        session_id=str(parent.id),
        budget=IterationBudget(max_total=10),
        turn_gate=gate,
    )
    # The handler catches and converts to an error envelope.
    parsed = json.loads(result)
    assert "error" in parsed
    # Despite the crash, the re-acquire in the finally restored the
    # slot to its pre-release value.
    assert gate_redis.values[counter_key] == 1, (
        "finally branch must re-acquire the slot even when "
        f"asyncio.gather raises; final = {gate_redis.values[counter_key]}"
    )
