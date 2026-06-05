"""ConnectionPool is keyed by (org_id, user_id, agent_id).

Two agents under one (org, user) must get disjoint cache entries, and
invalidate_agent() must evict exactly one agent's entries.
"""

from __future__ import annotations

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
