"""Integration tests for auth.link_channel — binding a channel identity to the
logged-in real user, including taking over a Mate shadow identity.

link_channel only reads ``tenant.org_id`` / ``tenant.user_id`` and
``request.app.state.{pairing_store,session_factory}``, so we call it directly
with a SimpleNamespace request/tenant against a real Postgres + an in-memory
pairing store.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from surogates.api.routes.auth import LinkChannelRequest, link_channel
from surogates.channels.identity import get_or_create_channel_identity
from surogates.channels.pairing import PairingStore
from surogates.db.models import ChannelIdentity

from .conftest import create_org, create_user

pytestmark = pytest.mark.asyncio(loop_scope="session")


class _FakeRedis:
    def __init__(self):
        self.kv = {}

    async def get(self, k):
        return self.kv.get(k)

    async def exists(self, k):
        return 1 if k in self.kv else 0

    async def setex(self, k, ttl, v):
        self.kv[k] = v

    async def getdel(self, k):
        return self.kv.pop(k, None)


async def _mint(pairing, org_id, platform="slack", user="U_X"):
    return await pairing.create(str(org_id), platform, user, {})


def _request(pairing, session_factory):
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(
            pairing_store=pairing, session_factory=session_factory,
        )),
    )


async def _identity(session_factory, platform="slack", user="U_X"):
    async with session_factory() as db:
        return (
            await db.execute(
                select(ChannelIdentity)
                .where(ChannelIdentity.platform == platform)
                .where(ChannelIdentity.platform_user_id == user)
            )
        ).scalars().all()


async def test_rejects_code_from_a_different_org(session_factory):
    org_a = await create_org(session_factory)
    org_b = await create_org(session_factory)
    real_user = await create_user(session_factory, org_b)
    pairing = PairingStore(_FakeRedis())
    code = await _mint(pairing, org_a, user="U_MISMATCH")

    tenant = SimpleNamespace(org_id=org_b, user_id=real_user)
    with pytest.raises(HTTPException):
        await link_channel(LinkChannelRequest(code=code), _request(pairing, session_factory), tenant)
    assert await _identity(session_factory, user="U_MISMATCH") == [], "nothing created cross-org"
    # The code must NOT be consumed by a wrong-org attempt — it stays valid for
    # the correct org (otherwise a wrong-org paste griefs the real owner).
    assert await pairing.get(code) is not None, "wrong-org attempt must not burn the code"


async def test_creates_identity_when_none_exists(session_factory):
    org_id = await create_org(session_factory)
    real_user = await create_user(session_factory, org_id)
    pairing = PairingStore(_FakeRedis())
    code = await _mint(pairing, org_id, user="U_NEW")

    tenant = SimpleNamespace(org_id=org_id, user_id=real_user)
    await link_channel(LinkChannelRequest(code=code), _request(pairing, session_factory), tenant)

    rows = await _identity(session_factory, user="U_NEW")
    assert len(rows) == 1 and rows[0].user_id == real_user


async def test_repoints_a_shadow_identity_to_the_real_user(session_factory):
    org_id = await create_org(session_factory)
    real_user = await create_user(session_factory, org_id)
    # A Mate shadow identity already exists for this platform user.
    await get_or_create_channel_identity(
        session_factory, platform="slack", platform_user_id="U_SHADOW2",
        org_id=org_id, display_name="shadow",
    )
    pairing = PairingStore(_FakeRedis())
    code = await _mint(pairing, org_id, user="U_SHADOW2")

    tenant = SimpleNamespace(org_id=org_id, user_id=real_user)
    await link_channel(LinkChannelRequest(code=code), _request(pairing, session_factory), tenant)

    rows = await _identity(session_factory, user="U_SHADOW2")
    assert len(rows) == 1 and rows[0].user_id == real_user, "shadow re-pointed to the real user"


async def test_idempotent_when_already_linked_to_this_user(session_factory):
    org_id = await create_org(session_factory)
    real_user = await create_user(session_factory, org_id)
    async with session_factory() as db:
        db.add(ChannelIdentity(
            id=uuid.uuid4(), org_id=org_id, user_id=real_user,
            platform="slack", platform_user_id="U_ME",
        ))
        await db.commit()
    pairing = PairingStore(_FakeRedis())
    code = await _mint(pairing, org_id, user="U_ME")

    tenant = SimpleNamespace(org_id=org_id, user_id=real_user)
    await link_channel(LinkChannelRequest(code=code), _request(pairing, session_factory), tenant)  # no raise

    rows = await _identity(session_factory, user="U_ME")
    assert len(rows) == 1 and rows[0].user_id == real_user


async def test_conflicts_when_linked_to_a_different_real_user(session_factory):
    org_id = await create_org(session_factory)
    user_one = await create_user(session_factory, org_id)
    user_two = await create_user(session_factory, org_id)
    async with session_factory() as db:
        db.add(ChannelIdentity(
            id=uuid.uuid4(), org_id=org_id, user_id=user_one,
            platform="slack", platform_user_id="U_TAKEN",
        ))
        await db.commit()
    pairing = PairingStore(_FakeRedis())
    code = await _mint(pairing, org_id, user="U_TAKEN")

    tenant = SimpleNamespace(org_id=org_id, user_id=user_two)
    with pytest.raises(HTTPException):
        await link_channel(LinkChannelRequest(code=code), _request(pairing, session_factory), tenant)

    rows = await _identity(session_factory, user="U_TAKEN")
    assert rows[0].user_id == user_one, "unchanged on conflict"
