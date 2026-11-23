"""``load_tools`` reads the group catalog in the worker: it needs no sandbox, and works when there is none."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from surogates.governance.policy import GovernanceGate
from surogates.harness.tool_exec import execute_single_tool
from surogates.harness.tool_groups import loaded_by
from surogates.sandbox.base import SandboxUnavailableError
from surogates.session.events import EventType
from surogates.tools.builtin import tool_groups
from surogates.tools.registry import ToolRegistry
from surogates.tools.router import TOOL_LOCATIONS, ToolLocation


def _pool() -> SimpleNamespace:
    """A sandbox pool whose sandbox cannot start."""
    return SimpleNamespace(
        ensure=AsyncMock(side_effect=SandboxUnavailableError("no sandbox")),
        execute=AsyncMock(return_value="{}"),
    )


def _store() -> AsyncMock:
    store = AsyncMock()
    store.emit_event = AsyncMock(side_effect=range(1, 500))
    store.get_events = AsyncMock(return_value=[])
    return store


async def _load(session, pool, store=None, groups=("browser",)) -> tuple[dict, dict]:
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
