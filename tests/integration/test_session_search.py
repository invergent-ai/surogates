"""Integration tests for the session_search builtin."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from surogates.session.events import EventType
from surogates.tools.builtin.session_search import _session_search_handler

from .conftest import create_org, create_user, issue_service_account_token

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _seed_student_session(
    session_store, session_factory, org_id, agent_id, content,
):
    """Create a user + a session owned by that user with one message."""
    user_id = await create_user(session_factory, org_id)
    session = await session_store.create_session(
        user_id=user_id,
        org_id=org_id,
        agent_id=agent_id,
        channel="web",
    )
    await session_store.emit_event(
        session.id, EventType.USER_MESSAGE, {"content": content},
    )
    return user_id, session


async def test_session_search_accepts_service_account_principal(
    session_store, session_factory,
):
    """API sessions should search prior sessions for the same service account."""
    org_id = await create_org(session_factory)
    issued = await issue_service_account_token(session_factory, org_id)

    session = await session_store.create_session(
        user_id=None,
        service_account_id=issued.id,
        org_id=org_id,
        agent_id="agent-a",
        channel="api",
    )
    await session_store.emit_event(
        session.id,
        EventType.USER_MESSAGE,
        {"content": "session 2 was about deployment notes"},
    )

    result = json.loads(
        await _session_search_handler(
            {"query": "deployment notes"},
            session_store=session_store,
            tenant=SimpleNamespace(
                org_id=org_id,
                user_id=None,
                service_account_id=issued.id,
            ),
            agent_id="agent-a",
        )
    )

    assert result["success"] is True
    assert result["count"] == 1
    assert result["results"][0]["session_id"] == str(session.id)


async def test_owner_scoped_search_sees_end_user_sessions(
    session_store, session_factory,
):
    """An ops owner-console session (SA principal + ops config stamp)
    searches across every principal's sessions for the same org + agent."""
    org_id = await create_org(session_factory)
    other_org = await create_org(session_factory)
    issued = await issue_service_account_token(
        session_factory, org_id, name=f"ops-chat-{org_id}-owner",
    )

    _, student_session = await _seed_student_session(
        session_store, session_factory, org_id, "agent-a",
        "the magnesium electron configuration is 2 8 2",
    )
    # Same org, different agent -- must stay invisible.
    await _seed_student_session(
        session_store, session_factory, org_id, "agent-b",
        "the magnesium electron configuration is 2 8 2",
    )
    # Different org entirely -- must stay invisible.
    await _seed_student_session(
        session_store, session_factory, other_org, "agent-a",
        "the magnesium electron configuration is 2 8 2",
    )

    result = json.loads(
        await _session_search_handler(
            {"query": "magnesium"},
            session_store=session_store,
            tenant=SimpleNamespace(
                org_id=org_id, user_id=None, service_account_id=issued.id,
            ),
            agent_id="agent-a",
            session_config={"ops": {"user_id": "11111111-1111-1111-1111-111111111111"}},
        )
    )

    assert result["success"] is True
    assert result["count"] == 1
    assert result["results"][0]["session_id"] == str(student_session.id)


async def test_forged_ops_config_on_user_principal_stays_scoped(
    session_store, session_factory,
):
    """A user-principal session cannot widen its scope by stamping an
    ``ops`` block into its own config -- the gate requires a
    service-account principal, which end-user surfaces never get."""
    org_id = await create_org(session_factory)

    attacker_id, attacker_session = await _seed_student_session(
        session_store, session_factory, org_id, "agent-a",
        "attacker asking about magnesium",
    )
    _, victim_session = await _seed_student_session(
        session_store, session_factory, org_id, "agent-a",
        "victim discussing magnesium secrets",
    )

    result = json.loads(
        await _session_search_handler(
            {"query": "magnesium"},
            session_store=session_store,
            tenant=SimpleNamespace(
                org_id=org_id, user_id=attacker_id, service_account_id=None,
            ),
            agent_id="agent-a",
            session_config={"ops": {"user_id": str(attacker_id)}},
        )
    )

    assert result["success"] is True
    session_ids = {r["session_id"] for r in result["results"]}
    assert str(victim_session.id) not in session_ids
    assert session_ids == {str(attacker_session.id)}


async def test_service_account_without_ops_stamp_stays_scoped(
    session_store, session_factory,
):
    """A service-account principal without the ops config stamp keeps
    the existing behaviour: only its own sessions."""
    org_id = await create_org(session_factory)
    issued = await issue_service_account_token(session_factory, org_id)

    own_session = await session_store.create_session(
        user_id=None,
        service_account_id=issued.id,
        org_id=org_id,
        agent_id="agent-a",
        channel="api",
    )
    await session_store.emit_event(
        own_session.id, EventType.USER_MESSAGE,
        {"content": "service account notes about magnesium"},
    )
    await _seed_student_session(
        session_store, session_factory, org_id, "agent-a",
        "student session about magnesium",
    )

    result = json.loads(
        await _session_search_handler(
            {"query": "magnesium"},
            session_store=session_store,
            tenant=SimpleNamespace(
                org_id=org_id, user_id=None, service_account_id=issued.id,
            ),
            agent_id="agent-a",
        )
    )

    assert result["success"] is True
    assert result["count"] == 1
    assert result["results"][0]["session_id"] == str(own_session.id)


async def test_owner_scoped_recent_mode_lists_end_user_sessions(
    session_store, session_factory,
):
    """Empty-query (recent) mode under owner scope lists end-user
    sessions and carries each session's user_id so the owner can tell
    principals apart."""
    org_id = await create_org(session_factory)
    issued = await issue_service_account_token(
        session_factory, org_id, name=f"ops-chat-{org_id}-owner",
    )

    student_id, student_session = await _seed_student_session(
        session_store, session_factory, org_id, "agent-a",
        "hello I want to learn chemistry",
    )

    result = json.loads(
        await _session_search_handler(
            {},
            session_store=session_store,
            tenant=SimpleNamespace(
                org_id=org_id, user_id=None, service_account_id=issued.id,
            ),
            agent_id="agent-a",
            session_config={"ops": {"user_id": "11111111-1111-1111-1111-111111111111"}},
        )
    )

    assert result["success"] is True
    assert result["mode"] == "recent"
    by_id = {r["session_id"]: r for r in result["results"]}
    assert str(student_session.id) in by_id
    assert by_id[str(student_session.id)]["user_id"] == str(student_id)


async def test_forged_ops_config_on_plain_service_account_stays_scoped(
    session_store, session_factory,
):
    """An org API service account (not an ops live-chat account) cannot
    widen its scope by stamping a forged ``ops`` block into its own
    session config -- the gate also requires the ops-chat account-name
    prefix that only surogate-ops's forwarder uses."""
    org_id = await create_org(session_factory)
    issued = await issue_service_account_token(
        session_factory, org_id, name="partner-integration",
    )

    own_session = await session_store.create_session(
        user_id=None,
        service_account_id=issued.id,
        org_id=org_id,
        agent_id="agent-a",
        channel="api",
    )
    await session_store.emit_event(
        own_session.id, EventType.USER_MESSAGE,
        {"content": "integration notes about magnesium"},
    )
    await _seed_student_session(
        session_store, session_factory, org_id, "agent-a",
        "student secrets about magnesium",
    )

    result = json.loads(
        await _session_search_handler(
            {"query": "magnesium"},
            session_store=session_store,
            tenant=SimpleNamespace(
                org_id=org_id, user_id=None, service_account_id=issued.id,
            ),
            agent_id="agent-a",
            session_config={"ops": {"user_id": "forged"}},
        )
    )

    assert result["success"] is True
    assert result["count"] == 1
    assert result["results"][0]["session_id"] == str(own_session.id)


async def test_system_agent_session_stays_scoped(
    session_store, session_factory,
):
    """System-agent console sessions (config.agent_type stamped by
    surogate-ops) stay private per user -- owner scope must not apply,
    matching the ops Sessions Explorer's forced scope='mine'."""
    org_id = await create_org(session_factory)
    issued = await issue_service_account_token(
        session_factory, org_id, name=f"ops-chat-{org_id}-member",
    )

    own_session = await session_store.create_session(
        user_id=None,
        service_account_id=issued.id,
        org_id=org_id,
        agent_id="copilot",
        channel="web",
    )
    await session_store.emit_event(
        own_session.id, EventType.USER_MESSAGE,
        {"content": "my private copilot chat about magnesium"},
    )
    other = await issue_service_account_token(
        session_factory, org_id, name=f"ops-chat-{org_id}-other",
    )
    other_session = await session_store.create_session(
        user_id=None,
        service_account_id=other.id,
        org_id=org_id,
        agent_id="copilot",
        channel="web",
    )
    await session_store.emit_event(
        other_session.id, EventType.USER_MESSAGE,
        {"content": "someone else's private copilot chat about magnesium"},
    )

    result = json.loads(
        await _session_search_handler(
            {"query": "magnesium"},
            session_store=session_store,
            tenant=SimpleNamespace(
                org_id=org_id, user_id=None, service_account_id=issued.id,
            ),
            agent_id="copilot",
            session_config={
                "ops": {"user_id": "11111111-1111-1111-1111-111111111111"},
                "agent_type": "surogate-platform-copilot",
            },
        )
    )

    assert result["success"] is True
    assert result["count"] == 1
    assert result["results"][0]["session_id"] == str(own_session.id)
