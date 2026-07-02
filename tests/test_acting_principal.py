"""Pure resolution of the acting principal from a user-message payload."""

from __future__ import annotations

from uuid import uuid4

from surogates.session.acting_principal import (
    ActingPrincipal,
    resolve_principal_from_event_data,
)


def _owner():
    return ActingPrincipal(user_id=uuid4(), service_account_id=None)


def test_stamped_user_id_wins():
    uid = uuid4()
    got = resolve_principal_from_event_data(
        {"principal_user_id": str(uid)}, fallback=_owner()
    )
    assert got == ActingPrincipal(user_id=uid, service_account_id=None)


def test_missing_stamp_falls_back_to_owner():
    fb = _owner()
    assert resolve_principal_from_event_data({}, fallback=fb) == fb
    assert resolve_principal_from_event_data(None, fallback=fb) == fb


def test_invalid_uuid_falls_back_to_owner():
    fb = _owner()
    assert resolve_principal_from_event_data(
        {"principal_user_id": "not-a-uuid"}, fallback=fb
    ) == fb


def test_service_account_stamp():
    sa = uuid4()
    got = resolve_principal_from_event_data(
        {"principal_service_account_id": str(sa)}, fallback=_owner()
    )
    assert got == ActingPrincipal(user_id=None, service_account_id=sa)
