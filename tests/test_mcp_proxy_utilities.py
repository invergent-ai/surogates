"""The MCP proxy: a server's resource / prompt tools are listed only when it has resources /
prompts (unless a session asks for all), named by the server's own name, and called as the MCP
methods they stand for (resources/read, prompts/get), never as tools/call."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from surogates.mcp_proxy import pool as pool_module
from surogates.mcp_proxy import routes as routes_module
from surogates.mcp_proxy.pool import ConnectionPool
from surogates.orchestrator.mcp_client import McpProxyClient, utility_tools_mode
from surogates.tools.mcp import client as mcp_client
from surogates.tools.mcp.client import MCPServerTask
from surogates.tools.registry import ToolRegistry

pytestmark = pytest.mark.asyncio


class Session:
    """An MCP client session: what was asked of it, and what it has."""

    def __init__(self) -> None:
        self.asked: list[tuple] = []

    async def call_tool(self, name, arguments=None, meta=None):
        self.asked.append(("tools/call", name))
        return SimpleNamespace(isError=False, content=[SimpleNamespace(text=f"ran {name}")])

    async def list_resources(self):
        self.asked.append(("resources/list",))
        return SimpleNamespace(resources=[SimpleNamespace(uri="rules://world", name="rules", description=None, mimeType=None)])

    async def read_resource(self, uri):
        self.asked.append(("resources/read", uri))
        return SimpleNamespace(contents=[SimpleNamespace(text="the rules")])

    async def list_prompts(self):
        self.asked.append(("prompts/list",))
        return SimpleNamespace(prompts=[])

    async def get_prompt(self, name, arguments=None):
        self.asked.append(("prompts/get", name))
        return SimpleNamespace(messages=[SimpleNamespace(role="user", content=SimpleNamespace(text="brief me"))],
                               description=None)


SERVERS = {
    # name: (what it offers, its own tools)
    "world": ({"resources": False, "prompts": False}, ["lookup"]),
    "docs": ({"resources": True, "prompts": False}, ["search"]),
}


@pytest.fixture
def connected(monkeypatch):
    """A pool whose discovery connects fake servers, as discover_mcp_tools would."""
    made: list[str] = []
    sessions: dict[str, Session] = {}

    def fake_discover(servers, registry):
        names = []
        for key, config in servers.items():
            plain = key.rsplit("__", 1)[-1]
            offers, tools = SERVERS[plain]
            server = MCPServerTask(key)
            server.session = sessions.setdefault(plain, Session())
            server.offers = dict(offers)
            server._tools = [SimpleNamespace(name=t, description=None, inputSchema={"type": "object", "properties": {}})
                             for t in tools]
            registered = mcp_client._register_server_tools(key, server, config, registry)
            server._registered_tool_names = registered
            with mcp_client._lock:
                mcp_client._servers[key] = server
            made.append(key)
            names += registered
        return names

    monkeypatch.setattr(pool_module, "discover_mcp_tools", fake_discover)
    yield sessions
    with mcp_client._lock:
        for key in made:
            mcp_client._servers.pop(key, None)


async def _connect(pool):
    org, user, agent = uuid4(), uuid4(), "agent-1"
    schemas = await pool.ensure_connected(org, user, agent, {name: {"url": f"http://{name}"} for name in SERVERS})
    return (org, user, agent), schemas


async def test_a_session_sees_the_resource_tools_only_of_a_server_that_has_resources(connected):
    pool = ConnectionPool()
    tenant, schemas = await _connect(pool)
    seen = {s["name"] for s in pool.visible_schemas(*tenant, schemas)}
    assert seen == {
        "mcp__world__lookup", "mcp__docs__search",
        "mcp__docs__list_resources", "mcp__docs__read_resource",
    }


async def test_a_session_that_asks_for_all_sees_them_for_every_server_as_before(connected):
    pool = ConnectionPool()
    tenant, schemas = await _connect(pool)
    seen = {s["name"] for s in pool.visible_schemas(*tenant, schemas, utility_tools="all")}
    for server in SERVERS:
        assert {f"mcp__{server}__{u}" for u in ("list_resources", "read_resource", "list_prompts", "get_prompt")} <= seen


async def test_descriptions_name_the_server_not_its_tenant_key(connected):
    pool = ConnectionPool()
    (org, user, agent), schemas = await _connect(pool)
    for schema in schemas:
        assert str(org) not in schema["description"] and str(user) not in schema["description"], schema
    read = next(s for s in schemas if s["name"] == "mcp__docs__read_resource")
    assert read["description"] == "Read a resource by URI from MCP server 'docs'"
    lookup = next(s for s in schemas if s["name"] == "mcp__world__lookup")
    assert lookup["description"] == "MCP tool lookup from world"


async def test_a_reconnect_that_finds_resources_shows_their_tools(connected):
    """Visibility follows the connected server's offers, read anew at each connection."""
    pool = ConnectionPool()
    tenant, schemas = await _connect(pool)
    key = next(k for k in mcp_client._servers if k.endswith("__world"))
    mcp_client._servers[key].offers = {"resources": True, "prompts": False}
    seen = {s["name"] for s in pool.visible_schemas(*tenant, schemas)}
    assert {"mcp__world__list_resources", "mcp__world__read_resource"} <= seen


async def test_a_resource_is_read_with_resources_read_not_tools_call(connected):
    pool = ConnectionPool()
    tenant, _ = await _connect(pool)
    assert pool.utility_of(*tenant, "mcp__docs__read_resource") == "read_resource"
    assert pool.utility_of(*tenant, "mcp__world__lookup") is None
    answer = await pool.call_tool(*tenant, "mcp__docs__read_resource", {"uri": "rules://world"})
    assert json.loads(answer) == {"result": "the rules"}
    assert connected["docs"].asked == [("resources/read", "rules://world")]
    # A server's own tool still goes through tools/call.
    assert await pool.call_tool(*tenant, "mcp__world__lookup", {}) == "ran lookup"
    assert connected["world"].asked == [("tools/call", "lookup")]


async def test_a_stdio_servers_prompt_is_got_with_prompts_get_in_its_call_sandbox(monkeypatch):
    session = Session()

    class Sandbox:
        def __init__(self, **kwargs):
            pass

        @asynccontextmanager
        async def mcp_session(self):
            yield session

    monkeypatch.setattr(routes_module, "MCPCallSandbox", Sandbox)
    pool = SimpleNamespace(utility_of=lambda *args: "get_prompt")
    text, outcome = await routes_module._execute_call(
        pool=pool, org_id=uuid4(), user_id=uuid4(), agent_id="a",
        server_config={"transport": "stdio", "command": "srv"},
        clean_tool_name="mcp__docs__get_prompt", original_tool="get_prompt",
        arguments={"name": "brief"}, meta=None,
    )
    assert session.asked == [("prompts/get", "brief")]
    assert json.loads(text)["messages"] == [{"role": "user", "content": "brief me"}]
    assert outcome == "success"


async def test_a_stdio_servers_error_answer_is_the_tools_error_not_a_transport_failure(monkeypatch):
    class Unknown(Session):
        async def read_resource(self, uri):
            raise RuntimeError("Unknown resource: ws_rules.md")

    class Sandbox:
        def __init__(self, **kwargs):
            pass

        @asynccontextmanager
        async def mcp_session(self):
            yield Unknown()

    monkeypatch.setattr(routes_module, "MCPCallSandbox", Sandbox)
    text, outcome = await routes_module._execute_call(
        pool=SimpleNamespace(utility_of=lambda *args: "read_resource"), org_id=uuid4(), user_id=uuid4(),
        agent_id="a", server_config={"transport": "stdio", "command": "srv"},
        clean_tool_name="mcp__docs__read_resource", original_tool="read_resource",
        arguments={"uri": "ws_rules.md"}, meta=None,
    )
    assert outcome == "tool_error" and "Unknown resource" in json.loads(text)["error"]


@pytest.mark.parametrize("config, mode", [(None, "offered"), ({}, "offered"), ({"mcp_utility_tools": "all"}, "all"),
                                          ({"mcp_utility_tools": "everything"}, "offered")])
def test_a_sessions_config_picks_the_mode(config, mode):
    assert utility_tools_mode(config) == mode


@pytest.mark.parametrize("mode, sent", [("offered", None), ("all", "all")])
async def test_the_worker_asks_the_proxy_for_what_its_session_wants(monkeypatch, mode, sent):
    monkeypatch.setenv("SUROGATES_JWT_SECRET", "test-secret-for-sandbox-tokens-32b")
    asked: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(request.url.params.get("utility_tools"))
        return httpx.Response(200, json={"tools": []})

    client = McpProxyClient("http://proxy", ToolRegistry())
    client._client = httpx.AsyncClient(base_url="http://proxy", transport=httpx.MockTransport(handler))
    await client.discover_and_register(uuid4(), uuid4(), uuid4(), agent_id="a", utility_tools=mode)
    assert asked == [sent]
