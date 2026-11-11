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
    yield
    side_effects._decided.clear()


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


# --- the proxy: every caller passes here, told by a signed claim ------------

ORG, USER, SESSION = UUID(int=1), UUID(int=2), UUID(int=3)
SIM_CONFIG = {"memory_boundary": "simulation:r1:whatsapp:+40"}


@pytest.fixture
def proxy(monkeypatch):
    monkeypatch.setenv("SUROGATES_JWT_SECRET", "test-secret")
    upstream = AsyncMock(return_value=('{"sent": true}', "success"))
    monkeypatch.setattr(proxy_routes, "_execute_call", upstream)
    monkeypatch.setattr(
        proxy_routes, "_ensure_tenant_connected",
        AsyncMock(return_value=[{"name": SEND}, {"name": "mcp__x__frobnicate"}]),
    )
    app = FastAPI()
    app.include_router(proxy_routes.router)
    app.state.pool = SimpleNamespace(resolve_call_target=lambda *a: ("srv", "tool", {}))
    app.state.session_factory = None  # the claim decides: no database is read
    app.state.audit_store = SimpleNamespace(emit=AsyncMock())
    app.dependency_overrides[agent_runtime_context_dep] = lambda: SimpleNamespace(
        agent_id="agent-A", mcp_server_ids=("s1",),
    )
    app.dependency_overrides[rate_limit_dep] = lambda: None
    return app, upstream


def _http(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://proxy")


async def _call(app, name: str, **claims) -> dict:
    token = create_sandbox_token(ORG, USER, SESSION, agent_id="agent-A", **claims)
    async with _http(app) as client:
        resp = await client.post(
            "/mcp/v1/tools/call", json={"name": name, "arguments": {"to": "a"}},
            headers={"Authorization": f"Bearer {token}"}, params={"agent_id": "agent-A"},
        )
    assert resp.status_code == 200
    return resp.json()


async def test_a_simulated_call_never_reaches_upstream_and_is_audited(proxy):
    app, upstream = proxy
    assert json.loads((await _call(app, SEND, simulated=True))["result"]) == {"ok": True}
    upstream.assert_not_awaited()
    data = app.state.audit_store.emit.await_args.kwargs["data"]
    assert data["outcome"] == "simulated"
    assert data["would_have"] == would_have(SEND, {"to": "a"})


async def test_a_token_without_the_claim_reaches_upstream(proxy):
    app, upstream = proxy
    assert json.loads((await _call(app, SEND))["result"]) == {"sent": True}
    upstream.assert_awaited_once()
    assert app.state.audit_store.emit.await_args.kwargs["data"]["outcome"] == "success"


async def test_the_proxy_fakes_unknown_tools_unless_the_harness_judged_a_read(proxy):
    app, upstream = proxy
    await _call(app, "mcp__x__frobnicate", simulated=True)
    upstream.assert_not_awaited()
    await _call(app, "mcp__x__frobnicate", simulated=True, simulation_read="mcp__x__frobnicate")
    upstream.assert_awaited_once()
    # A read claim for one tool lets no other through.
    await _call(app, SEND, simulated=True, simulation_read="mcp__x__frobnicate")
    upstream.assert_awaited_once()


def _proxy_client(app) -> tuple[ToolRegistry, McpProxyClient]:
    registry = ToolRegistry()
    mcp = McpProxyClient("http://proxy", registry)
    mcp._client = _http(app)
    for name in (SEND, FETCH):
        registry.register(
            name, ToolSchema(name=name, description="", parameters={}),
            handler=mcp._make_proxy_handler(name), toolset="mcp",
        )
    return registry, mcp


TENANT = SimpleNamespace(org_id=ORG, user_id=USER, service_account_id=None)


async def test_an_expert_in_a_simulation_never_reaches_upstream(proxy):
    app, upstream = proxy
    registry, mcp = _proxy_client(app)
    try:
        # The expert loop's own path: tool_router.execute -> registry.dispatch, no harness guard.
        result = await _RegistryOnlyToolRouter(registry).execute(
            name=SEND, arguments={"to": "a"}, tenant=TENANT, session_id=SESSION, session_config=SIM_CONFIG,
        )
    finally:
        await mcp.close()
    assert json.loads(result) == {"ok": True}
    upstream.assert_not_awaited()


async def test_the_worker_signs_the_simulation_into_a_short_token(proxy, monkeypatch):
    app, _ = proxy
    minted: list[dict] = []
    real = create_sandbox_token

    def spy(*args, **kwargs):
        minted.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr("surogates.orchestrator.mcp_client.create_sandbox_token", spy)
    registry, mcp = _proxy_client(app)
    try:
        await registry.dispatch(FETCH, {}, tenant=TENANT, session_id=SESSION, session_config=SIM_CONFIG,
                                simulation_read=True)
        await registry.dispatch(FETCH, {}, tenant=TENANT, session_id=SESSION, session_config={},
                                simulation_read=True)
    finally:
        await mcp.close()
    assert minted[0]["simulated"] is True and minted[0]["simulation_read"] == FETCH
    assert minted[0]["expires_minutes"] == 2
    assert minted[1]["simulated"] is False and minted[1]["simulation_read"] is None


def _claims(token: str) -> dict:
    from surogates.tenant.auth.jwt import decode_token

    return decode_token(token)


def test_sandbox_tokens_carry_the_simulation(monkeypatch):
    from surogates.sandbox.base import SandboxSpec
    from surogates.sandbox.docker import DockerSandbox
    from surogates.sandbox.kubernetes import K8sSandbox

    monkeypatch.setenv("SUROGATES_JWT_SECRET", "test-secret")
    for simulated in (True, False):
        spec = SandboxSpec(env={"ORG_ID": str(ORG), "USER_ID": str(USER)}, simulated=simulated)
        docker_token = DockerSandbox._mint_mcp_token(SimpleNamespace(), spec, uuid4().hex)
        assert _claims(docker_token).get("simulated", False) is simulated

        pod = K8sSandbox(mcp_proxy_url="http://proxy")._build_pod_manifest(
            uuid4().hex, "pod", "secret", spec, executor_token="t",
        )
        env = {e.name: e.value for e in pod.spec.containers[0].env}
        assert _claims(env["MCP_PROXY_TOKEN"]).get("simulated", False) is simulated
        # The pod's own environment must not tell the agent it is being tested.
        assert not any("SIMULAT" in name for name in env)


async def test_the_sandbox_spec_marks_a_simulated_session():
    from surogates.harness.tool_exec import _build_session_sandbox_spec

    for config, simulated in ((SIM_CONFIG, True), ({}, False)):
        session = SimpleNamespace(id=uuid4(), config={"workspace_path": "", **config}, agent_id="a")
        tenant = SimpleNamespace(org_id=ORG, user_id=USER, asset_root="/tmp/test")
        spec = await _build_session_sandbox_spec(session, tenant, str(session.id))
        assert spec.simulated is simulated
