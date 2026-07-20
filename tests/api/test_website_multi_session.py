"""Website bootstrap under the "multi session" capability.

With ``multi_session: false`` projected into the routing config, a
re-bootstrap from a browser still holding a valid session cookie
returns the visitor's existing session (HTTP 200, fresh CSRF + cookie)
instead of minting a new one.  A missing/foreign cookie, a dead
session, or the capability left on all fall through to today's
fresh-create behavior.
"""

from __future__ import annotations

import os
import uuid
from types import SimpleNamespace

os.environ.setdefault(
    "SUROGATES_AUTH_JWT_SECRET", "test-secret-key-for-tests-0123456789"
)

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from surogates.api.routes import website
from surogates.channels.website_session import (
    COOKIE_NAME,
    create_website_session_token,
    generate_csrf_token,
)
from surogates.session.store import SessionNotFoundError

pytestmark = pytest.mark.asyncio

_KEY = "surg_wk_test0000000000000000"
_ORIGIN = "https://acme.com"


class _FakeCache:
    def __init__(self, rows):
        self._rows = rows

    async def get(self, key):
        return self._rows.get(key)


class _Storage:
    async def create_bucket(self, bucket):
        return None

    def resolve_workspace_path(self, bucket, session_id):
        return f"/bucket-root/{bucket}/{session_id}"


class _Store:
    def __init__(self, sessions=None):
        self.sessions = sessions or {}
        self.created: list[dict] = []

    async def get_session(self, session_id):
        try:
            return self.sessions[session_id]
        except KeyError:
            raise SessionNotFoundError(f"session {session_id} not found")

    async def create_session(self, **kwargs):
        self.created.append(kwargs)
        return SimpleNamespace(
            id=kwargs["session_id"],
            agent_id=kwargs["agent_id"],
            channel=kwargs["channel"],
            status="active",
            config=kwargs["config"],
        )

    async def update_session_status(self, session_id, status):
        return None


def _settings():
    return SimpleNamespace(
        website=SimpleNamespace(
            enabled=True,
            allowed_origins=_ORIGIN,
            publishable_key="",
            session_message_cap=0,
        ),
        storage=SimpleNamespace(bucket="bkt"),
        llm=SimpleNamespace(model="m"),
    )


def _app(org_id, store, *, multi_session):
    config = {}
    if multi_session is not None:
        config["multi_session"] = multi_session
    rows = {
        f"website:{_KEY}": {
            "agent_id": "hero-agent",
            "org_id": str(org_id),
            "api_web_url": _ORIGIN,
            "config": config,
        }
    }
    app = FastAPI()
    app.include_router(website.router, prefix="/v1")
    app.state.settings = _settings()
    app.state.channel_routing_cache = _FakeCache(rows)
    app.state.session_store = store
    app.state.storage = _Storage()
    return app


def _cookie(session_id, org_id, *, origin=_ORIGIN, key=_KEY):
    return create_website_session_token(
        session_id=session_id,
        org_id=org_id,
        origin=origin,
        csrf_token=generate_csrf_token(),
        channel_identifier=key,
    )


def _live_session(session_id, *, status="active", agent_id="hero-agent"):
    return SimpleNamespace(
        id=session_id,
        agent_id=agent_id,
        channel="website",
        status=status,
        config={},
    )


async def _post(app, *, cookie=None):
    transport = ASGITransport(app=app)
    cookies = {COOKIE_NAME: cookie} if cookie else None
    async with AsyncClient(
        transport=transport, base_url="https://widget", cookies=cookies,
    ) as c:
        return await c.post(
            "/v1/website/sessions",
            headers={"Authorization": f"Bearer {_KEY}", "Origin": _ORIGIN},
        )


async def test_single_session_reuses_cookie_bound_session():
    org_id, session_id = uuid.uuid4(), uuid.uuid4()
    store = _Store({session_id: _live_session(session_id)})
    app = _app(org_id, store, multi_session=False)

    r = await _post(app, cookie=_cookie(session_id, org_id))

    assert r.status_code == 200
    assert r.json()["session_id"] == str(session_id)
    assert store.created == []
    # A fresh cookie + CSRF token are minted for the reused session.
    assert COOKIE_NAME in r.cookies
    assert r.json()["csrf_token"]


async def test_single_session_without_cookie_creates():
    org_id = uuid.uuid4()
    store = _Store()
    app = _app(org_id, store, multi_session=False)

    r = await _post(app)

    assert r.status_code == 201
    assert len(store.created) == 1


async def test_single_session_ignores_cookie_from_other_key():
    org_id, session_id = uuid.uuid4(), uuid.uuid4()
    store = _Store({session_id: _live_session(session_id)})
    app = _app(org_id, store, multi_session=False)

    r = await _post(
        app,
        cookie=_cookie(session_id, org_id, key="surg_wk_other000000000000000"),
    )

    assert r.status_code == 201
    assert len(store.created) == 1
    assert r.json()["session_id"] != str(session_id)


async def test_single_session_skips_dead_session():
    org_id, session_id = uuid.uuid4(), uuid.uuid4()
    store = _Store({session_id: _live_session(session_id, status="failed")})
    app = _app(org_id, store, multi_session=False)

    r = await _post(app, cookie=_cookie(session_id, org_id))

    assert r.status_code == 201
    assert len(store.created) == 1


async def test_single_session_reuses_completed_session():
    org_id, session_id = uuid.uuid4(), uuid.uuid4()
    store = _Store({session_id: _live_session(session_id, status="completed")})
    app = _app(org_id, store, multi_session=False)

    r = await _post(app, cookie=_cookie(session_id, org_id))

    assert r.status_code == 200
    assert r.json()["session_id"] == str(session_id)
    assert store.created == []


async def test_multi_session_on_always_creates():
    org_id, session_id = uuid.uuid4(), uuid.uuid4()
    store = _Store({session_id: _live_session(session_id)})
    app = _app(org_id, store, multi_session=None)

    r = await _post(app, cookie=_cookie(session_id, org_id))

    assert r.status_code == 201
    assert len(store.created) == 1
