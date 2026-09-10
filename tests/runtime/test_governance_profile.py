"""Tool execution enforces agent governance and records policy denials."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from surogates.harness.tool_exec import execute_single_tool
from surogates.runtime.governance import build_governance_gate
from surogates.session.events import EventType
from surogates.tools.registry import ToolRegistry, ToolSchema


# ---------------------------------------------------------------------------
# execute_single_tool enforcement
# ---------------------------------------------------------------------------


_ids = iter(range(1, 10_000))


def _make_registry(name: str = "terminal") -> tuple[ToolRegistry, AsyncMock]:
    registry = ToolRegistry()
    handler = AsyncMock(return_value='{"status": "ok"}')
    registry.register(
        name,
        ToolSchema(
            name=name,
            description="test tool",
            parameters={
                "type": "object",
                "properties": {"command": {"type": "string"}},
            },
        ),
        handler=handler,
    )
    return registry, handler


def _make_session() -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(), config={}, agent_id="test-agent", model="gpt-4o",
    )


def _make_store() -> AsyncMock:
    store = AsyncMock()
    store.emit_event = AsyncMock(side_effect=lambda *a, **k: next(_ids))
    store.advance_harness_cursor = AsyncMock()
    return store


@pytest.mark.asyncio
async def test_threaded_gate_denies_before_dispatch():
    registry, handler = _make_registry("terminal")
    store = _make_store()
    gate = build_governance_gate(
        {"enabled": True, "denied_tools": ["terminal"]},
    )

    result = await execute_single_tool(
        {
            "id": "tc_1",
            "function": {"name": "terminal", "arguments": '{"command": "ls"}'},
        },
        session=_make_session(),
        lease=SimpleNamespace(lease_token=uuid4()),
        store=store,
        tools=registry,
        tenant=MagicMock(asset_root="/tmp/test"),
        governance_gate=gate,
    )

    handler.assert_not_awaited()
    denied = [
        c for c in store.emit_event.call_args_list
        if c.args[1] is EventType.POLICY_DENIED
    ]
    assert denied, "policy.denied was never emitted"
    assert denied[0].args[2]["tool"] == "terminal"
    payload = json.loads(result["content"])
    assert "Blocked" in payload["error"]


@pytest.mark.asyncio
async def test_threaded_gate_allows_and_dispatches():
    registry, handler = _make_registry("terminal")
    store = _make_store()
    gate = build_governance_gate(
        {"enabled": True, "denied_tools": ["web_search"]},
    )

    result = await execute_single_tool(
        {
            "id": "tc_1",
            "function": {"name": "terminal", "arguments": '{"command": "ls"}'},
        },
        session=_make_session(),
        lease=SimpleNamespace(lease_token=uuid4()),
        store=store,
        tools=registry,
        tenant=MagicMock(asset_root="/tmp/test"),
        governance_gate=gate,
    )

    handler.assert_awaited()
    assert json.loads(result["content"])["status"] == "ok"


@pytest.mark.asyncio
async def test_no_gate_preserves_open_behaviour():
    registry, handler = _make_registry("terminal")
    store = _make_store()

    await execute_single_tool(
        {
            "id": "tc_1",
            "function": {"name": "terminal", "arguments": '{"command": "ls"}'},
        },
        session=_make_session(),
        lease=SimpleNamespace(lease_token=uuid4()),
        store=store,
        tools=registry,
        tenant=MagicMock(asset_root="/tmp/test"),
    )

    handler.assert_awaited()
