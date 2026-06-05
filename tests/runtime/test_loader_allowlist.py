"""Strict per-agent MCP loading: an empty allow-list yields no servers.

The empty case short-circuits before any DB access, so it is unit-
testable without a database.
"""

from __future__ import annotations

from uuid import UUID

import pytest

from surogates.mcp_proxy.loader import _load_db_configs


@pytest.mark.asyncio
async def test_empty_allowlist_returns_no_servers():
    # session_factory is never touched when allowed_ids is empty.
    result = await _load_db_configs(
        session_factory=None,
        org_id=UUID(int=1),
        allowed_ids=frozenset(),
    )
    assert result == {}


@pytest.mark.asyncio
async def test_all_malformed_ids_returns_empty_without_db():
    # A corrupt runtime config (non-UUID ids) must not 500 discovery.
    # When every id is malformed the result is empty and the DB is never
    # touched (session_factory=None proves it).
    result = await _load_db_configs(
        session_factory=None,
        org_id=UUID(int=1),
        allowed_ids=frozenset({"not-a-uuid", ""}),
    )
    assert result == {}
