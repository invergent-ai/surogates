"""``load_tools`` reads the group catalog in the worker: it needs no sandbox, and works when there is none."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from surogates.governance.policy import GovernanceGate
from surogates.governance.saga import SagaOrchestrator
from surogates.governance.saga.state_machine import SagaState
from surogates.harness.tool_exec import SAGA_EXCLUDED_TOOLS, _is_saga_step, execute_single_tool
from surogates.harness.tool_groups import loaded_by
from surogates.sandbox.base import SandboxUnavailableError
from surogates.session.events import EventType
from surogates.tools.builtin import tool_groups
from surogates.tools.registry import ToolRegistry
from surogates.tools.router import TOOL_LOCATIONS, ToolLocation
from surogates.workstreams import THREAD


def _pool() -> SimpleNamespace:
    """A sandbox pool whose sandbox cannot start."""
    return SimpleNamespace(
        ensure=AsyncMock(side_effect=SandboxUnavailableError("no sandbox")),
        execute=AsyncMock(return_value="{}"),
        copy_fresh=lambda _owner: False,
    )


def _store() -> AsyncMock:
    store = AsyncMock()
    store.emit_event = AsyncMock(side_effect=range(1, 500))
    store.get_events = AsyncMock(return_value=[])
    return store


async def _load(session, pool, store=None, groups=("browser",), saga=None) -> tuple[dict, dict]:
    tools = ToolRegistry()
    tool_groups.register(tools)
    call = {"id": "call_1", "function": {"name": "load_tools", "arguments": json.dumps({"groups": list(groups)})}}
    result = await execute_single_tool(
        call,
        session=session,
        lease=SimpleNamespace(lease_token=uuid4()),
        store=store or _store(),
        tools=tools,
        tenant=SimpleNamespace(org_id=session.org_id),
        governance_gate=GovernanceGate(),
        sandbox_pool=pool,
        saga=saga,
    )
    return call, result


def _session(**more) -> SimpleNamespace:
    return SimpleNamespace(id=uuid4(), config={}, agent_id="a", org_id=uuid4(), parent_id=None, **more)


def test_load_tools_runs_in_the_worker() -> None:
    assert TOOL_LOCATIONS["load_tools"] is ToolLocation.HARNESS


@pytest.mark.asyncio
async def test_a_load_with_the_sandbox_down_starts_none_and_releases_the_group() -> None:
    pool = _pool()
    call, result = await _load(_session(), pool)
    assert json.loads(result["content"])["loaded"] == ["browser"]
    assert loaded_by([call], [result]) == {"browser"}
    pool.ensure.assert_not_awaited()
    pool.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_sub_agent_loads_a_group_with_the_sandbox_down() -> None:
    pool = _pool()
    child = _session()
    child.parent_id = uuid4()
    child.config = {"tool_allow_list": ["load_tools", "read_file"]}
    call, result = await _load(child, pool)
    assert loaded_by([call], [result]) == {"browser"}
    pool.ensure.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_session_not_allowed_the_tool_is_still_refused() -> None:
    pool = _pool()
    session = _session()
    session.config = {"tool_allow_list": ["read_file"]}
    call, result = await _load(session, pool)
    assert "allow-list" in result["content"] and loaded_by([call], [result]) == set()
    pool.ensure.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_unknown_group_is_answered_and_releases_nothing() -> None:
    pool = _pool()
    call, result = await _load(_session(), pool, groups=("no_such_group",))
    assert "No such tool group" in json.loads(result["content"])["error"]
    assert loaded_by([call], [result]) == set()
    pool.ensure.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_result_event_is_emitted_for_the_load() -> None:
    store = _store()
    await _load(_session(), _pool(), store=store)
    kinds = [c.args[1] for c in store.emit_event.await_args_list]
    assert EventType.TOOL_CALL in kinds and EventType.TOOL_RESULT in kinds


def _thread() -> SimpleNamespace:
    session = _session()
    session.config = {"workstream_role": THREAD}
    return session


@pytest.mark.asyncio
async def test_a_load_in_a_cloud_thread_starts_no_sandbox_for_a_snapshot() -> None:
    pool = SimpleNamespace(
        ensure=AsyncMock(return_value="sandbox"), execute=AsyncMock(return_value='{"hash": "h"}'),
        holds_copy=lambda _owner: True, copy_fresh=lambda _owner: False,
    )
    session, saga = _thread(), SagaOrchestrator()
    current = saga.create_saga(session.id)
    call, result = await _load(session, pool, saga=saga)
    assert loaded_by([call], [result]) == {"browser"}
    pool.ensure.assert_not_awaited()
    pool.execute.assert_not_awaited()
    assert current.steps == []


@pytest.mark.asyncio
async def test_a_stop_after_a_load_with_the_sandbox_down_leaves_nothing_to_undo() -> None:
    session, saga = _thread(), SagaOrchestrator()
    current = saga.create_saga(session.id)
    await _load(session, _pool(), saga=saga)
    assert await saga.compensate(current.saga_id, AsyncMock()) == []
    assert current.state is SagaState.COMPLETED


def test_a_load_stays_a_saga_step_on_the_computer_only() -> None:
    assert _is_saga_step("load_tools", on_device=True) is True
    assert _is_saga_step("load_tools", on_device=False) is False
    # What a thread's turn end on the computer counts as work to land.
    assert "load_tools" not in SAGA_EXCLUDED_TOOLS
    for on_device in (True, False):
        assert _is_saga_step("write_file", on_device=on_device) is True
        assert _is_saga_step("read_file", on_device=on_device) is False
