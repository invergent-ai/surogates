"""Who may change an item in the org-shared layer, and who may create one there.

``require_shared_layer_writer`` answers for an item the storage found;
``require_shared_layer_creator`` answers for a create, which lands in the
org-shared layer exactly when the caller has no user.  The routes in
``tests/integration/test_skills_shared_layer.py`` and ``test_agents_api.py``
exercise the same rule over HTTP.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import HTTPException

from surogates.api.routes._shared import (
    require_shared_layer_creator,
    require_shared_layer_writer,
)
from surogates.tenant.context import TenantContext

ORG = {"key_prefix": "tenant/shared/skills/notes", "layer": "org"}
OWN = {"key_prefix": "tenant/users/u/skills/notes", "layer": "user"}
NO_LAYER = {"key_prefix": "tenant/shared/skills/notes"}


def _tenant(**fields) -> TenantContext:
    base = dict(
        org_id=uuid4(),
        user_id=None,
        org_config={},
        user_preferences={},
        permissions=frozenset(),
        asset_root="",
    )
    return TenantContext(**{**base, **fields})


USER = _tenant(user_id=uuid4(), permissions=frozenset({"sessions:read"}))
ADMIN = _tenant(user_id=uuid4(), permissions=frozenset({"sessions:read", "admin"}))
ORG_WIDE_SERVICE_ACCOUNT = _tenant(service_account_id=uuid4())
ORG_WIDE_SERVICE_ACCOUNT_SESSION = _tenant(
    service_account_id=uuid4(), session_scope_id=uuid4(),
)
BOUND_SERVICE_ACCOUNT = _tenant(
    service_account_id=uuid4(), service_account_agent_id="customer-agent",
)
BOUND_SERVICE_ACCOUNT_SESSION = _tenant(
    service_account_id=uuid4(),
    service_account_agent_id="customer-agent",
    session_scope_id=uuid4(),
)
CHANNEL = _tenant(session_scope_id=uuid4())
#: A service account is never an admin, whatever permissions it carries.
SERVICE_ACCOUNT_HOLDING_ADMIN = _tenant(
    service_account_id=uuid4(),
    service_account_agent_id="customer-agent",
    permissions=frozenset({"admin"}),
)

ALLOWED = {
    "admin": ADMIN,
    "org-wide service account": ORG_WIDE_SERVICE_ACCOUNT,
    "org-wide service account session": ORG_WIDE_SERVICE_ACCOUNT_SESSION,
}
REFUSED = {
    "user": USER,
    "agent-bound service account": BOUND_SERVICE_ACCOUNT,
    "agent-bound service account session": BOUND_SERVICE_ACCOUNT_SESSION,
    "agent-bound service account holding admin": SERVICE_ACCOUNT_HOLDING_ADMIN,
    "channel session": CHANNEL,
}


def _refused(call) -> HTTPException:
    with pytest.raises(HTTPException) as caught:
        call()
    assert caught.value.status_code == 403
    return caught.value


@pytest.mark.parametrize("tenant", [*ALLOWED.values(), *REFUSED.values()], ids=[*ALLOWED, *REFUSED])
def test_anyone_changes_an_item_in_their_own_layer(tenant):
    require_shared_layer_writer(tenant, OWN, "Skill 'notes'")


@pytest.mark.parametrize("tenant", ALLOWED.values(), ids=ALLOWED)
def test_an_admin_or_an_org_wide_service_account_changes_an_org_shared_item(tenant):
    require_shared_layer_writer(tenant, ORG, "Skill 'notes'")


@pytest.mark.parametrize("tenant", REFUSED.values(), ids=REFUSED)
def test_nobody_else_changes_an_org_shared_item(tenant):
    refusal = _refused(lambda: require_shared_layer_writer(tenant, ORG, "Skill 'notes'"))
    assert "Skill 'notes'" in refusal.detail
    assert "org-wide service account" in refusal.detail


def test_an_item_with_no_layer_counts_as_shared_and_is_refused_to_a_user():
    _refused(lambda: require_shared_layer_writer(USER, NO_LAYER, "Skill 'notes'"))


@pytest.mark.parametrize("tenant", ALLOWED.values(), ids=ALLOWED)
def test_an_item_with_no_layer_is_changed_by_an_admin_or_an_org_wide_service_account(tenant):
    require_shared_layer_writer(tenant, NO_LAYER, "Skill 'notes'")


@pytest.mark.parametrize("tenant", [USER, ADMIN], ids=["user", "admin"])
def test_a_signed_in_user_creates_into_their_own_layer(tenant):
    require_shared_layer_creator(tenant, "A skill")


@pytest.mark.parametrize(
    "tenant", [ORG_WIDE_SERVICE_ACCOUNT, ORG_WIDE_SERVICE_ACCOUNT_SESSION],
    ids=["service account", "service account session"],
)
def test_an_org_wide_service_account_creates_into_the_org_shared_layer(tenant):
    require_shared_layer_creator(tenant, "A skill")


@pytest.mark.parametrize(
    "tenant",
    [
        BOUND_SERVICE_ACCOUNT,
        BOUND_SERVICE_ACCOUNT_SESSION,
        SERVICE_ACCOUNT_HOLDING_ADMIN,
        CHANNEL,
    ],
    ids=[
        "agent-bound service account",
        "agent-bound service account session",
        "agent-bound service account holding admin",
        "channel session",
    ],
)
def test_nobody_else_creates_into_the_org_shared_layer(tenant):
    refusal = _refused(lambda: require_shared_layer_creator(tenant, "A skill"))
    assert "A skill" in refusal.detail
    assert "org-wide service account" in refusal.detail
