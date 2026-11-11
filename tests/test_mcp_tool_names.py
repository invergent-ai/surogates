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
from surogates.tools.mcp.names import (
    TOOL_NAME_RE,
    configured_tool_name,
    mcp_tool_name,
    mcp_tool_server,
    mcp_toolset,
)
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
    stem, digest = name.rsplit("-", 1)
    assert len(digest) == 12 and all(c in "0123456789abcdef" for c in digest)
    server_part, tool_part = stem[len("mcp__"):].split("__", 1)
    assert server_part == "acme_enterpr"  # at least 12 characters of the server
    assert LONG_TOOL.startswith(tool_part) and len(tool_part) == 32
    assert mcp_tool_name(LONG_SERVER, LONG_TOOL) == name  # the same in every wake
    # A tool name that fits beside the server is kept whole.
    assert "lookup_order-" in mcp_tool_name("s" * 70, "lookup_order")


def test_no_tool_can_take_a_shortened_name():
    name = mcp_tool_name(LONG_SERVER, LONG_TOOL)
    stem, digest = name.rsplit("-", 1)
    server_part, tool_part = stem[len("mcp__"):].split("__", 1)
    # A server named like the stem, with a tool named like the rest: sanitizing makes "-" a "_".
    assert mcp_tool_name(server_part, f"{tool_part}-{digest}") != name
    assert mcp_tool_name(server_part, f"{tool_part}_{digest}") != name


def test_two_long_names_that_would_read_alike_differ():
    a = mcp_tool_name(LONG_SERVER, LONG_TOOL)
    b = mcp_tool_name(LONG_SERVER + "-staging", LONG_TOOL)  # the same stem once cut
    c = mcp_tool_name(LONG_SERVER, LONG_TOOL + "_and_more_than_fits_in_the_room_left")
    d = mcp_tool_name(LONG_SERVER, LONG_TOOL + "_and_more_than_fits_in_the_room_right")
    assert a[:-13] == b[:-13] and a != b
    assert c[:-13] == d[:-13] and c != d
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
    other = ({"acme_enterpr"}, set())  # a server whose name is the shortened stem is not this one
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


def test_a_rule_written_with_the_full_long_name_applies_to_the_tool():
    from surogates.governance.policy import GovernanceGate

    full = f"mcp__{LONG_SERVER.replace('-', '_')}__{LONG_TOOL}"
    short = mcp_tool_name(LONG_SERVER, LONG_TOOL)
    assert configured_tool_name(full) == short
    assert configured_tool_name("mcp__github__create_issue") == "mcp__github__create_issue"
    assert configured_tool_name("read_file") == "read_file"
    assert not GovernanceGate(denied_tools={full}).check(short, {}).allowed
    assert GovernanceGate(denied_tools={full}).check("mcp__github__create_issue", {}).allowed
    assert GovernanceGate(require_approval={full}).check(short, {}).overridable


async def test_composio_router_tools_keep_their_prefix_and_toolkit(monkeypatch):
    from surogates.mcp_proxy.loader import COMPOSIO_SERVER_NAME

    made: list[str] = []
    tools = ["COMPOSIO_SEARCH_TOOLS", "GOOGLESHEETS_SPREADSHEETS_VALUES_APPEND",
             "GOOGLESHEETS_SPREADSHEETS_VALUES_APPEND_WITH_FORMATTING_OPTIONS"]

    def fake_discover(servers, registry):
        names = []
        for key, config in servers.items():
            server = _server(key, tools)
            server._registered_tool_names = _register_server_tools(key, server, config, registry)
            with mcp_client._lock:
                mcp_client._servers[key] = server
            made.append(key)
            names += server._registered_tool_names
        return names

    monkeypatch.setattr(pool_module, "discover_mcp_tools", fake_discover)
    try:
        pool = ConnectionPool(governance_enabled=True)
        schemas = await pool.ensure_connected(uuid4(), uuid4(), "agent", {COMPOSIO_SERVER_NAME: {"url": "http://c"}})
    finally:
        with mcp_client._lock:
            for key in made:
                mcp_client._servers.pop(key, None)
    names = {s["name"] for s in schemas}
    assert all(TOOL_NAME_RE.match(n) for n in names)
    # Names that fit are as before the change.
    assert {"mcp__tool_router__SEARCH_TOOLS", "mcp__tool_router__GOOGLESHEETS_SPREADSHEETS_VALUES_APPEND"} <= names
    [long_one] = [n for n in names if n.endswith(tuple("0123456789abcdef")) and "-" in n]
    assert long_one.startswith("mcp__tool_router__GOOGLESHEETS_")


async def test_tools_whose_names_sanitize_alike_keep_the_first_in_the_proxy(monkeypatch):
    made: list[str] = []

    def fake_discover(servers, registry):
        names = []
        for key, config in servers.items():
            server = _server(key, ["get-user", "get_user"])
            server._registered_tool_names = _register_server_tools(key, server, config, registry)
            with mcp_client._lock:
                mcp_client._servers[key] = server
            made.append(key)
            names += server._registered_tool_names
        return names

    monkeypatch.setattr(pool_module, "discover_mcp_tools", fake_discover)
    try:
        pool = ConnectionPool(governance_enabled=False)
        tenant = (uuid4(), uuid4(), "agent")
        schemas = await pool.ensure_connected(*tenant, {"world": {"url": "http://w"}})
    finally:
        with mcp_client._lock:
            for key in made:
                mcp_client._servers.pop(key, None)
    assert [s["name"] for s in schemas].count("mcp__world__get_user") == 1
    assert pool.resolve_call_target(*tenant, "mcp__world__get_user")[1] == "get-user"


async def test_the_proxy_route_tells_the_worker_each_tools_server(proxy_world, monkeypatch):
    """End to end: the proxy's list route, the worker's client, and its entitlement filter."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from surogates.mcp_proxy.auth import ProxyAuthContext, get_proxy_auth
    from surogates.mcp_proxy.routes import router
    from surogates.orchestrator.mcp_client import McpProxyClient
    from surogates.orchestrator.worker import _entitlement_tool_exclusions
    from surogates.runtime import AgentRuntimeContext, agent_runtime_context_dep, rate_limit_dep
    from surogates.runtime.entitlements import ENTITLEMENTS_CONFIG_KEY

    org, user = uuid4(), uuid4()
    pool = ConnectionPool(governance_enabled=True)
    await pool.ensure_connected(org, user, "agent", {LONG_SERVER: {"url": "http://crm"}, "world": {"url": "http://w"}})
    app = FastAPI()
    app.include_router(router)
    app.state.pool = pool
    app.dependency_overrides[get_proxy_auth] = lambda: ProxyAuthContext(org_id=org, user_id=user, session_id=uuid4())
    app.dependency_overrides[agent_runtime_context_dep] = lambda: AgentRuntimeContext(
        agent_id="agent", org_id=str(org), enabled=True, config_version=1, storage_key_prefix="",
        mcp_server_ids=("crm-id", "world-id"))
    app.dependency_overrides[rate_limit_dep] = lambda: None
    listed = TestClient(app).post("/mcp/v1/tools/list", params={"agent_id": "agent"}, json={})
    assert listed.status_code == 200

    class _Response:
        status_code = 200

        @staticmethod
        def json():
            return listed.json()

    registry = ToolRegistry()
    client = McpProxyClient("http://proxy.invalid", registry)

    async def post(*_args, **_kwargs):
        return _Response()

    monkeypatch.setattr(client._client, "post", post)
    monkeypatch.setattr("surogates.orchestrator.mcp_client.create_sandbox_token", lambda *a, **k: "t")
    await client.discover_and_register(org, user, uuid4(), agent_id="agent")
    await client._client.aclose()
    long_name = mcp_tool_name(LONG_SERVER, LONG_TOOL)
    assert long_name in registry.tool_names
    config = {ENTITLEMENTS_CONFIG_KEY: {"mcp_server_ids": ["crm-id"]}}
    crm_only = ({"acme_enterprise_customer_relationship_management_production"}, set())
    excluded = _entitlement_tool_exclusions(session_config=config, tool_registry=registry, mcp_scope=crm_only,
                                            browser_enabled=True)
    assert long_name not in excluded and "mcp__world__ping" in excluded
