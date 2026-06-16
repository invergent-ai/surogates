"""Tests for ``surogates.runtime.agent_runtime_context_dep``.

FastAPI dependency that resolves the per-request
``(org_id, agent_id)`` tuple into an
:class:`~surogates.runtime.AgentRuntimeContext`.

Resolution order:

1. ``?agent_id=<id>`` query parameter (explicit, highest precedence).
2. ``Host: <slug>.<base_domain>`` subdomain
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
    assert "not configured" in r.json()["detail"].lower()


def test_503_when_agent_is_disabled():
    async def loader(agent_id: str) -> dict:
        return _make_payload(agent_id=agent_id, enabled=False)

    app = _build_app(cache=RuntimeConfigCache(loader=loader))
    with TestClient(app) as c:
        r = c.get("/echo?agent_id=a-1")
    assert r.status_code == 503
    assert "stopped" in r.json()["detail"].lower()


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

def test_resolver_resolves_via_host_header_subdomain():
    """A Host like ``acme.runtime.example.com``
    flows through SlugResolverCache → agent_id and then through the
    normal runtime-config cache path."""
    from surogates.runtime import SlugResolverCache

    async def runtime_loader(agent_id: str) -> dict:
        return _make_payload(agent_id=agent_id)

    async def slug_loader(slug: str) -> str | None:
        return "agent-from-slug" if slug == "acme" else None

    app = _build_app(cache=RuntimeConfigCache(loader=runtime_loader))
    app.state.slug_resolver_cache = SlugResolverCache(loader=slug_loader)

    @app.get("/echo2")
    async def echo2(
        ctx: AgentRuntimeContext = Depends(agent_runtime_context_dep),
    ):
        return {"agent_id": ctx.agent_id}

    with TestClient(app) as c:
        r = c.get("/echo2", headers={"Host": "acme.runtime.example.com"})
    assert r.status_code == 200
    assert r.json()["agent_id"] == "agent-from-slug"


def test_resolver_skips_slug_lookup_for_reserved_subdomains():
    """``www.``, ``api.``, ``localhost`` (and the unset host) must
    not trigger a slug lookup — the resolver short-circuits before
    the cache is consulted."""
    from surogates.runtime import SlugResolverCache

    called: list[str] = []

    async def runtime_loader(agent_id: str) -> dict:
        return _make_payload(agent_id=agent_id)

    async def slug_loader(slug: str) -> str | None:
        called.append(slug)
        return "x"

    app = _build_app(cache=RuntimeConfigCache(loader=runtime_loader))
    app.state.slug_resolver_cache = SlugResolverCache(loader=slug_loader)

    @app.get("/echo3")
    async def echo3(
        ctx: AgentRuntimeContext = Depends(agent_runtime_context_dep),
    ):
        return {"agent_id": ctx.agent_id}

    with TestClient(app) as c:
        r = c.get("/echo3", headers={"Host": "www.example.com"})
    assert r.status_code == 400
    assert called == []


def test_resolver_falls_through_to_400_when_slug_unknown():
    """A non-reserved subdomain with no matching agent must surface
    a clean 400 (no agent_id resolved) — not crash or 500."""
    from surogates.runtime import SlugResolverCache

    async def runtime_loader(agent_id: str) -> dict:
        return _make_payload(agent_id=agent_id)

    async def slug_loader(_slug: str) -> str | None:
        return None

    app = _build_app(cache=RuntimeConfigCache(loader=runtime_loader))
    app.state.slug_resolver_cache = SlugResolverCache(loader=slug_loader)

    @app.get("/echo4")
    async def echo4(
        ctx: AgentRuntimeContext = Depends(agent_runtime_context_dep),
    ):
        return {"agent_id": ctx.agent_id}

    with TestClient(app) as c:
        r = c.get("/echo4", headers={"Host": "unknown.runtime.example.com"})
    assert r.status_code == 400


def test_resolver_without_slug_cache_returns_none_silently():
    """Helm-mode pods do not wire ``slug_resolver_cache``.  The
    resolver must treat that as "no Host-header routing available"
    and fall through, not raise AttributeError."""
    async def runtime_loader(agent_id: str) -> dict:
        return _make_payload(agent_id=agent_id)

    app = _build_app(cache=RuntimeConfigCache(loader=runtime_loader))
    # Deliberately leave app.state.slug_resolver_cache unset.

    @app.get("/echo5")
    async def echo5(
        ctx: AgentRuntimeContext = Depends(agent_runtime_context_dep),
    ):
        return {"agent_id": ctx.agent_id}

    with TestClient(app) as c:
        r = c.get("/echo5", headers={"Host": "acme.example.com"})
    # No cache wired ⇒ slug lookup returns None silently ⇒ 400
    # (no settings.agent_id fallback in shared mode).
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
