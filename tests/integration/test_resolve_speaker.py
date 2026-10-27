"""Who the agent is told it is speaking with, per session shape."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from surogates.orchestrator.worker import _resolve_speaker
from surogates.session.acting_principal import ActingPrincipal

from .conftest import create_org, create_user, issue_service_account_token

pytestmark = pytest.mark.asyncio(loop_scope="session")

_OPERATOR = {"operator": {"name": "Olga Operator", "email": "olga@ops.test"}}


def _session(config, service_account_id=None):
    return SimpleNamespace(config=config, service_account_id=service_account_id)


async def test_the_acting_user_is_named(session_store, session_factory):
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id, email="ana@example.com")

    speaker = await _resolve_speaker(
        _session({}), ActingPrincipal(user_id=user_id, service_account_id=None),
        session_store, session_factory,
    )

    assert speaker == (f"Test User {user_id}", "ana@example.com")


async def test_a_managed_channel_names_the_human_not_the_agent_account(
    session_store, session_factory,
):
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id, email="dm@example.com")
    agent_sa = await issue_service_account_token(session_factory, org_id, name="agent-sa")

    speaker = await _resolve_speaker(
        _session({}, agent_sa.id),
        ActingPrincipal(user_id=user_id, service_account_id=None),
        session_store, session_factory,
    )

    assert speaker[1] == "dm@example.com"


async def test_a_multi_party_thread_names_nobody(session_store, session_factory):
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)

    speaker = await _resolve_speaker(
        _session({"multi_party": True}),
        ActingPrincipal(user_id=user_id, service_account_id=None),
        session_store, session_factory,
    )

    assert speaker is None


async def test_an_ops_console_chat_names_the_stamped_operator(
    session_store, session_factory,
):
    org_id = await create_org(session_factory)
    ops_sa = await issue_service_account_token(
        session_factory, org_id, name=f"ops-chat-{org_id}-owner",
    )

    speaker = await _resolve_speaker(
        _session(_OPERATOR, ops_sa.id),
        ActingPrincipal(user_id=None, service_account_id=ops_sa.id),
        session_store, session_factory,
    )

    assert speaker == ("Olga Operator", "olga@ops.test")


async def test_an_operator_stamp_on_another_account_is_ignored(
    session_store, session_factory,
):
    """Any API caller can put ``operator`` in its session config; only the
    ops-chat account name proves surogate-ops wrote it."""
    org_id = await create_org(session_factory)
    partner = await issue_service_account_token(session_factory, org_id, name="partner")

    speaker = await _resolve_speaker(
        _session(_OPERATOR, partner.id),
        ActingPrincipal(user_id=None, service_account_id=partner.id),
        session_store, session_factory,
    )

    assert speaker is None


async def test_an_operator_stamp_cannot_rename_a_signed_in_user(
    session_store, session_factory,
):
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id, email="real@example.com")

    speaker = await _resolve_speaker(
        _session(_OPERATOR),
        ActingPrincipal(user_id=user_id, service_account_id=None),
        session_store, session_factory,
    )

    assert speaker[1] == "real@example.com"


async def test_an_ops_chat_without_a_stamp_names_nobody(session_store, session_factory):
    org_id = await create_org(session_factory)
    ops_sa = await issue_service_account_token(
        session_factory, org_id, name=f"ops-chat-{org_id}-owner",
    )

    speaker = await _resolve_speaker(
        _session({}, ops_sa.id),
        ActingPrincipal(user_id=None, service_account_id=ops_sa.id),
        session_store, session_factory,
    )

    assert speaker is None
