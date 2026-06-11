"""GET /v1/sessions/{id}/board."""
from __future__ import annotations

import uuid

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from surogates.api.routes import board as board_routes
from surogates.board.store import BoardStore
from surogates.tenant.auth.middleware import get_current_tenant


class _FakeTenant:
    """Covers every session in its org (route only calls owns_session)."""

    def __init__(self, org_id):
        self.org_id = org_id

    def owns_session(self, session_org_id, session_id):
        return session_org_id == self.org_id


async def _passthrough(drafts):
    return list(drafts), []


def _make_app(*, tenant, session_store, session_factory):
    app = FastAPI()
    app.state.session_store = session_store
    app.state.session_factory = session_factory
    app.dependency_overrides[get_current_tenant] = lambda: tenant
    app.include_router(board_routes.router, prefix="/v1")
    return app


def _client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio(loop_scope="session")
async def test_board_route_returns_notes_and_render(
    parent_session, session_factory, session_store, org_id,
):
    group_id = parent_session.id
    parent_session.config["context_group_id"] = str(group_id)
    await session_store.update_session_config_key(
        parent_session.id, "context_group_id", str(group_id),
    )
    board = BoardStore(session_factory)
    await board.admit(
        raw_notes=[
            {"type": "FACT", "content": "route test fact api.py:1"},
            {"type": "FAIL", "content": "route test dead end api.py:2"},
        ],
        org_id=org_id, group_id=group_id,
        writer_session_id=parent_session.id, writer_label="coord",
        verifier=_passthrough,
        max_claims_per_writer=2, max_notes_per_group=300,
        claim_ttl_seconds=300,
    )

    app = _make_app(
        tenant=_FakeTenant(org_id),
        session_store=session_store,
        session_factory=session_factory,
    )
    async with _client(app) as client:
        resp = await client.get(f"/v1/sessions/{parent_session.id}/board")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["group_id"] == str(group_id)
    assert len(body["notes"]) == 2
    assert {n["type"] for n in body["notes"]} == {"FACT", "FAIL"}
    assert "route test fact" in body["render"]


@pytest.mark.asyncio(loop_scope="session")
async def test_board_route_404_when_no_group(
    parent_session, session_factory, session_store, org_id,
):
    app = _make_app(
        tenant=_FakeTenant(org_id),
        session_store=session_store,
        session_factory=session_factory,
    )
    async with _client(app) as client:
        resp = await client.get(f"/v1/sessions/{parent_session.id}/board")
    assert resp.status_code == 404
    assert "coordination-group" in resp.json()["detail"]


@pytest.mark.asyncio(loop_scope="session")
async def test_board_route_404_cross_org_and_missing(
    parent_session, session_factory, session_store,
):
    app = _make_app(
        tenant=_FakeTenant(uuid.uuid4()),  # different org
        session_store=session_store,
        session_factory=session_factory,
    )
    async with _client(app) as client:
        resp = await client.get(f"/v1/sessions/{parent_session.id}/board")
        assert resp.status_code == 404
        resp2 = await client.get(f"/v1/sessions/{uuid.uuid4()}/board")
        assert resp2.status_code == 404
