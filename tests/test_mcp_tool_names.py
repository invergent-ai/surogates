"""MCP tool names an engine accepts: ``^[A-Za-z0-9_.-]{1,64}$``.

OpenAI-compatible engines refuse a whole request when one tool name is
longer than 64 characters or holds another character, so one long server
and tool name broke every model call of a session (bench finding F25).
A name that fits is unchanged; one that does not is shortened, the same way
in every wake, and calls under it reach the right server and tool, in
process and through the MCP proxy.
"""

from __future__ import annotations

import asyncio
import random
import string
from types import SimpleNamespace
from uuid import uuid4

import pytest

from surogates.harness.message_utils import neutralize_unsendable_tool_names
from surogates.mcp_proxy import pool as pool_module
from surogates.mcp_proxy.pool import ConnectionPool
from surogates.tools.mcp import client as mcp_client
from surogates.tools.mcp.client import MCPServerTask, _register_server_tools
from surogates.tools.mcp.names import TOOL_NAME_RE, mcp_tool_name, mcp_tool_server, mcp_toolset
from surogates.tools.registry import ToolRegistry

LONG_SERVER = "acme-enterprise-customer-relationship-management-production"
LONG_TOOL = "get_customer_account_details_with_full_history"


# ---------------------------------------------------------------------------
# The names
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("server, tool, name", [
    ("github", "create_issue", "mcp__github__create_issue"),
    ("github-mcp", "list.repos", "mcp__github_mcp__list_repos"),
    ("world", "rss list feeds", "mcp__world__rss_list_feeds"),
    ("composio-tool-router", "COMPOSIO_SEARCH_TOOLS", "mcp__composio_tool_router__COMPOSIO_SEARCH_TOOLS"),
    ("s" * 25, "t" * 32, f"mcp__{'s' * 25}__{'t' * 32}"),  # 64 exactly
])
def test_a_name_that_fits_is_unchanged(server, tool, name):
    assert mcp_tool_name(server, tool) == name


def test_a_long_name_keeps_the_tool_and_what_fits_of_the_server():
    name = mcp_tool_name(LONG_SERVER, LONG_TOOL)
    assert TOOL_NAME_RE.match(name) and len(name) <= 64
    assert name.startswith("mcp__acme_ent")
    server_part, rest = name[len("mcp__"):].split("__", 1)
    assert rest.startswith(LONG_TOOL[: len(rest) - 9]) and len(rest) - 9 >= 30
    assert "acme_enterprise_customer_relationship_management_production".startswith(server_part)
    assert mcp_tool_name(LONG_SERVER, LONG_TOOL) == name  # the same in every wake


def test_two_long_names_that_would_read_alike_differ():
    a = mcp_tool_name(LONG_SERVER, LONG_TOOL)
    b = mcp_tool_name(LONG_SERVER + "-staging", LONG_TOOL)  # the same stem once cut
    c = mcp_tool_name(LONG_SERVER, LONG_TOOL + "_and_more_than_fits_in_the_room_left")
    d = mcp_tool_name(LONG_SERVER, LONG_TOOL + "_and_more_than_fits_in_the_room_right")
    assert a[:-8] == b[:-8] and a != b
    assert c[:-8] == d[:-8] and c != d
    assert len({a, b, c, d}) == 4


def test_every_name_is_one_an_engine_accepts():
    rng = random.Random(25)
    alphabet = string.ascii_letters + string.digits + "-_. /:é漢"
    for _ in range(2000):
        server = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 120)))
        tool = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 200)))
        assert TOOL_NAME_RE.match(mcp_tool_name(server, tool)), (server, tool)


def test_the_server_of_a_shortened_name_comes_from_its_toolset():
    name = mcp_tool_name(LONG_SERVER, LONG_TOOL)
    assert mcp_tool_server(name, mcp_toolset(LONG_SERVER)) == "acme_enterprise_customer_relationship_management_production"
    assert mcp_tool_server("mcp__github__create_issue") == "github"
    assert mcp_tool_server("read_file") == ""


# ---------------------------------------------------------------------------
# In process: registered, called, and named the same after a replay
# ---------------------------------------------------------------------------


class _Session:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def call_tool(self, name, arguments=None, meta=None):
        self.calls.append((name, arguments))
        return SimpleNamespace(isError=False, content=[SimpleNamespace(text=f"ran {name}")])


def _server(key: str, tools: list[str]) -> MCPServerTask:
    server = MCPServerTask(key)
    server.session = _Session()
    server._tools = [SimpleNamespace(name=t, description=f"{t}.", inputSchema={"type": "object", "properties": {}})
                     for t in tools]
    return server


@pytest.fixture
def run_inline(monkeypatch):
    def run(coro, **_kwargs):
        return asyncio.new_event_loop().run_until_complete(coro)

    monkeypatch.setattr(mcp_client, "_run_on_mcp_loop", run)


def _wake(server: MCPServerTask, monkeypatch) -> ToolRegistry:
    registry = ToolRegistry()
    monkeypatch.setitem(mcp_client._servers, server.name, server)
    server._registered_tool_names = _register_server_tools(server.name, server, {}, registry)
    return registry


def test_a_shortened_tool_is_called_as_itself_on_its_server(monkeypatch, run_inline):
    server = _server(LONG_SERVER, [LONG_TOOL, "get-user", "ping"])
    registry = _wake(server, monkeypatch)
    names = set(registry.tool_names)
    long_name = mcp_tool_name(LONG_SERVER, LONG_TOOL)
    assert long_name in names and all(TOOL_NAME_RE.match(n) for n in names)
    assert registry.get(long_name).toolset == mcp_toolset(LONG_SERVER)
    registry.get(long_name).handler({"id": "7"})
    registry.get(mcp_tool_name(LONG_SERVER, "get-user")).handler({})
    assert server.session.calls == [(LONG_TOOL, {"id": "7"}), ("get-user", {})]


def test_a_call_in_the_history_still_routes_after_a_replay(monkeypatch, run_inline):
    first = _wake(_server(LONG_SERVER, [LONG_TOOL]), monkeypatch)
    long_name = mcp_tool_name(LONG_SERVER, LONG_TOOL)
    history = [{"id": "c1", "type": "function", "function": {"name": long_name, "arguments": "{}"}}]
    # The next wake (after a compaction, a restart, another worker) registers anew.
    server = _server(LONG_SERVER, [LONG_TOOL])
    second = _wake(server, monkeypatch)
    assert set(second.tool_names) == set(first.tool_names)
    assert neutralize_unsendable_tool_names(history, set(second.tool_names)) == history
    second.get(history[0]["function"]["name"]).handler({})
    assert server.session.calls == [(LONG_TOOL, {})]


def test_the_entitlement_filter_reads_a_shortened_tools_server(monkeypatch, run_inline):
    from surogates.orchestrator.worker import _entitlement_tool_exclusions

    registry = _wake(_server(LONG_SERVER, [LONG_TOOL]), monkeypatch)
    long_name = mcp_tool_name(LONG_SERVER, LONG_TOOL)
    from surogates.runtime.entitlements import ENTITLEMENTS_CONFIG_KEY

    config = {ENTITLEMENTS_CONFIG_KEY: {"mcp_server_ids": ["srv-1"]}}
    allowed = ({"acme_enterprise_customer_relationship_management_production"}, set())
    kept = _entitlement_tool_exclusions(session_config=config, tool_registry=registry, mcp_scope=allowed,
                                        browser_enabled=True)
    assert long_name not in kept
    other = ({"acme_ent"}, set())  # a server whose name is the shortened stem is not this one
    assert long_name in _entitlement_tool_exclusions(session_config=config, tool_registry=registry,
                                                     mcp_scope=other, browser_enabled=True)


# ---------------------------------------------------------------------------
# Through the MCP proxy
# ---------------------------------------------------------------------------


@pytest.fixture
def proxy_world(monkeypatch):
    made: list[str] = []
    sessions: dict[str, _Session] = {}

    def fake_discover(servers, registry):
        names = []
        for key, config in servers.items():
            plain = key.rsplit("__", 1)[-1]
            server = _server(key, [LONG_TOOL, "get-user", "ping"] if plain == LONG_SERVER else ["ping"])
            sessions[plain] = server.session
            server._registered_tool_names = _register_server_tools(key, server, config, registry)
            with mcp_client._lock:
                mcp_client._servers[key] = server
            made.append(key)
            names += server._registered_tool_names
        return names

    monkeypatch.setattr(pool_module, "discover_mcp_tools", fake_discover)
    yield sessions
    with mcp_client._lock:
        for key in made:
            mcp_client._servers.pop(key, None)


@pytest.mark.parametrize("governance", [True, False])
async def test_the_proxy_offers_names_an_engine_accepts_and_routes_them_back(proxy_world, governance):
    pool = ConnectionPool(governance_enabled=governance)
    tenant = (uuid4(), uuid4(), "agent")
    schemas = await pool.ensure_connected(*tenant, {LONG_SERVER: {"url": "http://crm"}, "world": {"url": "http://w"}})
    names = {s["name"]: s for s in schemas}
    assert all(TOOL_NAME_RE.match(n) for n in names)
    assert "mcp__world__ping" in names  # a name that fits is unchanged
    long_name = mcp_tool_name(LONG_SERVER, LONG_TOOL)
    assert long_name in names and names[long_name]["server"] == LONG_SERVER
    # Each name resolves to its server and the tool's own MCP name, hyphens and all.
    for tool in (LONG_TOOL, "get-user", "ping"):
        target = pool.resolve_call_target(*tenant, mcp_tool_name(LONG_SERVER, tool))
        assert target is not None and target[0] == LONG_SERVER and target[1] == tool


async def test_the_proxy_client_registers_the_server_with_each_tool(monkeypatch):
    from surogates.orchestrator.mcp_client import McpProxyClient

    long_name = mcp_tool_name(LONG_SERVER, LONG_TOOL)

    class _Response:
        status_code = 200

        @staticmethod
        def json():
            return {"tools": [
                {"name": long_name, "description": "d", "parameters": {}, "server": LONG_SERVER},
                {"name": "mcp__world__ping", "description": "d", "parameters": {}},
            ]}

    registry = ToolRegistry()
    client = McpProxyClient("http://proxy.invalid", registry)

    async def post(*_args, **_kwargs):
        return _Response()

    monkeypatch.setattr(client._client, "post", post)
    monkeypatch.setattr("surogates.orchestrator.mcp_client.create_sandbox_token", lambda *a, **k: "t")
    await client.discover_and_register(uuid4(), uuid4(), uuid4(), agent_id="agent")
    await client._client.aclose()
    assert mcp_tool_server(long_name, registry.get(long_name).toolset) == (
        "acme_enterprise_customer_relationship_management_production")
    assert registry.get("mcp__world__ping").toolset == "mcp"
    assert mcp_tool_server("mcp__world__ping", "mcp") == "world"
