"""resolve_acting_principal reads the latest stamped USER_MESSAGE, else owner."""

from __future__ import annotations

from uuid import uuid4

import pytest

from surogates.session.acting_principal import ActingPrincipal
from surogates.session.events import EventType
from tests.integration.conftest import create_org, create_user

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _new_session(session_store, owner, org_id):
    sid = uuid4()
    await session_store.create_session(
        session_id=sid,
        user_id=owner,
        org_id=org_id,
        agent_id="a1",
        channel="slack",
        config={},
    )
    return sid


async def test_latest_stamp_wins_over_owner(session_store, session_factory):
    org_id = await create_org(session_factory)
    owner = await create_user(session_factory, org_id)
    sender = await create_user(session_factory, org_id)
    sid = await _new_session(session_store, owner, org_id)

    await session_store.emit_event(sid, EventType.USER_MESSAGE, {
        "content": "hi", "principal_user_id": str(sender),
    })
    got = await session_store.resolve_acting_principal(sid)
    assert got == ActingPrincipal(user_id=sender, service_account_id=None)


async def test_falls_back_to_owner_when_unstamped(session_store, session_factory):
    org_id = await create_org(session_factory)
    owner = await create_user(session_factory, org_id)
    sid = await _new_session(session_store, owner, org_id)

    await session_store.emit_event(sid, EventType.USER_MESSAGE, {"content": "hi"})
    got = await session_store.resolve_acting_principal(sid)
    assert got == ActingPrincipal(user_id=owner, service_account_id=None)


async def test_ignores_synthetic_user_messages(session_store, session_factory):
    org_id = await create_org(session_factory)
    owner = await create_user(session_factory, org_id)
    sender = await create_user(session_factory, org_id)
    sid = await _new_session(session_store, owner, org_id)

    await session_store.emit_event(sid, EventType.USER_MESSAGE, {
        "content": "real", "principal_user_id": str(sender),
    })
    await session_store.emit_event(sid, EventType.USER_MESSAGE, {
        "content": "ambient", "synthetic": "ambient",
    })
    got = await session_store.resolve_acting_principal(sid)
    assert got == ActingPrincipal(user_id=sender, service_account_id=None)


async def test_many_synthetic_do_not_shadow_real_sender(session_store, session_factory):
    # A long synthetic burst (e.g. a mission/outcome loop) must not shadow the
    # last real human sender — the SQL synthetic filter has no bounded window.
    org_id = await create_org(session_factory)
    owner = await create_user(session_factory, org_id)
    sender = await create_user(session_factory, org_id)
    sid = await _new_session(session_store, owner, org_id)

    await session_store.emit_event(sid, EventType.USER_MESSAGE, {
        "content": "real", "principal_user_id": str(sender),
    })
    for i in range(30):
        await session_store.emit_event(sid, EventType.USER_MESSAGE, {
            "content": f"loop {i}", "synthetic": "outcome_continuation",
        })
    got = await session_store.resolve_acting_principal(sid)
    assert got == ActingPrincipal(user_id=sender, service_account_id=None)


async def test_for_session_variant_matches(session_store, session_factory):
    org_id = await create_org(session_factory)
    owner = await create_user(session_factory, org_id)
    sender = await create_user(session_factory, org_id)
    sid = await _new_session(session_store, owner, org_id)

    await session_store.emit_event(sid, EventType.USER_MESSAGE, {
        "content": "hi", "principal_user_id": str(sender),
    })
    session = await session_store.get_session(sid)
    got = await session_store.resolve_acting_principal_for_session(session)
    assert got == ActingPrincipal(user_id=sender, service_account_id=None)
