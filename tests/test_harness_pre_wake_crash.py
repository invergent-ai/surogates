"""Pre-wake hangs surface as ``harness.crash`` events instead of silent zombies.

Pins two regressions of the same bug:

  * A Hub hang in ``resolve_agent_def`` used to leave the writer
    silently re-enqueued forever (every 60s the orphan sweeper found
    a stale session, but no event was ever emitted and no log line
    pointed at the hang).  ``asyncio.wait_for`` with
    ``_PRE_WAKE_HUB_TIMEOUT_SECONDS`` now turns the next failure into a
    visible ``HARNESS_CRASH`` event with ``error_category=timeout``.

  * The except/finally cleanup paths used to assume ``session`` and
    ``lease`` were always bound -- a pre-wake crash would
    ``NameError`` out of the except clause itself, masking the real
    cause.  Both are initialized to ``None`` up front and the
    cleanup branches gate on that.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

import surogates.harness.loop as loop_module
from surogates.harness.budget import IterationBudget
from surogates.harness.loop import (
    AgentHarness,
    _PRE_WAKE_HUB_TIMEOUT_SECONDS,
)
from surogates.sandbox.pool import SandboxPool
from surogates.session.events import EventType
from surogates.session.models import Session, SessionLease
from surogates.tenant.context import TenantContext
from surogates.tools.registry import ToolRegistry


def _harness(store: Any) -> AgentHarness:
    from surogates.harness.context import ContextCompressor
    from surogates.harness.prompt import PromptBuilder

    tenant = TenantContext(
        org_id=UUID("00000000-0000-0000-0000-000000000001"),
        user_id=UUID("00000000-0000-0000-0000-000000000002"),
        org_config={},
        user_preferences={},
        permissions=frozenset(),
        asset_root="/tmp/test",
    )
    return AgentHarness(
        session_store=store,
        tool_registry=ToolRegistry(),
        llm_client=AsyncMock(),
        tenant=tenant,
        worker_id="test-worker",
        budget=IterationBudget(max_total=10),
        context_compressor=MagicMock(spec=ContextCompressor),
        prompt_builder=MagicMock(spec=PromptBuilder),
        sandbox_pool=MagicMock(spec=SandboxPool),
        vision_client=None,
        vision_model="",
    )


def _root_session() -> Session:
    now = datetime.now(timezone.utc)
    return Session(
        id=uuid4(),
        user_id=uuid4(),
        org_id=uuid4(),
        agent_id="agent-1",
        channel="web",
        status="active",
        config={},
        created_at=now,
        updated_at=now,
    )


def _child_session(parent_id: UUID) -> Session:
    now = datetime.now(timezone.utc)
    return Session(
        id=uuid4(),
        user_id=uuid4(),
        org_id=uuid4(),
        agent_id="agent-1",
        channel="delegation",
        status="active",
        config={"agent_type": "research-writer"},
        parent_id=parent_id,
        created_at=now,
        updated_at=now,
    )


def _stub_store(session: Session | None) -> AsyncMock:
    store = AsyncMock()
    if session is None:
        store.get_session.side_effect = RuntimeError("DB unavailable")
    else:
        store.get_session.return_value = session
    store.emit_event = AsyncMock(return_value=1)
    # try_acquire_lease and release_lease are touched in some paths;
    # set them up so the finally cleanup doesn't blow up when the test
    # reaches them.
    store.try_acquire_lease = AsyncMock(return_value=None)
    store.release_lease = AsyncMock(return_value=None)
    store.renew_lease = AsyncMock(return_value=None)
    store.get_harness_cursor = AsyncMock(return_value=0)
    store.get_events = AsyncMock(return_value=[])
    return store


@pytest.mark.asyncio
async def test_resolve_agent_def_timeout_emits_harness_crash(monkeypatch):
    """A Hub hang in resolve_agent_def must crash with
    ``error_category=timeout`` instead of producing no event at all."""
    session = _root_session()
    store = _stub_store(session)
    harness = _harness(store)

    async def _hangs(*_args, **_kwargs):
        # Simulate Hub never responding.  ``wait_for`` should cancel
        # this and raise ``asyncio.TimeoutError``.
        await asyncio.sleep(_PRE_WAKE_HUB_TIMEOUT_SECONDS + 5)

    monkeypatch.setattr(loop_module, "resolve_agent_def", _hangs)
    # Drop the real timeout to a few ms so the test is fast.  The
    # constant feeds the production code, but the wrapper inside
    # ``wake()`` always reads the module-level value, so monkey-patch
    # there too.
    monkeypatch.setattr(loop_module, "_PRE_WAKE_HUB_TIMEOUT_SECONDS", 0.05)

    with pytest.raises(asyncio.TimeoutError):
        await harness.wake(session.id)

    crash_calls = [
        call for call in store.emit_event.call_args_list
        if call.args and call.args[1] == EventType.HARNESS_CRASH
    ]
    assert len(crash_calls) == 1, (
        "exactly one harness.crash event should be emitted on a "
        f"pre-wake Hub hang; got {len(crash_calls)}"
    )
    data = crash_calls[0].args[2]
    assert data["error_category"] == "timeout", (
        f"hangs should classify as timeout, got {data['error_category']!r}"
    )
    assert data["worker_id"] == "test-worker"
    # The lease was never acquired, so release_lease must not run.
    store.release_lease.assert_not_called()


@pytest.mark.asyncio
async def test_get_session_failure_crashes_cleanly(monkeypatch):
    """A DB-level failure during ``get_session`` (the very first step
    inside the wake's try block) must produce a ``harness.crash`` event
    and not blow up the except branch with a ``NameError`` on the
    still-unbound ``session`` local."""
    store = _stub_store(session=None)  # get_session raises
    harness = _harness(store)

    # No ``resolve_agent_def`` patch -- the wake should crash earlier
    # at ``get_session``.  Lower the timeout so the test exits fast
    # even if the implementation changes order.
    monkeypatch.setattr(loop_module, "_PRE_WAKE_HUB_TIMEOUT_SECONDS", 0.05)

    fake_session_id = uuid4()
    with pytest.raises(RuntimeError, match="DB unavailable"):
        await harness.wake(fake_session_id)

    crash_calls = [
        call for call in store.emit_event.call_args_list
        if call.args and call.args[1] == EventType.HARNESS_CRASH
    ]
    assert len(crash_calls) == 1, (
        "get_session failure must still emit a harness.crash event "
        "(the regression we fixed was the except branch NameError-ing "
        "on the unbound ``session``)"
    )
    # No lease, no parent notification -- ``session`` was never loaded.
    store.release_lease.assert_not_called()


@pytest.mark.asyncio
async def test_paused_session_short_circuits_without_crash(monkeypatch):
    """The 'session already paused/completed/failed' short-circuit
    must NOT be reclassified as a crash by the new wrapping.  No
    event of any kind is emitted; the wake just returns."""
    session = _root_session()
    session.status = "paused"  # type: ignore[misc]  # Pydantic v2 allows
    store = _stub_store(session)
    harness = _harness(store)

    # If the code accidentally calls into resolve_agent_def the test
    # would deadlock; patch to a no-op that returns None so a future
    # ordering change is caught by the assertions below instead.
    async def _resolver(*_args, **_kwargs):
        return None

    monkeypatch.setattr(loop_module, "resolve_agent_def", _resolver)
    monkeypatch.setattr(loop_module, "_PRE_WAKE_HUB_TIMEOUT_SECONDS", 0.05)

    result = await harness.wake(session.id)
    assert result is None
    # No crash event for a deliberate early exit.
    crash_calls = [
        call for call in store.emit_event.call_args_list
        if call.args and call.args[1] == EventType.HARNESS_CRASH
    ]
    assert crash_calls == []
    store.release_lease.assert_not_called()
