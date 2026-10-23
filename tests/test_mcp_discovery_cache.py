"""MCP tool discovery through the proxy: a phone call's turns reuse their session's tool list instead of asking the
proxy every turn, and a tool whose schema changed is registered anew."""
from uuid import uuid4

import httpx

from surogates.orchestrator.mcp_client import McpProxyClient
from surogates.tools.registry import ToolRegistry


def _client(posts: list) -> McpProxyClient:
    def handler(request: httpx.Request) -> httpx.Response:
        posts.append(request.url.path)
        return httpx.Response(200, json={"tools": [{"name": "mcp__crm__lookup", "description": "", "parameters": {}}]})

    client = McpProxyClient("http://proxy", ToolRegistry())
    client._client = httpx.AsyncClient(base_url="http://proxy", transport=httpx.MockTransport(handler))
    return client


async def test_a_session_with_a_cache_ttl_asks_the_proxy_once(monkeypatch):
    monkeypatch.setenv("SUROGATES_JWT_SECRET", "test-secret-for-sandbox-tokens-32b")
    posts: list = []
    client, ids = _client(posts), dict(org_id=uuid4(), user_id=uuid4(), session_id=uuid4())
    first = await client.discover_and_register(**ids, agent_id="a", cache_ttl=300)
    again = await client.discover_and_register(**ids, agent_id="a", cache_ttl=300)
    assert first == again == ["mcp__crm__lookup"] and len(posts) == 1


async def test_a_changed_tool_schema_replaces_the_registered_one(monkeypatch):
    monkeypatch.setenv("SUROGATES_JWT_SECRET", "test-secret-for-sandbox-tokens-32b")
    params = {"properties": {"source_id": {"type": "integer"}}}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"tools": [{"name": "mcp__bid__upsert", "description": "", "parameters": params}]})

    registry = ToolRegistry()
    client = McpProxyClient("http://proxy", registry)
    client._client = httpx.AsyncClient(base_url="http://proxy", transport=httpx.MockTransport(handler))
    ids = dict(org_id=uuid4(), user_id=uuid4(), agent_id="a")
    await client.discover_and_register(**ids, session_id=uuid4())
    params = {"properties": {"source_ids": {"type": "array"}}}  # the MCP server redeployed with new arguments
    await client.discover_and_register(**ids, session_id=uuid4())
    assert registry.get("mcp__bid__upsert").schema.parameters == params


async def test_without_a_ttl_every_wake_still_asks(monkeypatch):
    monkeypatch.setenv("SUROGATES_JWT_SECRET", "test-secret-for-sandbox-tokens-32b")
    posts: list = []
    client, ids = _client(posts), dict(org_id=uuid4(), user_id=uuid4(), session_id=uuid4())
    await client.discover_and_register(**ids, agent_id="a")
    await client.discover_and_register(**ids, agent_id="a")
    assert len(posts) == 2
