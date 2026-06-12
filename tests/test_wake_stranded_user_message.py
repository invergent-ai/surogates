"""A user reply racing with turn completion must resume the session.

Pins the PROD bug where replying to the agent's final assistant message
killed the chat thread: ``send_message`` reads ``session.status`` to
decide whether to emit ``SESSION_RESUME``, but between the final
``llm.response`` and ``_complete_session``'s status flip the session
still reads 'active', so the API appends the ``user.message`` and
enqueues without a resume.  Completion then sets status='completed'
with the cursor behind the new message, and every subsequent wake
bailed on the terminal-status check before looking at pending events —
stranding the reply forever (no harness.wake, no llm.request, session
shown as completed).

The fix: a wake on a completed/failed session checks for a real
(non-synthetic) ``user.message`` past the harness cursor and resumes
the session (status='active' + ``SESSION_RESUME``) instead of bailing.
Paused sessions keep bailing — pause is an explicit user stop, and a
later ``send_message`` on a paused session already resumes via the API
path.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

import surogates.harness.loop as loop_module
from surogates.harness.budget import IterationBudget
from surogates.harness.loop import AgentHarness
from surogates.sandbox.pool import SandboxPool
from surogates.session.events import EventType
from surogates.session.models import Session
from surogates.tenant.context import TenantContext
from surogates.tools.registry import ToolRegistry
from uuid import UUID


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


def _session(status: str) -> Session:
    now = datetime.now(timezone.utc)
    return Session(
        id=uuid4(),
        user_id=uuid4(),
        org_id=uuid4(),
        agent_id="agent-1",
        channel="api",
        status=status,
        config={},
        created_at=now,
        updated_at=now,
    )


def _user_message_event(event_id: int, *, synthetic: bool = False) -> Any:
    data: dict[str, Any] = {"content": "yes"}
    if synthetic:
        data["synthetic"] = "mission_continuation"
    return SimpleNamespace(
        id=event_id,
        type=EventType.USER_MESSAGE.value,
        data=data,
    )


def _stub_store(
    session: Session,
    *,
    cursor: int,
    pending_user_messages: list[Any],
) -> AsyncMock:
    store = AsyncMock()
    store.get_session.return_value = session
    store.emit_event = AsyncMock(return_value=cursor + 100)
    store.get_harness_cursor = AsyncMock(return_value=cursor)
    store.get_events = AsyncMock(return_value=pending_user_messages)
    # Lease denied: a resumed wake returns "lease_held" right after the
    # resume bookkeeping, which proves it got PAST the terminal-status
    # bail without dragging the whole turn machinery into the test.
    store.try_acquire_lease = AsyncMock(return_value=None)
    store.release_lease = AsyncMock(return_value=None)
    store.update_session_status = AsyncMock(return_value=None)
    return store


def _patch_resolver(monkeypatch) -> None:
    async def _resolver(*_args, **_kwargs):
        return None

    monkeypatch.setattr(loop_module, "resolve_agent_def", _resolver)


def _resume_calls(store: AsyncMock) -> list[Any]:
    return [
        call for call in store.emit_event.call_args_list
        if call.args and call.args[1] == EventType.SESSION_RESUME
    ]


@pytest.mark.asyncio
async def test_completed_session_with_stranded_user_message_resumes(monkeypatch):
    """The PROD race: completed status, real user.message past cursor."""
    session = _session("completed")
    store = _stub_store(
        session,
        cursor=791861,
        pending_user_messages=[_user_message_event(791907)],
    )
    harness = _harness(store)
    _patch_resolver(monkeypatch)

    result = await harness.wake(session.id)

    # Got past the terminal-status bail all the way to lease acquisition.
    assert result == "lease_held"
    store.update_session_status.assert_awaited_once_with(session.id, "active")
    resumes = _resume_calls(store)
    assert len(resumes) == 1, (
        "a stranded user message must emit exactly one SESSION_RESUME; "
        f"got {len(resumes)}"
    )
    assert resumes[0].args[2].get("source") == "stranded_user_message"


@pytest.mark.asyncio
async def test_failed_session_with_stranded_user_message_resumes(monkeypatch):
    """Same race against a crash: SESSION_FAIL landing after the reply."""
    session = _session("failed")
    store = _stub_store(
        session,
        cursor=100,
        pending_user_messages=[_user_message_event(105)],
    )
    harness = _harness(store)
    _patch_resolver(monkeypatch)

    result = await harness.wake(session.id)

    assert result == "lease_held"
    store.update_session_status.assert_awaited_once_with(session.id, "active")
    assert len(_resume_calls(store)) == 1


@pytest.mark.asyncio
async def test_completed_session_with_only_synthetic_message_skips(monkeypatch):
    """Synthetic user messages (mission continuations, nudges) must not
    revive a terminal session — same rule as the dispatcher's crash-loop
    ``_has_user_signal_since``."""
    session = _session("completed")
    store = _stub_store(
        session,
        cursor=100,
        pending_user_messages=[_user_message_event(105, synthetic=True)],
    )
    harness = _harness(store)
    _patch_resolver(monkeypatch)

    result = await harness.wake(session.id)

    assert result is None
    store.update_session_status.assert_not_called()
    assert _resume_calls(store) == []


@pytest.mark.asyncio
async def test_completed_session_without_pending_user_message_skips(monkeypatch):
    """Every completed session has bookkeeping events past the cursor
    (turn.summary / session.complete / inbox.task_complete) — only a
    real user.message may resume it."""
    session = _session("completed")
    store = _stub_store(session, cursor=100, pending_user_messages=[])
    harness = _harness(store)
    _patch_resolver(monkeypatch)

    result = await harness.wake(session.id)

    assert result is None
    store.update_session_status.assert_not_called()
    assert _resume_calls(store) == []


@pytest.mark.asyncio
async def test_paused_session_with_pending_user_message_skips(monkeypatch):
    """Pause is an explicit user stop; a stranded message must not
    override it.  (A later send_message on the paused session resumes
    through the API path.)"""
    session = _session("paused")
    store = _stub_store(
        session,
        cursor=100,
        pending_user_messages=[_user_message_event(105)],
    )
    harness = _harness(store)
    _patch_resolver(monkeypatch)

    result = await harness.wake(session.id)

    assert result is None
    store.update_session_status.assert_not_called()
    assert _resume_calls(store) == []
