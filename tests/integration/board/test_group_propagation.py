"""Coordination-group formation across the three spawn paths."""
from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from surogates.board.groups import ensure_group_and_inherit
from surogates.db.models import Session as ORMSession
from surogates.session.store import SessionStore


@pytest.mark.asyncio(loop_scope="session")
async def test_helper_self_assigns_inherits_and_mirrors_live_config(
    session_factory, parent_session,
):
    store = SessionStore(session_factory)
    live = dict(parent_session.config)
    child_config: dict = {}

    gid = await ensure_group_and_inherit(
        parent_session=parent_session,
        session_store=store,
        child_config=child_config,
        live_parent_config=live,
    )
    assert gid == str(parent_session.id)
    assert child_config["context_group_id"] == gid
    # Live (in-wake) dict mirrored so the same wake's board hook sees it.
    assert live["context_group_id"] == gid
    # Persisted on the parent row.
    refreshed = await store.get_session(parent_session.id)
    assert refreshed.config["context_group_id"] == gid

    # Second spawn inherits the SAME group — no new group formed.
    child_config2: dict = {}
    gid2 = await ensure_group_and_inherit(
        parent_session=refreshed,
        session_store=store,
        child_config=child_config2,
        live_parent_config=None,
    )
    assert gid2 == gid
    assert child_config2["context_group_id"] == gid


@pytest.mark.asyncio(loop_scope="session")
async def test_a_background_agent_stamps_group(
    session_factory, parent_session,
):
    from surogates.tools.builtin.agents import _agent_handler

    store = SessionStore(session_factory)
    redis = AsyncMock()
    redis.zadd = AsyncMock()
    redis.publish = AsyncMock()
    tenant = MagicMock(org_id=parent_session.org_id, user_id=None)
    live_config = dict(parent_session.config)

    result = json.loads(await _agent_handler(
        {"description": "Inspect", "prompt": "inspect channels", "run_in_background": True},
        session_store=store,
        redis=redis,
        tenant=tenant,
        session_id=str(parent_session.id),
        budget=SimpleNamespace(remaining=20),
        session_factory=session_factory,
        session_config=live_config,
    ))
    assert "error" not in result, result
    worker_id = uuid.UUID(result["agent_id"])

    child = await store.get_session(worker_id)
    assert child.config["context_group_id"] == str(parent_session.id)
    # Parent persisted + live dict mirrored (same-wake board read path).
    refreshed = await store.get_session(parent_session.id)
    assert refreshed.config["context_group_id"] == str(parent_session.id)
    assert live_config["context_group_id"] == str(parent_session.id)


@pytest.mark.asyncio(loop_scope="session")
async def test_delegate_child_config_inherits_group(
    session_factory, parent_session,
):
    # The delegate path calls the same helper with the parent loaded
    # fresh from the store; assert the handler-level wiring by driving
    # ensure_group_and_inherit exactly as _run_single_delegation does.
    store = SessionStore(session_factory)
    parent = await store.get_session(parent_session.id)
    child_config: dict = {
        "max_iterations": 10,
        "streaming": False,
        "delegation_depth": 1,
        "delegation_role": "leaf",
    }
    gid = await ensure_group_and_inherit(
        parent_session=parent,
        session_store=store,
        child_config=child_config,
        live_parent_config=None,
    )
    assert child_config["context_group_id"] == gid == str(parent_session.id)
