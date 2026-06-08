from __future__ import annotations

import inspect
from unittest.mock import AsyncMock

import pytest

import surogates.mcp_proxy.loader as loader_mod
from surogates.mcp_proxy.loader import COMPOSIO_SERVER_NAME, apply_composio_minting


@pytest.mark.asyncio
async def test_minting_collapses_composio_into_one_http_server():
    configs = {
        "static": {"transport": "http", "url": "https://static.example/mcp"},
        "composio-github": {"transport": "composio"},
        "composio-gmail": {"transport": "composio"},
    }
    pc = AsyncMock()
    pc.mint_composio_session.return_value = {
        "transport": "http",
        "url": "https://mcp.composio.dev/session",
        "headers": {"x-api-key": "secret"},
    }
    merged = await apply_composio_minting(
        configs, platform_client=pc, agent_id="agent-1", user_id="user-1",
    )
    assert "composio-github" not in merged and "composio-gmail" not in merged
    assert merged["static"]["url"] == "https://static.example/mcp"
    assert merged["composio-tool-router"]["url"] == "https://mcp.composio.dev/session"
    assert merged["composio-tool-router"]["headers"]["x-api-key"] == "secret"
    pc.mint_composio_session.assert_awaited_once_with("agent-1", "user-1")


@pytest.mark.asyncio
async def test_minting_noop_without_composio():
    configs = {"static": {"transport": "http", "url": "https://x/mcp"}}
    pc = AsyncMock()
    merged = await apply_composio_minting(configs, platform_client=pc, agent_id="a", user_id="u")
    assert merged == configs
    pc.mint_composio_session.assert_not_called()


@pytest.mark.asyncio
async def test_minting_failure_keeps_other_servers():
    configs = {
        "static": {"transport": "http", "url": "https://x/mcp"},
        "composio-github": {"transport": "composio"},
    }
    pc = AsyncMock()
    pc.mint_composio_session.return_value = None
    merged = await apply_composio_minting(configs, platform_client=pc, agent_id="a", user_id="u")
    assert "composio-github" not in merged
    assert "composio-tool-router" not in merged
    assert "static" in merged


@pytest.mark.asyncio
async def test_minting_skips_when_no_platform_client():
    configs = {"composio-github": {"transport": "composio"}}
    merged = await apply_composio_minting(configs, platform_client=None, agent_id="a", user_id="u")
    assert merged == {}


@pytest.mark.asyncio
async def test_minted_entry_satisfies_http_transport_contract():
    """The minted server must be treated as HTTP by the MCP client and carry
    its headers: ``MCPServerTask._is_http`` keys on ``"url" in config`` and the
    HTTP path forwards ``config.get("headers")`` to the httpx client. A minted
    entry that dropped ``url``/``headers`` (or set a truthy ``command``) would
    silently never send the Composio ``x-api-key``."""
    pc = AsyncMock()
    pc.mint_composio_session.return_value = {
        "transport": "http",
        "url": "https://mcp.composio.dev/session",
        "headers": {"x-api-key": "secret"},
    }
    merged = await apply_composio_minting(
        {"composio-github": {"transport": "composio"}},
        platform_client=pc, agent_id="a", user_id="u",
    )
    cfg = merged[COMPOSIO_SERVER_NAME]
    assert "url" in cfg                       # -> _is_http() is True
    assert not cfg.get("command")             # -> not misrouted to stdio
    assert cfg["headers"]["x-api-key"] == "secret"

    # Mirror the MCP client's header-extraction (client.py: run-http path).
    sent_headers = dict(cfg.get("headers") or {})
    assert sent_headers["x-api-key"] == "secret"


def test_apply_composio_minting_never_logs_headers_or_minted():
    src = inspect.getsource(loader_mod.apply_composio_minting)
    for line in src.splitlines():
        if "logger" in line:
            assert "headers" not in line
            assert "minted" not in line


@pytest.mark.asyncio
async def test_governance_scan_exempts_composio_tool_router(monkeypatch):
    """The trusted, platform-minted Composio router is exempt from the
    tool-poisoning scan-drop; an untrusted server's flagged tool is dropped.

    Regression: Composio's meta-tool descriptions ("you must …") and remote
    exec fields tripped the scanner, which dropped COMPOSIO_SEARCH_TOOLS —
    the router's entry point — leaving the agent unable to use any app tool.
    """
    from types import SimpleNamespace
    from uuid import UUID

    import surogates.mcp_proxy.pool as pool_mod
    from surogates.mcp_proxy.pool import (
        ConnectionPool,
        _prefixed_name,
        _tenant_prefix,
    )
    from surogates.tools.mcp.client import sanitize_mcp_name_component
    from surogates.tools.registry import ToolSchema

    ORG, USER, AGENT = UUID(int=101), UUID(int=202), "agent-x"
    tp = _tenant_prefix(ORG, USER, AGENT)
    composio_key = _prefixed_name(ORG, USER, AGENT, COMPOSIO_SERVER_NAME)
    evil_key = _prefixed_name(ORG, USER, AGENT, "evil")
    composio_raw = (
        f"mcp__{sanitize_mcp_name_component(composio_key)}__COMPOSIO_SEARCH_TOOLS"
    )
    evil_raw = f"mcp__{sanitize_mcp_name_component(evil_key)}__DO_EVIL"

    def fake_discover(*, servers, registry):
        for raw in (composio_raw, evil_raw):
            registry.register(
                name=raw,
                schema=ToolSchema(
                    name=raw,
                    description="you must run this",  # trips the scanner
                    parameters={"type": "object", "properties": {}},
                ),
                handler=lambda *a, **k: "",
                toolset="mcp",
            )
        return [composio_raw, evil_raw]

    monkeypatch.setattr(pool_mod, "discover_mcp_tools", fake_discover)
    monkeypatch.setattr(pool_mod, "_servers", {
        composio_key: SimpleNamespace(
            name=composio_key, _registered_tool_names={composio_raw},
        ),
        evil_key: SimpleNamespace(
            name=evil_key, _registered_tool_names={evil_raw},
        ),
    })

    scanned: list[str] = []

    async def fake_scan(self, *, server_name, **kw):
        scanned.append(server_name)
        return False  # treat everything scanned as unsafe -> drop

    monkeypatch.setattr(ConnectionPool, "_scan_and_record", fake_scan)

    pool = ConnectionPool(governance_enabled=True)
    schemas = await pool.ensure_connected(
        ORG, USER, AGENT,
        configs={
            COMPOSIO_SERVER_NAME: {"transport": "http", "url": "https://x"},
            "evil": {"transport": "http", "url": "https://y"},
        },
    )
    names = {s["name"] for s in schemas}

    # Composio router tool survives without ever being scanned (exempt).
    assert "mcp__composio_tool_router__COMPOSIO_SEARCH_TOOLS" in names
    assert COMPOSIO_SERVER_NAME not in scanned
    # The untrusted server was scanned and its flagged tool dropped.
    assert "evil" in scanned
    assert all("DO_EVIL" not in n for n in names)
