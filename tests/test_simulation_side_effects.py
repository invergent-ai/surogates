"""A simulated conversation runs tools that only read, and fakes the ones that change things."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI

from surogates.harness.tool_exec import execute_single_tool
from surogates.mcp_proxy import routes as proxy_routes
from surogates.mcp_proxy.auth import get_proxy_auth
from surogates.orchestrator.mcp_client import McpProxyClient
from surogates.runtime import agent_runtime_context_dep, rate_limit_dep
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
from surogates.tenant.auth.jwt import create_sandbox_token
from surogates.tools.builtin.expert_service import _RegistryOnlyToolRouter
from surogates.tools.registry import ToolRegistry, ToolSchema

SEND = "mcp__composio__GMAIL_SEND_EMAIL"
FETCH = "mcp__composio__GMAIL_FETCH_EMAILS"


@pytest.fixture(autouse=True)
def _fresh_cache():
    side_effects._decided.clear()
    proxy_routes._simulated_sessions.clear()
    yield
    side_effects._decided.clear()
    proxy_routes._simulated_sessions.clear()


def test_annotations_win():
    assert changes_something("mcp__crm__lookup", {"readOnlyHint": True}) is False
    assert changes_something("mcp__crm__lookup", {"destructiveHint": True}) is True


def test_composio_verbs():
    assert changes_something(SEND, None) is True
    assert changes_something("mcp__composio__HUBSPOT_CREATE_CONTACT", None) is True
    assert changes_something(FETCH, None) is False
    assert changes_something("mcp__composio__GOOGLECALENDAR_LIST_EVENTS", None) is False
    assert changes_something("mcp__posthog__POSTHOG_CREATE_QUERY_IN_PROJECT_BY_ID", None) is True


@pytest.mark.parametrize("tool", [
    "mcp__crm__getOrCreateUser",
    "mcp__composio__GMAIL_MARK_AS_READ",
    "mcp__composio__SLACK_SEARCH_AND_DELETE",
    "mcp__composio__HUBSPOT_LIST_AND_ARCHIVE",
    "mcp__composio__GOOGLEDOCS_GET_AND_REPLACE",
    "mcp__crm__sendInvoice",
])
def test_a_write_word_anywhere_makes_a_write(tool):
    assert changes_something(tool, None) is True


async def test_check_in_is_not_a_read():
    assert changes_something("mcp__hotel__check_in_guest", None) is not False
    assert await decide("mcp__hotel__check_in_guest", None, llm=None) is True


def test_unknown_is_none():
    assert changes_something("mcp__x__frobnicate", None) is None


def test_is_simulated():
    assert is_simulated({"simulation_run_id": "r1"})
    # A sub-agent keeps its parent's boundary but not the run id.
    assert is_simulated({"memory_boundary": "simulation:r1:whatsapp:+40"})
    assert not is_simulated({"memory_boundary": "whatsapp:+40"})
    assert not is_simulated(None)


async def test_decide_asks_the_llm_once_per_tool_and_description():
    judge = AsyncMock(return_value=False)
    assert await decide("mcp__x__frobnicate", None, llm=judge, description="a") is False
    assert await decide("mcp__x__frobnicate", None, llm=judge, description="a") is False
    judge.assert_awaited_once_with("mcp__x__frobnicate", "a")
    # A redeployed server with a new description is judged again.
    judge.return_value = True
    assert await decide("mcp__x__frobnicate", None, llm=judge, description="b") is True


async def test_decide_fakes_when_the_llm_fails_and_remembers_for_a_while():
    judge = AsyncMock(side_effect=RuntimeError("down"))
    assert await decide("mcp__x__frobnicate", None, llm=judge) is True
    assert await decide("mcp__x__frobnicate", None, llm=judge) is True
    judge.assert_awaited_once()
    assert await decide("mcp__x__other", None, llm=None) is True


async def test_decide_skips_the_llm_when_known():
    judge = AsyncMock()
    assert await decide(SEND, None, llm=judge) is True
    judge.assert_not_awaited()


async def test_router_execute_is_judged_by_the_tools_it_runs():
    name = "mcp__tool_router__MULTI_EXECUTE_TOOL"
    fetch = {"tools": [{"tool_slug": "GMAIL_FETCH_EMAILS", "arguments": {}}]}
    send = {"tools": [{"tool_slug": "GMAIL_FETCH_EMAILS"}, {"tool_slug": "GMAIL_SEND_EMAIL"}]}
    assert await decide(name, None, llm=None, args=fetch) is False
    # A read verdict for the router must not let a send through.
    assert await decide(name, None, llm=None, args=send) is True
    assert await decide(name, None, llm=None, args={}) is True
    assert await decide("mcp__tool_router__REMOTE_WORKBENCH", None, llm=AsyncMock(return_value=False)) is True
    assert await decide("mcp__tool_router__SEARCH_TOOLS", None, llm=None) is False


def test_fake_result_looks_real_and_would_have_is_clipped():
    assert json.loads(fake_result("mcp__a__b", {})) == {"ok": True}
    routed = json.loads(fake_result(
        "mcp__tool_router__MULTI_EXECUTE_TOOL", {"tools": [{"tool_slug": "GMAIL_SEND_EMAIL"}]},
    ))
    assert routed == {
        "data": {"results": [{"tool_slug": "GMAIL_SEND_EMAIL", "successful": True, "data": {}}]},
        "successful": True,
    }
    line = would_have(SEND, {"body": "x" * 1000})
    assert line.startswith(f'would have called {SEND} with {{"body": "xxx')
    assert len(line) <= len(f"would have called {SEND} with ") + 300


async def test_summary_judge_reads_a_structured_answer():
    reply = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"changes": false}'))])
    client = SimpleNamespace(
        base_url="https://example.test/v1",
        chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(return_value=reply))),
    )
    judge = summary_judge(client, "m")
    assert await judge("mcp__x__frobnicate", "Frobnicates a widget.") is False
    assert summary_judge(None, "m") is None


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


# --- the harness: the trace says what would have happened -----------------


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
    result, event = await _run(SEND, {"simulation_run_id": "r1"}, handler)
    handler.assert_not_awaited()
    assert json.loads(result["content"]) == {"ok": True}
    assert "simulat" not in result["content"]
    assert event["simulated"]["would_have"].startswith(f"would have called {SEND}")


async def test_real_session_sends_for_real():
    handler = AsyncMock(return_value='{"sent": true}')
    result, event = await _run(SEND, {}, handler)
    handler.assert_awaited_once()
    assert "simulation_read" not in handler.await_args.kwargs
    assert "simulated" not in event
    assert json.loads(result["content"]) == {"sent": True}


async def test_simulation_read_runs_for_real_and_says_so_to_the_proxy():
    handler = AsyncMock(return_value='{"emails": []}')
    result, event = await _run(FETCH, {"simulation_run_id": "r1"}, handler)
    handler.assert_awaited_once()
    assert handler.await_args.kwargs["simulation_read"] is True
    assert "simulated" not in event


# --- the proxy: every caller passes here ------------------------------------

ORG, USER = UUID(int=1), UUID(int=2)
SIM_SESSION, REAL_SESSION = UUID(int=3), UUID(int=4)
CONFIGS = {
    SIM_SESSION: {"memory_boundary": "simulation:r1:whatsapp:+40"},
    REAL_SESSION: {"memory_boundary": "whatsapp:+40"},
}


class _Db:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def scalar(self, stmt):
        wanted = stmt.whereclause.right.value
        return CONFIGS.get(wanted)


@pytest.fixture
def proxy(monkeypatch):
    monkeypatch.setenv("SUROGATES_JWT_SECRET", "test-secret")
    upstream = AsyncMock(return_value=('{"sent": true}', "success"))
    monkeypatch.setattr(proxy_routes, "_execute_call", upstream)
    monkeypatch.setattr(
        proxy_routes, "_ensure_tenant_connected",
        AsyncMock(return_value=[{"name": SEND}, {"name": "mcp__x__frobnicate"}]),
    )
    pool = SimpleNamespace(resolve_call_target=lambda *a: ("srv", "tool", {}))
    app = FastAPI()
    app.include_router(proxy_routes.router)
    app.state.pool = pool
    app.state.session_factory = _Db
    app.dependency_overrides[agent_runtime_context_dep] = lambda: SimpleNamespace(
        agent_id="agent-A", mcp_server_ids=("s1",),
    )
    app.dependency_overrides[rate_limit_dep] = lambda: None
    assert get_proxy_auth not in app.dependency_overrides  # the real signed token is read
    return app, upstream


def _http(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://proxy")


async def _call(app, session_id: UUID, name: str, **claims) -> dict:
    token = create_sandbox_token(ORG, USER, session_id, agent_id="agent-A", **claims)
    async with _http(app) as client:
        resp = await client.post(
            "/mcp/v1/tools/call", json={"name": name, "arguments": {"to": "a"}},
            headers={"Authorization": f"Bearer {token}"}, params={"agent_id": "agent-A"},
        )
    assert resp.status_code == 200
    return resp.json()


async def test_a_sandbox_script_in_a_simulation_never_reaches_upstream(proxy):
    app, upstream = proxy
    assert json.loads((await _call(app, SIM_SESSION, SEND))["result"]) == {"ok": True}
    upstream.assert_not_awaited()


async def test_a_real_session_reaches_upstream(proxy):
    app, upstream = proxy
    assert json.loads((await _call(app, REAL_SESSION, SEND))["result"]) == {"sent": True}
    upstream.assert_awaited_once()


async def test_an_unknown_session_is_not_simulated(proxy):
    app, upstream = proxy
    await _call(app, UUID(int=99), SEND)
    upstream.assert_awaited_once()


async def test_the_proxy_fakes_unknown_tools_unless_the_harness_judged_a_read(proxy):
    app, upstream = proxy
    await _call(app, SIM_SESSION, "mcp__x__frobnicate")
    upstream.assert_not_awaited()
    await _call(app, SIM_SESSION, "mcp__x__frobnicate", simulation_read="mcp__x__frobnicate")
    upstream.assert_awaited_once()
    # A read claim for one tool lets no other through.
    await _call(app, SIM_SESSION, SEND, simulation_read="mcp__x__frobnicate")
    upstream.assert_awaited_once()


async def test_an_expert_in_a_simulation_never_reaches_upstream(proxy):
    app, upstream = proxy
    registry = ToolRegistry()
    mcp = McpProxyClient("http://proxy", registry)
    mcp._client = _http(app)
    registry.register(
        SEND, ToolSchema(name=SEND, description="", parameters={}),
        handler=mcp._make_proxy_handler(SEND), toolset="mcp",
    )
    tenant = SimpleNamespace(org_id=ORG, user_id=USER, service_account_id=None)
    try:
        # The expert loop's own path: tool_router.execute -> registry.dispatch, no harness guard.
        result = await _RegistryOnlyToolRouter(registry).execute(
            name=SEND, arguments={"to": "a"}, tenant=tenant, session_id=SIM_SESSION,
        )
    finally:
        await mcp.close()
    assert json.loads(result) == {"ok": True}
    upstream.assert_not_awaited()
