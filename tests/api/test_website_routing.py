"""Website bootstrap resolves the agent from channel_routing (per-agent).

These exercise the gate behaviour only (no storage / session store), which is
the security-critical surface: key→routing resolution + the global origin
allow-list.
"""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from surogates.api.routes import website


class _FakeCache:
    def __init__(self, rows):
        self._rows = rows

    async def get(self, key):
        return self._rows.get(key)


def _settings(allowed="https://acme.com"):
    return SimpleNamespace(
        website=SimpleNamespace(
            enabled=True,
            allowed_origins=allowed,
            publishable_key="",
            session_message_cap=0,
        ),
        storage=SimpleNamespace(bucket="bkt"),
        llm=SimpleNamespace(model="m"),
    )


def _app(rows, allowed="https://acme.com"):
    app = FastAPI()
    app.include_router(website.router, prefix="/v1")
    app.state.settings = _settings(allowed)
    app.state.channel_routing_cache = _FakeCache(rows)
    # No rate_limiter on app.state → the website rate-limit helper passes through.
    return app


async def _post(app, *, key, origin):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://widget") as c:
        return await c.post(
            "/v1/website/sessions",
            headers={"Authorization": f"Bearer {key}", "Origin": origin},
        )


@pytest.mark.asyncio
async def test_bootstrap_unknown_key_returns_404():
    app = _app(rows={})
    r = await _post(app, key="surg_wk_nope", origin="https://acme.com")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_bootstrap_missing_key_returns_401():
    app = _app(rows={})
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://widget") as c:
        r = await c.post(
            "/v1/website/sessions", headers={"Origin": "https://acme.com"},
        )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_bootstrap_origin_not_allowlisted_returns_403():
    rows = {"website:surg_wk_ok": {"agent_id": "a1", "org_id": "o1", "api_web_url": "https://acme.com"}}
    app = _app(rows, allowed="https://acme.com")
    r = await _post(app, key="surg_wk_ok", origin="https://evil.com")
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_bootstrap_ignores_a_body_naming_a_memory_boundary():
    """Nothing the browser sends names the boundary: it is derived server-side,
    and a resold embed's visitors stay apart from the operator's own."""
    import uuid

    from surogates.channels.memory_boundary import visitor_boundary
    from tests.api.test_website_multi_session import _Storage, _Store

    org_id = str(uuid.uuid4())
    row = {
        "agent_id": "a1",
        "org_id": org_id,
        "api_web_url": "https://acme.com",
        "config": {"embed_end_user_id": "eu-7"},
    }
    app = _app(rows={"website:surg_wk_k": row})
    store = _Store()
    app.state.session_store = store
    app.state.storage = _Storage()
    visitor = str(uuid.uuid4())

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="https://widget",
        cookies={website.VISITOR_COOKIE_NAME: visitor},
    ) as c:
        r = await c.post(
            "/v1/website/sessions",
            headers={"Authorization": "Bearer surg_wk_k", "Origin": "https://acme.com"},
            json={"memory_boundary": "public", "config": {"memory_boundary": "public"}},
        )

    assert r.status_code == 201, r.text
    boundary = store.created[0]["config"]["memory_boundary"]
    assert boundary == visitor_boundary("a1", "eu-7", visitor)
    assert boundary != visitor_boundary("a1", None, visitor)

