"""_select_harness_token / _filter_effective_tools key off the tenant principal.

In a shared session owned by A but acted by B, the minted token and the
API-client-gated tools follow the tenant (acting principal), not the session row.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from surogates.orchestrator.worker import (
    _select_harness_token,
    _filter_effective_tools,
    principal_subject,
)
from surogates.session.acting_principal import ActingPrincipal
from surogates.tenant.auth.jwt import decode_token
from surogates.tenant.context import TenantContext


def _tenant(*, user_id=None, service_account_id=None):
    return TenantContext(
        org_id=uuid4(), user_id=user_id, org_config={}, user_preferences={},
        permissions=frozenset(), asset_root="p/x",
        service_account_id=service_account_id,
    )


def _session(*, service_account_id=None, channel="slack"):
    return SimpleNamespace(
        id=uuid4(), service_account_id=service_account_id, channel=channel,
    )


def test_service_account_token_uses_tenant_not_session():
    acting_sa = uuid4()
    tenant = _tenant(service_account_id=acting_sa)
    # Session row's SA is a DIFFERENT (owner) principal / or None.
    owner_sa = uuid4()
    session = _session(service_account_id=owner_sa)
    token = _select_harness_token(tenant=tenant, session=session, agent_id="a1")
    payload = decode_token(token)
    assert payload["type"] == "service_account_session"
    assert payload["service_account_id"] == str(acting_sa)
    assert payload["service_account_id"] != str(owner_sa)
    assert payload["session_id"] == str(session.id)


def test_service_account_token_mints_when_session_row_has_no_sa():
    acting_sa = uuid4()
    tenant = _tenant(service_account_id=acting_sa)
    session = _session(service_account_id=None)
    token = _select_harness_token(tenant=tenant, session=session, agent_id="a1")
    payload = decode_token(token)
    assert payload["type"] == "service_account_session"
    assert payload["service_account_id"] == str(acting_sa)


def test_artifact_kept_for_acting_service_account():
    tenant = _tenant(service_account_id=uuid4())
    session = _session(service_account_id=None)
    tools = _filter_effective_tools(
        tools={"create_artifact", "memory"}, tenant=tenant, session=session,
        use_api_for_harness_tools=True,
    )
    assert "create_artifact" in tools  # acting SA -> will have an API client


def test_principal_subject_marks_user_principal():
    user_id = uuid4()

    subject_id, is_service_account = principal_subject(
        ActingPrincipal(user_id=user_id, service_account_id=None),
    )

    assert subject_id == user_id
    assert is_service_account is False


def test_principal_subject_marks_service_account_principal():
    service_account_id = uuid4()

    subject_id, is_service_account = principal_subject(
        ActingPrincipal(user_id=None, service_account_id=service_account_id),
    )

    assert subject_id == service_account_id
    assert is_service_account is True


def test_principal_subject_handles_empty_principal_shape():
    subject_id, is_service_account = principal_subject(
        ActingPrincipal(user_id=None, service_account_id=None),
    )

    assert subject_id is None
    assert is_service_account is False
