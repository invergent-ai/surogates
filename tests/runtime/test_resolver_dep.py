"""Tests for ``surogates.runtime.agent_runtime_context_dep``.

Plan 1 / Task 15.  FastAPI dependency that resolves the per-request
``(org_id, agent_id)`` tuple into an
:class:`~surogates.runtime.AgentRuntimeContext`.

Resolution order (v1; expanded in Plan 1b):

1. ``?agent_id=<id>`` query parameter (explicit, highest precedence).
2. ``Host: <slug>.<base_domain>`` subdomain (stub in Plan 1 — Plan
   1b wires the slug → agent_id lookup).
3. ``settings.agent_id`` fallback when ``runtime_mode=helm`` so
   legacy single-agent api pods keep working unchanged.

Failure responses:

- 400 when no ``agent_id`` can be resolved.
- 404 when surogate-ops refuses the agent (``runtime_kind != shared``
  or the row does not exist).
- 503 when the agent exists but is administratively stopped
  (``enabled=false``).
"""

from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from types import SimpleNamespace

from surogates.runtime import (
    AgentRuntimeContext,
    RuntimeConfigCache,
    agent_runtime_context_dep,
)


def _make_payload(*, agent_id="a-1", enabled=True) -> dict:
    return {
        "agent_id": agent_id,
        "org_id": "o-1",
        "project_id": "p-1",
        "enabled": enabled,
        "version": 1,
        "api_web_url": None,
        "llm_main": {"model": "m", "base_url": "u", "api_key_ref": "v"},
        "llm_summary": None,
        "llm_vision": None,
        "llm_advisor": None,
        "mcp_server_ids": [],
        "governance": {},
        "storage_key_prefix": "p-1/a-1",
    }


def _build_app(*, cache: RuntimeConfigCache, runtime_mode="shared", agent_id=""):
    app = FastAPI()
    app.state.runtime_config_cache = cache
    app.state.settings = SimpleNamespace(
        runtime_mode=runtime_mode, agent_id=agent_id,
    )

    @app.get("/echo")
    async def echo(ctx: AgentRuntimeContext = Depends(agent_runtime_context_dep)):
        return {"agent_id": ctx.agent_id, "org_id": ctx.org_id}

    return app


def test_resolves_from_query_param():
    async def loader(agent_id: str) -> dict:
        return _make_payload(agent_id=agent_id)

    app = _build_app(cache=RuntimeConfigCache(loader=loader))
    with TestClient(app) as c:
        r = c.get("/echo?agent_id=a-1")
    assert r.status_code == 200
    assert r.json() == {"agent_id": "a-1", "org_id": "o-1"}


def test_400_when_no_agent_id_anywhere_in_shared_mode():
    async def loader(_):
        return _make_payload()

    app = _build_app(cache=RuntimeConfigCache(loader=loader))
    with TestClient(app) as c:
        r = c.get("/echo")
    assert r.status_code == 400
    assert "agent_id" in r.json()["detail"].lower()


def test_404_when_platform_returns_lookup_error():
    async def loader(_):
        raise LookupError("not shared")

    app = _build_app(cache=RuntimeConfigCache(loader=loader))
    with TestClient(app) as c:
        r = c.get("/echo?agent_id=a-1")
    assert r.status_code == 404
    assert "shared runtime" in r.json()["detail"].lower()


def test_503_when_agent_is_disabled():
    async def loader(agent_id: str) -> dict:
        return _make_payload(agent_id=agent_id, enabled=False)

    app = _build_app(cache=RuntimeConfigCache(loader=loader))
    with TestClient(app) as c:
        r = c.get("/echo?agent_id=a-1")
    assert r.status_code == 503
    assert "stopped" in r.json()["detail"].lower()


def test_helm_mode_falls_back_to_process_wide_settings_agent_id():
    """When the process-wide ``runtime_mode`` is ``helm``, the per-pod
    ``settings.agent_id`` IS the tenant — no query param required.
    The cache loader is still consulted (legacy api pods will be
    pointed at their own loader in Plan 7 lifecycle migration).
    """
    async def loader(agent_id: str) -> dict:
        return _make_payload(agent_id=agent_id)

    app = _build_app(
        cache=RuntimeConfigCache(loader=loader),
        runtime_mode="helm",
        agent_id="legacy-a",
    )
    with TestClient(app) as c:
        r = c.get("/echo")
    assert r.status_code == 200
    assert r.json()["agent_id"] == "legacy-a"


def test_shared_mode_does_not_fall_back_to_settings_agent_id():
    """In shared mode, the legacy fallback is intentionally disabled so
    a misconfigured pod (settings.agent_id leftover) cannot silently
    route traffic to the wrong tenant.  400 surfaces the mistake.
    """
    async def loader(agent_id: str) -> dict:
        return _make_payload(agent_id=agent_id)

    app = _build_app(
        cache=RuntimeConfigCache(loader=loader),
        runtime_mode="shared",
        agent_id="stale-leftover",
    )
    with TestClient(app) as c:
        r = c.get("/echo")
    assert r.status_code == 400


def test_query_param_overrides_settings_agent_id_in_helm_mode():
    """Explicit query param wins even in helm mode — useful for admin
    tools that pin requests to a specific agent_id from inside a
    per-agent pod."""
    async def loader(agent_id: str) -> dict:
        return _make_payload(agent_id=agent_id)

    app = _build_app(
        cache=RuntimeConfigCache(loader=loader),
        runtime_mode="helm",
        agent_id="pod-agent",
    )
    with TestClient(app) as c:
        r = c.get("/echo?agent_id=explicit")
    assert r.status_code == 200
    assert r.json()["agent_id"] == "explicit"
