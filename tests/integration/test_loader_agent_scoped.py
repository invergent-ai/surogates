"""DB-backed: the loader returns exactly the agent's attached servers.

Two server rows exist under one org; the loader returns only the ids in
the allow-list, proving per-agent scoping at the SQL layer.
"""

from __future__ import annotations

import uuid

import pytest

from surogates.db.models import McpServer
from surogates.mcp_proxy.loader import _load_db_configs

from .conftest import create_org

# Integration tests share the session-scoped engine/session_factory
# fixtures, so every coroutine must run on the session event loop.
pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_loader_returns_only_attached_ids(session_factory):
    org_id = await create_org(session_factory)

    keep_id = uuid.uuid4()
    drop_id = uuid.uuid4()
    async with session_factory() as db:
        db.add(McpServer(
            id=keep_id, org_id=org_id, user_id=None, name="github",
            transport="stdio", command="cat", enabled=True,
        ))
        db.add(McpServer(
            id=drop_id, org_id=org_id, user_id=None, name="jira",
            transport="stdio", command="cat", enabled=True,
        ))
        await db.commit()

    configs = await _load_db_configs(
        session_factory=session_factory,
        org_id=org_id,
        allowed_ids=frozenset({str(keep_id)}),
    )

    assert set(configs) == {"github"}
