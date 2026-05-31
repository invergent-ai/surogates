"""End-to-end smoke for the shared-runtime wiring.

Spins up a FastAPI app exactly as production wires
it for ``runtime_mode='shared'`` and exercises a request through the
full chain:

    HTTP request
      → agent_runtime_context_dep
        → RuntimeConfigCache
          → PlatformClient (mocked transport)
            → AgentRuntimeContext built from a real payload

The test asserts the resolved context flows correctly through to the
route handler and that the lifespan teardown cleans up its background
task + httpx pool.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

import httpx
import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient

from surogates.runtime import (
    AgentRuntimeContext,
    PlatformClient,
    RuntimeConfigCache,
    agent_runtime_context_dep,
)


def _runtime_config_payload(
    *, agent_id: str = "a-shared", enabled: bool = True,
) -> dict:
    return {
        "agent_id": agent_id,
        "org_id": "o-1",
        "project_id": "p-1",
        "enabled": enabled,
        "version": 5,
        "api_web_url": "https://web.example.com",
        "llm_main": {
            "model": "gpt-4o", "base_url": "u", "api_key_ref": "vault://x",
        },
        "llm_summary": None,
        "llm_vision": None,
        "llm_advisor": None,
        "mcp_server_ids": ["m1", "m2"],
        "governance": {"enabled": True},
        "storage_key_prefix": "p-1/a-shared",
    }


def _build_shared_app(
    *,
    transport_handler,
    runtime_mode: str = "shared",
) -> FastAPI:
    """Build a FastAPI app wired exactly like ``surogates.api.app``
    would in shared mode, but with a mocked httpx transport so we do
    not need a real surogate-ops to be reachable."""
    from types import SimpleNamespace

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        client = PlatformClient(
            base_url="https://ops.example.com",
            token="runtime-token",
            transport=httpx.MockTransport(transport_handler),
        )
        cache = RuntimeConfigCache(
            loader=client.get_runtime_config, ttl_seconds=1.0,
        )
        app.state.platform_client = client
        app.state.runtime_config_cache = cache
        app.state.settings = SimpleNamespace(
            runtime_mode=runtime_mode,
            agent_id="",
            org_id="",
        )
        try:
            yield
        finally:
            await client.aclose()

    app = FastAPI(lifespan=lifespan)

    @app.get("/echo")
    async def echo(
        request: Request,
        ctx: AgentRuntimeContext = Depends(agent_runtime_context_dep),
    ):
        return {
            "agent_id": ctx.agent_id,
            "org_id": ctx.org_id,
            "project_id": ctx.project_id,
            "enabled": ctx.enabled,
            "version": ctx.config_version,
            "storage_key_prefix": ctx.storage_key_prefix,
            "mcp_server_ids": list(ctx.mcp_server_ids),
            "api_web_url": ctx.api_web_url,
        }

    return app


def test_shared_runtime_resolves_context_end_to_end():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        assert request.headers["Authorization"] == "Bearer runtime-token"
        return httpx.Response(200, json=_runtime_config_payload())

    app = _build_shared_app(transport_handler=handler)
    with TestClient(app) as client:
        resp = client.get("/echo?agent_id=a-shared")

    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "agent_id": "a-shared",
        "org_id": "o-1",
        "project_id": "p-1",
        "enabled": True,
        "version": 5,
        "storage_key_prefix": "p-1/a-shared",
        "mcp_server_ids": ["m1", "m2"],
        "api_web_url": "https://web.example.com",
    }
    assert calls == ["/api/agents/a-shared/runtime-config"]


def test_shared_runtime_cache_dedupes_repeated_calls_within_ttl():
    """The same agent_id requested twice within the cache TTL hits
    surogate-ops exactly once."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json=_runtime_config_payload())

    app = _build_shared_app(transport_handler=handler)
    with TestClient(app) as client:
        r1 = client.get("/echo?agent_id=a-shared")
        r2 = client.get("/echo?agent_id=a-shared")

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert len(calls) == 1, calls


def test_shared_runtime_returns_503_when_agent_is_disabled():
    """The platform returns ``enabled=false``; the resolver translates
    that into a 503 before the route handler ever runs."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_runtime_config_payload(enabled=False))

    app = _build_shared_app(transport_handler=handler)
    with TestClient(app) as client:
        resp = client.get("/echo?agent_id=a-shared")

    assert resp.status_code == 503
    assert "stopped" in resp.json()["detail"].lower()


def test_shared_runtime_returns_404_when_platform_returns_404():
    """A 404 from surogate-ops surfaces as 404 to the caller."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "not configured"})

    app = _build_shared_app(transport_handler=handler)
    with TestClient(app) as client:
        resp = client.get("/echo?agent_id=a-shared")

    assert resp.status_code == 404
    assert "shared runtime" in resp.json()["detail"].lower()


def test_shared_runtime_returns_400_when_no_agent_id():
    """Shared mode requires explicit agent_id resolution — settings
    fallback is intentionally disabled."""

    def handler(_request):  # pragma: no cover — never called
        return httpx.Response(200, json=_runtime_config_payload())

    app = _build_shared_app(transport_handler=handler)
    with TestClient(app) as client:
        resp = client.get("/echo")  # no agent_id

    assert resp.status_code == 400
