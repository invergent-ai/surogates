"""Tests for resolve_runtime_context_for_session.

The worker's harness_factory needs to project a
DB session row into an AgentRuntimeContext using the same cache the
api uses.  This module is the worker-side bridge — pure async
function so the harness factory can call it without knowing about
the cache's existence (the cache is wired into the loader).
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from uuid import uuid4

import pytest

from surogates.runtime import AgentRuntimeContext, RuntimeConfigCache


def _make_payload(*, agent_id, enabled=True) -> dict:
    return {
        "agent_id": agent_id,
        "org_id": "o-1",
        "project_id": "p-1",
        "enabled": enabled,
        "version": 1,
        "api_web_url": None,
        "llm_main": {
            "model": "m", "base_url": "u", "api_key_ref": "vault://k",
        },
        "llm_summary": None,
        "llm_vision": None,
        "llm_advisor": None,
        "mcp_server_ids": [],
        "governance": {},
        "storage_key_prefix": "p-1/" + agent_id,
    }


@dataclass
class _FakeSessionRow:
    id: object
    agent_id: str
    org_id: object


@pytest.mark.asyncio
async def test_resolve_runtime_context_shared_mode_uses_cache():
    from surogates.runtime import resolve_runtime_context_for_session

    calls: list[str] = []

    async def loader(agent_id):
        calls.append(agent_id)
        return _make_payload(agent_id=agent_id)

    cache = RuntimeConfigCache(loader=loader, ttl_seconds=10)
    settings = SimpleNamespace(runtime_mode="shared")
    sess = _FakeSessionRow(id=uuid4(), agent_id="a-1", org_id=uuid4())

    ctx = await resolve_runtime_context_for_session(
        sess, cache=cache, settings=settings,
    )
    assert isinstance(ctx, AgentRuntimeContext)
    assert ctx.agent_id == "a-1"
    assert ctx.org_id == "o-1"
    assert calls == ["a-1"]


@pytest.mark.asyncio
async def test_resolve_runtime_context_helm_mode_synthesises_from_settings():
    """Helm-mode workers have no cache; the helper synthesises the
    context from process-wide settings.  Mirrors the
    _legacy_helm_context branch in the api resolver."""
    from surogates.runtime import resolve_runtime_context_for_session

    settings = SimpleNamespace(
        runtime_mode="helm", org_id="helm-org", agent_id="helm-agent",
    )
    sess = _FakeSessionRow(id=uuid4(), agent_id="helm-agent", org_id=uuid4())

    ctx = await resolve_runtime_context_for_session(
        sess, cache=None, settings=settings,
    )
    assert ctx.agent_id == "helm-agent"
    assert ctx.org_id == "helm-org"
    assert ctx.project_id is None


@pytest.mark.asyncio
async def test_resolve_runtime_context_shared_disabled_raises():
    """A row marked enabled=False is an administrative stop; the
    worker must refuse to process the session rather than serve it
    as if nothing happened."""
    from surogates.runtime import (
        AgentDisabledError, resolve_runtime_context_for_session,
    )

    async def loader(agent_id):
        return _make_payload(agent_id=agent_id, enabled=False)

    cache = RuntimeConfigCache(loader=loader, ttl_seconds=10)
    settings = SimpleNamespace(runtime_mode="shared")
    sess = _FakeSessionRow(id=uuid4(), agent_id="a-1", org_id=uuid4())

    with pytest.raises(AgentDisabledError):
        await resolve_runtime_context_for_session(
            sess, cache=cache, settings=settings,
        )


@pytest.mark.asyncio
async def test_resolve_runtime_context_shared_lookup_error_propagates():
    """If surogate-ops returns 404 for a session's agent, the
    LookupError surfaces — caller (harness_factory) catches and
    marks the session failed."""
    from surogates.runtime import resolve_runtime_context_for_session

    async def loader(agent_id):
        raise LookupError("not shared")

    cache = RuntimeConfigCache(loader=loader, ttl_seconds=10)
    settings = SimpleNamespace(runtime_mode="shared")
    sess = _FakeSessionRow(id=uuid4(), agent_id="a-1", org_id=uuid4())

    with pytest.raises(LookupError):
        await resolve_runtime_context_for_session(
            sess, cache=cache, settings=settings,
        )
