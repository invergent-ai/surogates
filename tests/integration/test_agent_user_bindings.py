"""Integration tests for the ``agent_users`` binding table.

Covers the idempotent enrollment helper, the two runtime write points
(session creation and web-channel login), the migrate-time backfill
from historical sessions, and the delete cascade from ``users``.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import text

pytestmark = pytest.mark.asyncio(loop_scope="session")

from sqlalchemy import select

from surogates.db.agent_users import (
    BACKFILL_SQL,
    SOURCE_LOGIN,
    SOURCE_MANUAL,
    SOURCE_SESSION,
    ensure_agent_user,
    remove_user_from_agent,
    user_visibility,
    visible_users_filter,
)
from surogates.db.models import User

from .conftest import create_org, create_user
from .test_api import TEST_AGENT_ID, _create_test_tenant, app, client  # noqa: F401


async def _bindings(session_factory, org_id, agent_id=None):
    async with session_factory() as db:
        query = (
            "SELECT agent_id, user_id, source FROM agent_users "
            "WHERE org_id = :org_id"
        )
        params: dict = {"org_id": org_id}
        if agent_id is not None:
            query += " AND agent_id = :agent_id"
            params["agent_id"] = agent_id
        rows = await db.execute(text(query + " ORDER BY created_at"), params)
        return [tuple(r) for r in rows]


async def test_ensure_agent_user_idempotent_first_source_wins(session_factory):
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)

    async with session_factory() as db:
        await ensure_agent_user(
            db, org_id=org_id, agent_id="agent-a",
            user_id=user_id, source=SOURCE_MANUAL,
        )
        await db.commit()
    async with session_factory() as db:
        await ensure_agent_user(
            db, org_id=org_id, agent_id="agent-a",
            user_id=user_id, source=SOURCE_SESSION,
        )
        # Same user on a second agent is a separate, legitimate binding.
        await ensure_agent_user(
            db, org_id=org_id, agent_id="agent-b",
            user_id=user_id, source=SOURCE_SESSION,
        )
        await db.commit()

    rows = await _bindings(session_factory, org_id)
    assert rows == [
        ("agent-a", user_id, SOURCE_MANUAL),
        ("agent-b", user_id, SOURCE_SESSION),
    ]


async def test_create_session_enrolls_interactive_user_only(
    session_store, session_factory,
):
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)

    await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id="agent-web", channel="web",
    )
    # Service-account/api sessions carry no end-user — no binding.
    await session_store.create_session(
        user_id=None, org_id=org_id, agent_id="agent-api", channel="api",
    )

    rows = await _bindings(session_factory, org_id)
    assert rows == [("agent-web", user_id, SOURCE_SESSION)]

    # A second session on the same agent stays one binding.
    await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id="agent-web", channel="web",
    )
    assert await _bindings(session_factory, org_id) == rows


async def test_login_enrolls_user_on_serving_agent(
    client: AsyncClient, session_factory,
):
    org_id, user_id, _, email = await _create_test_tenant(
        session_factory, password="bind-me-123",
    )

    resp = await client.post(
        "/v1/auth/login",
        json={"email": email, "password": "bind-me-123", "org_id": str(org_id)},
    )
    assert resp.status_code == 200

    rows = await _bindings(session_factory, org_id, agent_id=TEST_AGENT_ID)
    assert rows == [(TEST_AGENT_ID, user_id, SOURCE_LOGIN)]

    # A repeat login stays idempotent.
    resp = await client.post(
        "/v1/auth/login",
        json={"email": email, "password": "bind-me-123", "org_id": str(org_id)},
    )
    assert resp.status_code == 200
    assert await _bindings(session_factory, org_id, agent_id=TEST_AGENT_ID) == rows


async def test_backfill_derives_bindings_from_sessions(
    session_store, session_factory,
):
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    other_user = await create_user(session_factory, org_id)

    # Suppress live enrollment to simulate a pre-binding database, then
    # let the backfill derive the rows from the session history alone.
    async with session_factory() as db:
        for agent_id, uid in [
            ("agent-1", user_id),
            ("agent-1", user_id),  # second session, same pair
            ("agent-2", other_user),
        ]:
            await db.execute(
                text(
                    "INSERT INTO sessions (id, org_id, agent_id, user_id, "
                    "channel, status) VALUES (:id, :org, :agent, :uid, "
                    "'web', 'completed')"
                ),
                {
                    "id": uuid.uuid4(), "org": org_id,
                    "agent": agent_id, "uid": uid,
                },
            )
        # Service-account style row — must not produce a binding.
        await db.execute(
            text(
                "INSERT INTO sessions (id, org_id, agent_id, user_id, "
                "channel, status) VALUES (:id, :org, 'agent-3', NULL, "
                "'api', 'completed')"
            ),
            {"id": uuid.uuid4(), "org": org_id},
        )
        await db.commit()

    async with session_factory() as db:
        await db.execute(text(BACKFILL_SQL))
        await db.execute(text(BACKFILL_SQL))  # idempotent re-run
        await db.commit()

    rows = await _bindings(session_factory, org_id)
    assert sorted(rows) == sorted([
        ("agent-1", user_id, "backfill"),
        ("agent-2", other_user, "backfill"),
    ])


async def test_visible_users_filter_bound_plus_orphans(session_factory):
    org_id = await create_org(session_factory)
    bound_a = await create_user(session_factory, org_id)
    bound_b = await create_user(session_factory, org_id)
    orphan = await create_user(session_factory, org_id)

    async with session_factory() as db:
        await ensure_agent_user(
            db, org_id=org_id, agent_id="agent-a",
            user_id=bound_a, source=SOURCE_MANUAL,
        )
        await ensure_agent_user(
            db, org_id=org_id, agent_id="agent-b",
            user_id=bound_b, source=SOURCE_MANUAL,
        )
        await db.commit()

    async with session_factory() as db:
        visible = (
            (
                await db.execute(
                    select(User.id).where(
                        User.org_id == org_id,
                        visible_users_filter(org_id, "agent-a"),
                    )
                )
            )
            .scalars()
            .all()
        )
    # Agent A sees its own user and the unassigned orphan — but never
    # agent B's user.
    assert sorted(map(str, visible)) == sorted(map(str, [bound_a, orphan]))


async def test_user_visibility_states(session_factory):
    org_id = await create_org(session_factory)
    bound = await create_user(session_factory, org_id)
    orphan = await create_user(session_factory, org_id)

    async with session_factory() as db:
        await ensure_agent_user(
            db, org_id=org_id, agent_id="agent-a",
            user_id=bound, source=SOURCE_MANUAL,
        )
        await db.commit()

    async with session_factory() as db:
        assert await user_visibility(
            db, org_id=org_id, agent_id="agent-a", user_id=bound,
        ) == "bound"
        # Another agent's tab must not see agent A's user.
        assert await user_visibility(
            db, org_id=org_id, agent_id="agent-b", user_id=bound,
        ) is None
        assert await user_visibility(
            db, org_id=org_id, agent_id="agent-a", user_id=orphan,
        ) == "orphan"
        assert await user_visibility(
            db, org_id=org_id, agent_id="agent-a", user_id=uuid.uuid4(),
        ) is None


async def test_remove_user_from_agent_semantics(session_factory):
    org_id = await create_org(session_factory)
    shared = await create_user(session_factory, org_id)
    orphan = await create_user(session_factory, org_id)

    async with session_factory() as db:
        for agent in ("agent-a", "agent-b"):
            await ensure_agent_user(
                db, org_id=org_id, agent_id=agent,
                user_id=shared, source=SOURCE_MANUAL,
            )
        await db.commit()

    async def _user_exists(uid):
        async with session_factory() as db:
            return (
                await db.execute(select(User.id).where(User.id == uid))
            ).scalar_one_or_none() is not None

    # Removing from one agent unbinds but the shared account survives.
    async with session_factory() as db:
        assert await remove_user_from_agent(
            db, org_id=org_id, agent_id="agent-a", user_id=shared,
        )
        await db.commit()
    assert await _user_exists(shared)
    assert await _bindings(session_factory, org_id, agent_id="agent-a") == []

    # A tab the user isn't visible from cannot remove them.
    async with session_factory() as db:
        assert not await remove_user_from_agent(
            db, org_id=org_id, agent_id="agent-a", user_id=shared,
        )

    # Removing the last binding deletes the account.
    async with session_factory() as db:
        assert await remove_user_from_agent(
            db, org_id=org_id, agent_id="agent-b", user_id=shared,
        )
        await db.commit()
    assert not await _user_exists(shared)

    # An orphan is removable from any tab — plain account delete.
    async with session_factory() as db:
        assert await remove_user_from_agent(
            db, org_id=org_id, agent_id="agent-a", user_id=orphan,
        )
        await db.commit()
    assert not await _user_exists(orphan)


async def test_user_delete_cascades_bindings(session_factory):
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    async with session_factory() as db:
        await ensure_agent_user(
            db, org_id=org_id, agent_id="agent-a",
            user_id=user_id, source=SOURCE_MANUAL,
        )
        await db.commit()

    async with session_factory() as db:
        await db.execute(
            text("DELETE FROM users WHERE id = :id"), {"id": user_id},
        )
        await db.commit()

    assert await _bindings(session_factory, org_id) == []
