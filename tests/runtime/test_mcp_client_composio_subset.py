"""McpProxyClient tracks the Composio-router subset of each agent's MCP tools.

The channel-toolkit filter needs to know which discovered MCP tools came from
the Composio tool-router (vs other MCP servers), so a Slack channel agent can
have its Composio SLACK toolkit hidden without touching other MCP tools.
"""

from __future__ import annotations

from uuid import UUID

from surogates.orchestrator.mcp_client import (
    McpProxyClient,
    is_composio_router_name,
)
from surogates.tools.registry import ToolRegistry


class _FakeResp:
    status_code = 200

    def __init__(self, names):
        self._names = names

    def json(self):
        return {
            "tools": [
                {"name": n, "description": "", "parameters": {}}
                for n in self._names
            ]
        }


def test_is_composio_router_name_recognizes_both_prefixes():
    assert is_composio_router_name("mcp__tool_router__SLACK_SEND_MESSAGE")
    assert is_composio_router_name("mcp__composio_tool_router__SLACK_SEND_MESSAGE")
    assert not is_composio_router_name("mcp__github__list_issues")
    assert not is_composio_router_name("read_file")
    assert not is_composio_router_name("")


async def test_composio_subset_tracked_per_agent(monkeypatch):
    reg = ToolRegistry()
    client = McpProxyClient(base_url="http://proxy", registry=reg)

    async def fake_post(url, headers=None, params=None, json=None):
        return _FakeResp([
            "mcp__tool_router__SLACK_SEND_MESSAGE",
            "mcp__tool_router__GMAIL_SEND_EMAIL",
            "mcp__github__list_issues",
        ])

    monkeypatch.setattr(client._client, "post", fake_post)

    await client.discover_and_register(
        org_id=UUID(int=1), user_id=UUID(int=2), session_id=UUID(int=3),
        agent_id="agent-A",
    )

    composio = client.composio_tool_names_for_agent("agent-A")
    assert composio == frozenset({
        "mcp__tool_router__SLACK_SEND_MESSAGE",
        "mcp__tool_router__GMAIL_SEND_EMAIL",
    })
    # Non-Composio MCP tools are NOT in the Composio subset.
    assert "mcp__github__list_issues" not in composio
    # Unknown agent → empty.
    assert client.composio_tool_names_for_agent("agent-Z") == frozenset()
    await client.close()
