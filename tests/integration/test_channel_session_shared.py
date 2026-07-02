"""A Slack thread is one shared session across all participants.

Query-boundary test (real Postgres via testcontainers): two different users
with the SAME channel routing key must resolve to the SAME session, because
get_or_create_channel_session matches on the key, not the user. DMs (distinct
keys), different orgs, different agents, and different platform channels must
still isolate.
"""

from __future__ import annotations

import pytest

from surogates.channels.identity import get_or_create_channel_session
from tests.integration.conftest import create_org, create_user

# The session/session_factory fixtures are bound to the session-scoped
# testcontainers event loop; tests in this module must run on that same
# loop (see tests/integration/test_delivery.py for the same convention).
pytestmark = pytest.mark.asyncio(loop_scope="session")

_AGENT = "flavius/default"
_OTHER_AGENT = "flavius/other"


async def _resolve(
    session_store,
    session_factory,
    *,
    key,
    user_id,
    org_id,
    agent_id=_AGENT,
    channel="slack",
):
    return await get_or_create_channel_session(
        session_store,
        None,  # redis unused on the resolution path; caller enqueues
        session_key=key,
        user_id=user_id,
        org_id=org_id,
        agent_id=agent_id,
        channel=channel,
        config={"slack_channel_id": "C123"},
        session_factory=session_factory,
    )


async def test_two_users_same_thread_key_share_one_session(session_store, session_factory):
    org_id = await create_org(session_factory)
    user_a = await create_user(session_factory, org_id)
    user_b = await create_user(session_factory, org_id)
    key = "agent:slack:group:C123:1700000000.000100"

    sid_a = await _resolve(session_store, session_factory, key=key, user_id=user_a, org_id=org_id)
    sid_b = await _resolve(session_store, session_factory, key=key, user_id=user_b, org_id=org_id)

    assert sid_b == sid_a  # same thread -> same session, regardless of sender


async def test_distinct_dm_keys_stay_separate(session_store, session_factory):
    org_id = await create_org(session_factory)
    user_a = await create_user(session_factory, org_id)
    user_b = await create_user(session_factory, org_id)

    sid_a = await _resolve(
        session_store, session_factory,
        key="agent:slack:dm:D_AAA", user_id=user_a, org_id=org_id,
    )
    sid_b = await _resolve(
        session_store, session_factory,
        key="agent:slack:dm:D_BBB", user_id=user_b, org_id=org_id,
    )

    assert sid_b != sid_a  # different DM conversations -> different sessions


async def test_same_key_different_org_does_not_collide(session_store, session_factory):
    org_1 = await create_org(session_factory)
    org_2 = await create_org(session_factory)
    user_1 = await create_user(session_factory, org_1)
    user_2 = await create_user(session_factory, org_2)
    key = "agent:slack:group:C999:1700000000.000200"

    sid_1 = await _resolve(session_store, session_factory, key=key, user_id=user_1, org_id=org_1)
    sid_2 = await _resolve(session_store, session_factory, key=key, user_id=user_2, org_id=org_2)

    assert sid_2 != sid_1  # org-scoped: same key in another org is a fresh session


async def test_same_key_different_agent_does_not_collide(session_store, session_factory):
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    key = "agent:slack:group:C777:1700000000.000300"

    sid_1 = await _resolve(
        session_store, session_factory,
        key=key, user_id=user_id, org_id=org_id, agent_id=_AGENT,
    )
    sid_2 = await _resolve(
        session_store, session_factory,
        key=key, user_id=user_id, org_id=org_id, agent_id=_OTHER_AGENT,
    )

    assert sid_2 != sid_1  # agent-scoped: another agent gets its own session


async def test_same_key_different_channel_does_not_collide(session_store, session_factory):
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    key = "agent:slack:group:C888:1700000000.000400"

    sid_slack = await _resolve(
        session_store, session_factory,
        key=key, user_id=user_id, org_id=org_id, channel="slack",
    )
    sid_telegram = await _resolve(
        session_store, session_factory,
        key=key, user_id=user_id, org_id=org_id, channel="telegram",
    )

    assert sid_telegram != sid_slack  # channel-scoped: platform remains isolated
