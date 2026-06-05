"""McpProxyClient discovery is per-agent.

The worker shares one ToolRegistry across every agent it serves, so
discovery must (a) send the agent_id to the proxy, (b) track which
tools each agent has, and (c) return the agent's FULL discovered set
on every call (not just newly-registered names) so the harness can
filter the shared registry's prompt schemas down to this agent.
"""

from __future__ import annotations

from uuid import UUID

import pytest

from surogates.orchestrator.mcp_client import McpProxyClient
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


@pytest.mark.asyncio
async def test_discover_sends_agent_id_and_returns_full_set(monkeypatch):
    reg = ToolRegistry()
    client = McpProxyClient(base_url="http://proxy", registry=reg)

    captured = []

    async def fake_post(url, headers=None, params=None, json=None):
        captured.append((url, params))
        return _FakeResp(["mcp__github__list_issues"])

    monkeypatch.setattr(client._client, "post", fake_post)

    names = await client.discover_and_register(
        org_id=UUID(int=1), user_id=UUID(int=2), session_id=UUID(int=3),
        agent_id="agent-A",
    )

    assert names == ["mcp__github__list_issues"]
    assert captured[0][1] == {"agent_id": "agent-A"}
    assert "mcp__github__list_issues" in reg.tool_names

    # Second discovery for the SAME agent returns the full set again,
    # not an empty "nothing new" list.
    names2 = await client.discover_and_register(
        org_id=UUID(int=1), user_id=UUID(int=2), session_id=UUID(int=3),
        agent_id="agent-A",
    )
    assert names2 == ["mcp__github__list_issues"]
    await client.close()


@pytest.mark.asyncio
async def test_discover_tracks_each_agent_separately(monkeypatch):
    reg = ToolRegistry()
    client = McpProxyClient(base_url="http://proxy", registry=reg)

    async def fake_post(url, headers=None, params=None, json=None):
        agent = params["agent_id"]
        names = {
            "agent-A": ["mcp__github__list_issues"],
            "agent-B": ["mcp__jira__search"],
        }[agent]
        return _FakeResp(names)

    monkeypatch.setattr(client._client, "post", fake_post)

    a = await client.discover_and_register(
        org_id=UUID(int=1), user_id=UUID(int=2), session_id=UUID(int=3),
        agent_id="agent-A",
    )
    b = await client.discover_and_register(
        org_id=UUID(int=1), user_id=UUID(int=2), session_id=UUID(int=4),
        agent_id="agent-B",
    )

    assert a == ["mcp__github__list_issues"]
    assert b == ["mcp__jira__search"]
    await client.close()


@pytest.mark.asyncio
async def test_successful_rediscovery_replaces_agent_tool_set(monkeypatch):
    reg = ToolRegistry()
    client = McpProxyClient(base_url="http://proxy", registry=reg)

    responses = [
        ["mcp__github__list_issues"],
        [],
    ]

    async def fake_post(url, headers=None, params=None, json=None):
        return _FakeResp(responses.pop(0))

    monkeypatch.setattr(client._client, "post", fake_post)

    first = await client.discover_and_register(
        org_id=UUID(int=1), user_id=UUID(int=2), session_id=UUID(int=3),
        agent_id="agent-A",
    )
    second = await client.discover_and_register(
        org_id=UUID(int=1), user_id=UUID(int=2), session_id=UUID(int=4),
        agent_id="agent-A",
    )

    assert first == ["mcp__github__list_issues"]
    assert second == []
    # The handler may remain registered process-wide; the per-agent
    # discovery set is what controls prompt-schema visibility.
    assert "mcp__github__list_issues" in reg.tool_names
    await client.close()
