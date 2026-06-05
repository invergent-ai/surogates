"""ConnectionPool is keyed by (org_id, user_id, agent_id).

Two agents under one (org, user) must get disjoint cache entries, and
invalidate_agent() must evict exactly one agent's entries.
"""

from __future__ import annotations

import asyncio
from uuid import UUID

import pytest

from surogates.mcp_proxy.pool import ConnectionPool, PoolEntry, _prefixed_name


ORG = UUID(int=1)
USER = UUID(int=2)


def _entry(agent_id: str, schemas):
    return PoolEntry(
        org_id=ORG, user_id=USER, agent_id=agent_id,
        server_names=[], tool_schemas=schemas,
    )


def test_prefixed_name_includes_agent_id():
    a = _prefixed_name(ORG, USER, "agent-A", "github")
    b = _prefixed_name(ORG, USER, "agent-B", "github")
    assert a != b
    assert a.endswith("__github")


def test_get_cached_schemas_is_agent_keyed():
    pool = ConnectionPool()
    pool._entries[(ORG, USER, "agent-A")] = _entry(
        "agent-A", [{"name": "mcp__github__x"}],
    )
    assert pool.get_cached_schemas(ORG, USER, "agent-A") == [
        {"name": "mcp__github__x"},
    ]
    assert pool.get_cached_schemas(ORG, USER, "agent-B") is None


@pytest.mark.asyncio
async def test_invalidate_agent_evicts_only_that_agent():
    pool = ConnectionPool()
    pool._entries[(ORG, USER, "agent-A")] = _entry("agent-A", [])
    pool._entries[(ORG, USER, "agent-B")] = _entry("agent-B", [])

    pool.invalidate_agent("agent-A")

    assert (ORG, USER, "agent-A") not in pool._entries
    assert (ORG, USER, "agent-B") in pool._entries


@pytest.mark.asyncio
async def test_invalidate_agent_background_task_self_cleans():
    # The teardown task is retained (not GC-eligible) and removes itself
    # from the tracking set via its done-callback once complete.
    pool = ConnectionPool()
    pool._entries[(ORG, USER, "agent-A")] = _entry("agent-A", [])

    pool.invalidate_agent("agent-A")
    assert (ORG, USER, "agent-A") not in pool._entries  # immediate removal

    for _ in range(3):
        await asyncio.sleep(0)
    assert pool._invalidation_tasks == set()


def test_resolve_call_target_denies_cross_agent_tool():
    # A tool registered under agent-A's entry is unreachable for agent-B
    # under the same (org, user): per-agent keying IS the call boundary.
    pool = ConnectionPool()
    entry = _entry("agent-A", [{"name": "mcp__github__list"}])
    prefixed = _prefixed_name(ORG, USER, "agent-A", "github")
    entry.tool_index = {"mcp__github__list": (prefixed, "list")}
    entry.server_configs = {"github": {"transport": "stdio", "command": "cat"}}
    pool._entries[(ORG, USER, "agent-A")] = entry

    # agent-A resolves its own tool.
    assert pool.resolve_call_target(
        ORG, USER, "agent-A", "mcp__github__list",
    ) is not None
    # agent-B (no entry) cannot resolve it.
    assert pool.resolve_call_target(
        ORG, USER, "agent-B", "mcp__github__list",
    ) is None
