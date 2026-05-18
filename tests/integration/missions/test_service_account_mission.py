"""SA-principal /mission flow: a service-account-owned session must be
able to create, list, get, and detail-read its mission through the REST
API, while a user principal from the same org cannot see it.

This is the path the Surogate Ops Work UI takes in PROD: it authenticates
to surogates as a per-user service account, so every chat session has
``user_id=NULL`` and ``service_account_id=<sa>`` on the surogates side.
Missions created from that flow are SA-owned and reach the
``service_account_session`` JWT readers via the principal-aware
authorization predicate added in ``surogates.api.routes.missions``.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient

from surogates.missions.commands import handle_mission_create
from surogates.missions.store import MissionStore
from surogates.tenant.auth.jwt import (
    create_access_token,
    create_service_account_session_token,
)

from tests.integration.conftest import (
    create_org,
    create_user,
    issue_service_account_token,
)


@dataclass(frozen=True)
class SaSession:
    """Bundle of identifiers + JWT for an SA-owned chat session."""

    org_id: UUID
    service_account_id: UUID
    session_id: UUID
    token: str

    @property
    def auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}


async def _create_sa_session(
    session_factory, session_store, *, agent_id: str = "orchestrator",
) -> SaSession:
    """Provision a real (org, service_account, session) trio + a session-scoped JWT.

    Mirrors :func:`create_user_token_session` but for the SA principal
    shape produced by ops's Work UI in PROD. The session-scoped JWT is
    minted with :func:`create_service_account_session_token` — the same
    helper the worker uses when calling back into ``/v1/skills`` etc. —
    so the middleware's ``service_account_session`` validation path is
    actually exercised here, not bypassed.
    """
    org_id = await create_org(session_factory)
    issued = await issue_service_account_token(
        session_factory, org_id, name=f"ops-chat-sa-{uuid.uuid4()}",
    )
    sa_id = issued.id
    session = await session_store.create_session(
        user_id=None,
        org_id=org_id,
        agent_id=agent_id,
        channel="api",
        service_account_id=sa_id,
    )
    token = create_service_account_session_token(
        org_id=org_id,
        service_account_id=sa_id,
        session_id=session.id,
    )
    return SaSession(
        org_id=org_id,
        service_account_id=sa_id,
        session_id=session.id,
        token=token,
    )


def _redis_stub() -> AsyncMock:
    """Minimal AsyncMock matching ``handle_mission_create``'s redis usage."""
    return AsyncMock(zadd=AsyncMock())


async def _insert_sa_mission(
    *, session_factory, session_store, sa_session: SaSession,
    description: str = "Audit failing CI jobs",
    rubric: str = "Every red job is triaged with a ticket link.",
) -> UUID:
    """Drive the create-mission code path under an SA principal."""
    store = MissionStore(session_factory)
    result = await handle_mission_create(
        description=description,
        rubric=rubric,
        session_id=sa_session.session_id,
        service_account_id=sa_session.service_account_id,
        org_id=sa_session.org_id,
        agent_id="orchestrator",
        session_store=session_store,
        session_factory=session_factory,
        mission_store=store,
        redis=_redis_stub(),
    )
    assert result.ok is True, result.error
    assert result.mission_id is not None
    return result.mission_id


def _client(app: Any, headers: dict[str, str]) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers=headers,
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_sa_principal_can_get_its_mission(
    inbox_app, session_factory, session_store,
):
    """GET /v1/missions/{id} for an SA-owned mission returns the row when
    the request carries the matching ``service_account_session`` JWT."""
    sa = await _create_sa_session(session_factory, session_store)
    mission_id = await _insert_sa_mission(
        session_factory=session_factory, session_store=session_store,
        sa_session=sa,
    )

    async with _client(inbox_app, sa.auth_headers) as client:
        detail = await client.get(f"/v1/missions/{mission_id}")
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["id"] == str(mission_id)
    assert body["user_id"] is None
    assert body["service_account_id"] == str(sa.service_account_id)


@pytest.mark.asyncio(loop_scope="session")
async def test_sa_principal_lists_only_own_missions(
    inbox_app, session_factory, session_store,
):
    """GET /v1/missions is principal-scoped: the SA only sees its own
    rows, not other tenants' missions in the same DB."""
    sa = await _create_sa_session(session_factory, session_store)
    own_id = await _insert_sa_mission(
        session_factory=session_factory, session_store=session_store,
        sa_session=sa,
    )
    # A second SA in a different org — its mission must not appear.
    other_sa = await _create_sa_session(session_factory, session_store)
    other_id = await _insert_sa_mission(
        session_factory=session_factory, session_store=session_store,
        sa_session=other_sa, description="other-org", rubric="other-rubric",
    )

    async with _client(inbox_app, sa.auth_headers) as client:
        listing = await client.get("/v1/missions")
    assert listing.status_code == 200, listing.text
    ids = [m["id"] for m in listing.json()["missions"]]
    assert str(own_id) in ids
    assert str(other_id) not in ids


@pytest.mark.asyncio(loop_scope="session")
async def test_user_principal_cannot_see_sa_owned_mission(
    inbox_app, session_factory, session_store,
):
    """A user-principal tenant in the same org cannot read an SA-owned
    mission's detail — even given the id. The principal predicate
    matches on ``user_id`` for users; the SA row has ``user_id=None``."""
    sa = await _create_sa_session(session_factory, session_store)
    mission_id = await _insert_sa_mission(
        session_factory=session_factory, session_store=session_store,
        sa_session=sa,
    )

    # Mint a user in the SAME org as the SA's mission.
    user_id = await create_user(session_factory, sa.org_id)
    user_token = create_access_token(
        sa.org_id, user_id, {"sessions:read", "sessions:write"},
    )

    async with _client(
        inbox_app, {"Authorization": f"Bearer {user_token}"},
    ) as client:
        detail = await client.get(f"/v1/missions/{mission_id}")
    assert detail.status_code == 404, detail.text


@pytest.mark.asyncio(loop_scope="session")
async def test_user_principal_listing_excludes_sa_owned_missions(
    inbox_app, session_factory, session_store,
):
    """User-principal listing must not surface SA-owned missions, even
    in the same org. Mirrors the detail test but exercises the WHERE
    clause path."""
    sa = await _create_sa_session(session_factory, session_store)
    sa_mission_id = await _insert_sa_mission(
        session_factory=session_factory, session_store=session_store,
        sa_session=sa,
    )
    user_id = await create_user(session_factory, sa.org_id)
    user_token = create_access_token(
        sa.org_id, user_id, {"sessions:read", "sessions:write"},
    )

    async with _client(
        inbox_app, {"Authorization": f"Bearer {user_token}"},
    ) as client:
        listing = await client.get("/v1/missions")
    assert listing.status_code == 200, listing.text
    ids = [m["id"] for m in listing.json()["missions"]]
    assert str(sa_mission_id) not in ids
