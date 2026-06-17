"""End-to-end check that the slash-command capability gate is actually
wired into ``AgentHarness.wake``.

The unit tests in ``test_outcome_harness.py`` cover the gate *logic*
(``_slash_command_name`` / ``_slash_command_block_reason``).  This module
covers the *wiring*: it drives ``wake`` far enough to reach the dispatch
chain and asserts that a disabled command is refused with an
``LLM_RESPONSE`` and never reaches its handler, while an enabled command
flows through to its handler.  Guards against a refactor that drops the
gate insertion in ``wake`` — the logic unit tests would still pass.

The heavy pre-dispatch steps (title generation, context engineering,
system-prompt build, lease renewal) are stubbed so the test exercises the
gate without dragging the whole turn machinery in.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

import surogates.harness.loop as loop_module
from surogates.harness.budget import IterationBudget
from surogates.harness.context import ContextCompressor
from surogates.harness.loop import AgentHarness
from surogates.harness.prompt import PromptBuilder
from surogates.runtime import SlashCommandConfig
from surogates.sandbox.pool import SandboxPool
from surogates.session.events import EventType
from surogates.session.models import Session
from surogates.tenant.context import TenantContext
from surogates.tools.registry import ToolRegistry


def _tenant() -> TenantContext:
    return TenantContext(
        org_id=UUID("00000000-0000-0000-0000-000000000001"),
        user_id=UUID("00000000-0000-0000-0000-000000000002"),
        org_config={},
        user_preferences={},
        permissions=frozenset(),
        asset_root="/tmp/test",
    )


def _session() -> Session:
    now = datetime.now(timezone.utc)
    return Session(
        id=uuid4(),
        user_id=uuid4(),
        org_id=uuid4(),
        agent_id="agent-1",
        channel="api",
        status="active",
        config={},
        created_at=now,
        updated_at=now,
    )


def _user_event(event_id: int, content: str) -> Any:
    return SimpleNamespace(
        id=event_id,
        type=EventType.USER_MESSAGE.value,
        data={"content": content},
    )


def _stub_store(session: Session, events: list[Any]) -> AsyncMock:
    store = AsyncMock()
    store.get_session = AsyncMock(return_value=session)
    store.try_acquire_lease = AsyncMock(
        return_value=SimpleNamespace(lease_token="lease-tok"),
    )
    store.release_lease = AsyncMock(return_value=None)
    store.get_harness_cursor = AsyncMock(return_value=0)
    store.get_events = AsyncMock(return_value=events)
    store.emit_event = AsyncMock(return_value=1000)
    store.advance_harness_cursor = AsyncMock(return_value=None)
    return store


def _harness(store: Any, slash_commands: SlashCommandConfig) -> AgentHarness:
    h = AgentHarness(
        session_store=store,
        tool_registry=ToolRegistry(),
        llm_client=AsyncMock(),
        tenant=_tenant(),
        worker_id="test-worker",
        budget=IterationBudget(max_total=10),
        context_compressor=MagicMock(spec=ContextCompressor),
        prompt_builder=MagicMock(spec=PromptBuilder),
        sandbox_pool=MagicMock(spec=SandboxPool),
        slash_commands=slash_commands,
    )
    # Collapse the heavy pre-dispatch steps so wake() reaches the gate
    # (step 10) without the title/context/prompt machinery.
    h._renew_lease_forever = AsyncMock(return_value=None)
    h._maybe_generate_title = MagicMock(return_value=None)
    h._rebuild_messages = MagicMock(
        return_value=[{"role": "user", "content": "x"}],
    )
    h._engineer_context = AsyncMock(
        side_effect=lambda _session, _events, messages: messages,
    )
    h._build_system_prompt = AsyncMock(return_value="SYS")
    return h


def _llm_responses(store: AsyncMock) -> list[str]:
    return [
        c.args[2]["message"]["content"]
        for c in store.emit_event.call_args_list
        if c.args and c.args[1] == EventType.LLM_RESPONSE
    ]


def _permissive() -> SlashCommandConfig:
    return SlashCommandConfig()


def _without(*omit: str) -> SlashCommandConfig:
    from surogates.runtime import SLASH_COMMAND_IDS

    return SlashCommandConfig(
        commands=frozenset(SLASH_COMMAND_IDS - set(omit)),
    )


@pytest.mark.asyncio
async def test_disabled_command_refused_in_wake(monkeypatch):
    """``/loop`` individually disabled → refused, handler never reached."""
    monkeypatch.setattr(
        loop_module, "resolve_agent_def", AsyncMock(return_value=None),
    )
    session = _session()
    store = _stub_store(session, [_user_event(10, "/loop 5m /x")])
    harness = _harness(store, _without("loop"))
    harness._handle_loop_command = AsyncMock()

    await harness.wake(session.id)

    assert _llm_responses(store) == ["/loop is disabled for this agent."]
    harness._handle_loop_command.assert_not_awaited()


@pytest.mark.asyncio
async def test_enabled_command_reaches_handler(monkeypatch):
    """Permissive config → the gate stays out of the way and ``/loop``
    flows through to its dispatch handler."""
    monkeypatch.setattr(
        loop_module, "resolve_agent_def", AsyncMock(return_value=None),
    )
    session = _session()
    store = _stub_store(session, [_user_event(10, "/loop 5m /x")])
    harness = _harness(store, _permissive())
    harness._handle_loop_command = AsyncMock()

    await harness.wake(session.id)

    harness._handle_loop_command.assert_awaited_once()
    # The gate did not fire, so no "disabled" response was emitted.
    assert _llm_responses(store) == []
