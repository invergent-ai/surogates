"""A simulated conversation runs tools that only read, and fakes the ones that change things."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from surogates.harness.tool_exec import execute_single_tool
from surogates.session.events import EventType
from surogates.simulation import side_effects
from surogates.simulation.side_effects import (
    changes_something,
    decide,
    fake_result,
    is_simulated,
    summary_judge,
    would_have,
)
from surogates.tools.registry import ToolRegistry, ToolSchema


@pytest.fixture(autouse=True)
def _fresh_cache():
    side_effects._decided.clear()
    yield
    side_effects._decided.clear()


def test_annotations_win():
    assert changes_something("mcp__crm__lookup", {"readOnlyHint": True}) is False
    assert changes_something("mcp__crm__lookup", {"destructiveHint": True}) is True


def test_composio_verbs():
    assert changes_something("mcp__composio__GMAIL_SEND_EMAIL", None) is True
    assert changes_something("mcp__composio__HUBSPOT_CREATE_CONTACT", None) is True
    assert changes_something("mcp__composio__GMAIL_FETCH_EMAILS", None) is False
    assert changes_something("mcp__composio__GOOGLECALENDAR_LIST_EVENTS", None) is False


def test_camel_case_verbs():
    assert changes_something("mcp__crm__sendInvoice", None) is True
    assert changes_something("mcp__crm__getCustomer", None) is False


def test_unknown_is_none():
    assert changes_something("mcp__x__frobnicate", None) is None


def test_is_simulated():
    assert is_simulated({"simulation_run_id": "r1"})
    # A sub-agent keeps its parent's boundary but not the run id.
    assert is_simulated({"memory_boundary": "simulation:r1:whatsapp:+40"})
    assert not is_simulated({"memory_boundary": "whatsapp:+40"})
    assert not is_simulated(None)


async def test_decide_asks_the_llm_once_per_tool():
    judge = AsyncMock(return_value=False)
    assert await decide("mcp__x__frobnicate", None, llm=judge) is False
    assert await decide("mcp__x__frobnicate", None, llm=judge) is False
    judge.assert_awaited_once()


async def test_decide_fakes_when_the_llm_fails():
    judge = AsyncMock(side_effect=RuntimeError("down"))
    assert await decide("mcp__x__frobnicate", None, llm=judge) is True
    assert await decide("mcp__x__frobnicate", None, llm=None) is True


async def test_decide_skips_the_llm_when_known():
    judge = AsyncMock()
    assert await decide("mcp__composio__GMAIL_SEND_EMAIL", None, llm=judge) is True
    judge.assert_not_awaited()


async def test_router_execute_is_judged_by_the_tools_it_runs():
    name = "mcp__tool_router__MULTI_EXECUTE_TOOL"
    fetch = {"tools": [{"tool_slug": "GMAIL_FETCH_EMAILS", "arguments": {}}]}
    send = {"tools": [{"tool_slug": "GMAIL_FETCH_EMAILS"}, {"tool_slug": "GMAIL_SEND_EMAIL"}]}
    assert await decide(name, None, llm=None, args=fetch) is False
    # A cached read verdict for the router must not let a send through.
    assert await decide(name, None, llm=None, args=send) is True
    assert await decide(name, None, llm=None, args={}) is True
    assert await decide("mcp__tool_router__REMOTE_WORKBENCH", None, llm=AsyncMock(return_value=False)) is True
    assert await decide("mcp__tool_router__SEARCH_TOOLS", None, llm=None) is False


def test_fake_result_and_would_have():
    assert json.loads(fake_result("mcp__a__b", {})) == {"ok": True, "simulated": True, "note": "Done."}
    line = would_have("mcp__composio__GMAIL_SEND_EMAIL", {"body": "x" * 1000})
    assert line.startswith('would have called mcp__composio__GMAIL_SEND_EMAIL with {"body": "xxx')
    assert len(line) <= len("would have called mcp__composio__GMAIL_SEND_EMAIL with ") + 300


async def test_summary_judge_reads_a_structured_answer():
    reply = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"changes": false}'))])
    client = SimpleNamespace(
        base_url="https://example.test/v1",
        chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(return_value=reply))),
    )
    judge = summary_judge(client, "m", {"mcp__x__frobnicate": "Frobnicates a widget."})
    assert await judge("mcp__x__frobnicate") is False
    assert summary_judge(None, "m", {}) is None


def _registry(name: str, handler) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        name,
        ToolSchema(name=name, description=name, parameters={"type": "object", "properties": {}}),
        handler=handler,
        toolset="mcp",
    )
    return registry


async def _run(name: str, config: dict, handler) -> tuple[dict, dict]:
    store = AsyncMock()
    store.emit_event = AsyncMock(side_effect=[1, 2])
    store.advance_harness_cursor = AsyncMock()
    session = SimpleNamespace(id=uuid4(), config={"workspace_path": "", **config}, agent_id="a")
    result = await execute_single_tool(
        {"id": "c1", "function": {"name": name, "arguments": '{"to": "ana@example.com"}'}},
        session=session,
        lease=SimpleNamespace(lease_token=uuid4()),
        store=store,
        tools=_registry(name, handler),
        tenant=SimpleNamespace(asset_root="/tmp/test"),
    )
    emitted = store.emit_event.await_args_list[-1].args
    assert emitted[1] == EventType.TOOL_RESULT
    return result, emitted[2]


async def test_simulation_send_never_reaches_the_mcp_client():
    handler = AsyncMock(return_value='{"sent": true}')
    result, event = await _run("mcp__composio__GMAIL_SEND_EMAIL", {"simulation_run_id": "r1"}, handler)
    handler.assert_not_awaited()
    assert json.loads(result["content"])["simulated"] is True
    assert event["simulated"]["would_have"].startswith("would have called mcp__composio__GMAIL_SEND_EMAIL")


async def test_real_session_sends_for_real():
    handler = AsyncMock(return_value='{"sent": true}')
    result, event = await _run("mcp__composio__GMAIL_SEND_EMAIL", {}, handler)
    handler.assert_awaited_once()
    assert "simulated" not in event
    assert json.loads(result["content"]) == {"sent": True}


async def test_simulation_read_runs_for_real():
    handler = AsyncMock(return_value='{"emails": []}')
    result, event = await _run("mcp__composio__GMAIL_FETCH_EMAILS", {"simulation_run_id": "r1"}, handler)
    handler.assert_awaited_once()
    assert "simulated" not in event


def test_mcp_hints_reach_the_registry_but_not_the_model():
    from mcp.types import Tool, ToolAnnotations

    from surogates.tools.mcp.client import _convert_mcp_schema

    tool = Tool(name="lookup", inputSchema={"type": "object"}, annotations=ToolAnnotations(readOnlyHint=True))
    assert _convert_mcp_schema("crm", tool)["annotations"] == {"readOnlyHint": True}

    registry = ToolRegistry()
    registry.register(
        "mcp__crm__lookup",
        ToolSchema(name="mcp__crm__lookup", description="", parameters={}, annotations={"readOnlyHint": True}),
        handler=AsyncMock(),
    )
    assert "annotations" not in registry.get_schemas()[0]["function"]
