"""Integration tests for auto-provisioning channel identities.

A channel participant (e.g. a Slack user in a team channel) must NOT have to
create a Surogate platform account or complete a pairing handshake before the
agent will talk to them.  On first contact the pipeline get-or-creates a
lightweight external ``User`` (``auth_provider=<platform>``,
``external_id=<platform_user_id>``) scoped to the agent's org, idempotently,
and links a ``channel_identities`` row to it.

These run against a real Postgres (testcontainers) because the behaviour under
test is the DB get-or-create and its unique constraints — the exact thing a
mock would paper over.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from surogates.channels.identity import (
    ResolvedIdentity,
    get_or_create_channel_identity,
)
from surogates.db.models import ChannelIdentity, User

from .conftest import create_org, create_user

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_provisions_external_user_and_identity_for_unknown_sender(session_factory):
    org_id = await create_org(session_factory)

    result = await get_or_create_channel_identity(
        session_factory,
        platform="slack",
        platform_user_id="U_UNKNOWN_1",
        org_id=org_id,
        display_name="Flavius",
    )

    assert isinstance(result, ResolvedIdentity)
    assert result.org_id == org_id
    assert result.platform == "slack"
    assert result.platform_user_id == "U_UNKNOWN_1"

    async with session_factory() as db:
        user = (
            await db.execute(select(User).where(User.id == result.user_id))
        ).scalar_one()
        assert user.org_id == org_id
        assert user.auth_provider == "slack"
        assert user.external_id == "U_UNKNOWN_1"
        assert user.display_name == "Flavius"
        assert user.email  # NOT NULL satisfied by a synthetic address

        ident = (
            await db.execute(
                select(ChannelIdentity)
                .where(ChannelIdentity.platform == "slack")
                .where(ChannelIdentity.platform_user_id == "U_UNKNOWN_1")
            )
        ).scalar_one()
        assert ident.user_id == result.user_id


async def test_idempotent_second_call_returns_same_user_no_duplicates(session_factory):
    org_id = await create_org(session_factory)

    first = await get_or_create_channel_identity(
        session_factory,
        platform="telegram",
        platform_user_id="@dup_bot_user",
        org_id=org_id,
        display_name="Dup",
    )
    second = await get_or_create_channel_identity(
        session_factory,
        platform="telegram",
        platform_user_id="@dup_bot_user",
        org_id=org_id,
        display_name="Dup (renamed)",
    )

    assert second.user_id == first.user_id

    async with session_factory() as db:
        users = (
            await db.execute(
                select(User)
                .where(User.auth_provider == "telegram")
                .where(User.external_id == "@dup_bot_user")
            )
        ).scalars().all()
        assert len(users) == 1
        idents = (
            await db.execute(
                select(ChannelIdentity)
                .where(ChannelIdentity.platform == "telegram")
                .where(ChannelIdentity.platform_user_id == "@dup_bot_user")
            )
        ).scalars().all()
        assert len(idents) == 1


async def test_same_platform_user_in_two_orgs_gets_separate_identities(session_factory):
    """Tenant isolation: the same platform user id provisioned under two
    different orgs yields two distinct users/identities — the second org must
    NOT resolve to the first org's user (which would attribute org B's session
    to an org A user)."""
    org_a = await create_org(session_factory)
    org_b = await create_org(session_factory)

    ra = await get_or_create_channel_identity(
        session_factory, platform="slack", platform_user_id="U_SHARED",
        org_id=org_a, display_name="in A",
    )
    rb = await get_or_create_channel_identity(
        session_factory, platform="slack", platform_user_id="U_SHARED",
        org_id=org_b, display_name="in B",
    )

    assert ra.org_id == org_a
    assert rb.org_id == org_b, "second org must get its OWN identity, not org A's"
    assert ra.user_id != rb.user_id, "distinct users per org"


async def test_returns_existing_identity_without_creating_a_new_user(session_factory):
    org_id = await create_org(session_factory)
    existing_user_id = await create_user(session_factory, org_id)

    async with session_factory() as db:
        db.add(
            ChannelIdentity(
                id=uuid.uuid4(),
                org_id=org_id,
                user_id=existing_user_id,
                platform="slack",
                platform_user_id="U_ALREADY_LINKED",
            )
        )
        await db.commit()

    result = await get_or_create_channel_identity(
        session_factory,
        platform="slack",
        platform_user_id="U_ALREADY_LINKED",
        org_id=org_id,
        display_name="ignored — already linked",
    )

    assert result.user_id == existing_user_id

    async with session_factory() as db:
        # No second (auto-provisioned) user was created for this sender.
        external = (
            await db.execute(
                select(User)
                .where(User.auth_provider == "slack")
                .where(User.external_id == "U_ALREADY_LINKED")
            )
        ).scalars().all()
        assert external == []


async def test_resolve_real_identity_excludes_shadow_users(session_factory):
    """A shadow identity (auth_provider == platform) is not a linked real user."""
    org_id = await create_org(session_factory)
    await get_or_create_channel_identity(
        session_factory, platform="slack", platform_user_id="U_SHADOW",
        org_id=org_id, display_name="Shadow",
    )
    from surogates.channels.identity import resolve_real_identity
    assert await resolve_real_identity(
        session_factory, "slack", "U_SHADOW", org_id=org_id
    ) is None


async def test_resolve_real_identity_finds_real_user(session_factory):
    """A channel identity pointing at a real (database) user resolves."""
    from surogates.channels.identity import resolve_real_identity
    org_id = await create_org(session_factory)
    real_user_id = await create_user(session_factory, org_id)  # auth_provider defaults to "database"
    async with session_factory() as db:
        db.add(ChannelIdentity(
            id=uuid.uuid4(), org_id=org_id, user_id=real_user_id,
            platform="slack", platform_user_id="U_REAL",
        ))
        await db.commit()
    r = await resolve_real_identity(session_factory, "slack", "U_REAL", org_id=org_id)
    assert r is not None and r.user_id == real_user_id
