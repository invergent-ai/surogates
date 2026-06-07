from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from surogates.mcp_proxy.loader import apply_composio_minting


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
