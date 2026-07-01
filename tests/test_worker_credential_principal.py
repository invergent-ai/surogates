from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from surogates.orchestrator.worker import resolve_credential_principal
from surogates.session.acting_principal import ActingPrincipal


def _session(channel: str):
    return SimpleNamespace(channel=channel)


def test_slack_session_with_agent_sa_uses_agent_service_account():
    acting = ActingPrincipal(user_id=uuid4(), service_account_id=None)
    agent_principal = SimpleNamespace(id=uuid4())

    got = resolve_credential_principal(
        session=_session("slack"),
        acting=acting,
        agent_principal=agent_principal,
    )

    assert got == ActingPrincipal(
        user_id=None,
        service_account_id=agent_principal.id,
    )


def test_telegram_session_with_agent_sa_uses_agent_service_account():
    acting = ActingPrincipal(user_id=uuid4(), service_account_id=None)
    agent_principal = SimpleNamespace(id=uuid4())

    got = resolve_credential_principal(
        session=_session("telegram"),
        acting=acting,
        agent_principal=agent_principal,
    )

    assert got == ActingPrincipal(
        user_id=None,
        service_account_id=agent_principal.id,
    )


def test_managed_channel_without_agent_sa_falls_back_to_acting():
    acting = ActingPrincipal(user_id=uuid4(), service_account_id=None)

    got = resolve_credential_principal(
        session=_session("slack"),
        acting=acting,
        agent_principal=None,
    )

    assert got == acting


def test_web_session_never_switches_to_agent_sa():
    acting = ActingPrincipal(user_id=uuid4(), service_account_id=None)
    agent_principal = SimpleNamespace(id=uuid4())

    got = resolve_credential_principal(
        session=_session("web"),
        acting=acting,
        agent_principal=agent_principal,
    )

    assert got == acting


def test_existing_service_account_acting_principal_is_preserved_for_api_session():
    acting = ActingPrincipal(user_id=None, service_account_id=uuid4())
    agent_principal = SimpleNamespace(id=uuid4())

    got = resolve_credential_principal(
        session=_session("api"),
        acting=acting,
        agent_principal=agent_principal,
    )

    assert got == acting
