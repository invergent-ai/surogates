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
from surogates.workstreams import thread_refusal


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


@pytest.mark.parametrize("command", ["loop", "auto-research", "deep-research", "code"])
def test_a_project_thread_refuses_a_command_that_starts_helpers(command):
    harness = _harness(AsyncMock(), _permissive())
    thread = _session()
    thread.config["workstream_role"] = "thread"
    assert harness._slash_command_block_reason(f"/{command} Go.", thread) == thread_refusal(f"/{command}")
    # Elsewhere they run as before.
    assert harness._slash_command_block_reason(f"/{command} Go.", _session()) is None


@pytest.mark.parametrize("command", ["mission"])
def test_a_project_thread_runs_a_command_whose_helpers_work_on_copies_of_their_own(command):
    harness = _harness(AsyncMock(), _permissive())
    thread = _session()
    thread.config["workstream_role"] = "thread"
    # Its helpers keep their work on its hand-off, which lands with it.
    assert harness._slash_command_block_reason(f"/{command} Go.", thread) is None


@pytest.mark.parametrize("command, answer", [
    ("loop", "A thread can't start /loop yet: do this step in the thread itself."),
    ("code", "A thread can't start /code yet: do this step in the thread itself."),
    # As a thread in the cloud answers them: the thread's rule comes before the local folder's.
    ("auto-research", "A thread can't start /auto-research yet: do this step in the thread itself."),
    ("deep-research", "A thread can't start /deep-research yet: do this step in the thread itself."),
    ("mission", None),
])
def test_a_project_thread_on_the_users_computer_runs_the_commands_its_folder_can(command, answer):
    harness = _harness(AsyncMock(), _permissive())
    thread = _session()
    thread.config["workstream_role"] = "thread"
    thread.config["execution"] = {"kind": "device", "device_id": "00000000-0000-0000-0000-0000000000d1"}
    assert harness._slash_command_block_reason(f"/{command} Go.", thread) == answer


@pytest.mark.asyncio
async def test_a_project_threads_loop_never_schedules_a_run(monkeypatch):
    monkeypatch.setattr(loop_module, "resolve_agent_def", AsyncMock(return_value=None))
    thread = _session()
    thread.config["workstream_role"] = "thread"
    store = _stub_store(thread, [_user_event(10, "/loop 5m Append a line to Report.docx.")])
    harness = _harness(store, _permissive())
    harness._handle_loop_command = AsyncMock()
    await harness.wake(thread.id)
    # No routine, so no run works on a copy its thread never lands.
    assert _llm_responses(store) == ["A thread can't start /loop yet: do this step in the thread itself."]
    harness._handle_loop_command.assert_not_awaited()


def test_a_local_folder_chat_refuses_auto_research():
    harness = _harness(AsyncMock(), _permissive())
    local = _session()
    local.config["execution"] = {"kind": "device", "device_id": str(uuid4())}
    assert harness._slash_command_block_reason("/auto-research Go.", local) == (
        "/auto-research is not available for sessions on a local folder"
    )
    # Its other commands run as before, and so does /auto-research elsewhere.
    assert harness._slash_command_block_reason("/mission Go.", local) is None
    assert harness._slash_command_block_reason("/auto-research Go.", _session()) is None
