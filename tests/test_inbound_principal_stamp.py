"""Channel USER_MESSAGE events carry the resolved Surogates principal.

The acting-principal resolver reads principal_user_id off the latest user
message; inbound must stamp it (the resolved identity.user_id), distinct from
source.user_id (the platform id).
"""

from __future__ import annotations

from uuid import uuid4

from surogates.channels.inbound import build_principal_stamp


def test_stamp_carries_resolved_user_id():
    uid = uuid4()
    stamp = build_principal_stamp(user_id=uid)
    assert stamp == {"principal_user_id": str(uid)}


def test_stamp_carries_resolved_service_account_id():
    sa = uuid4()
    stamp = build_principal_stamp(service_account_id=sa)
    assert stamp == {"principal_service_account_id": str(sa)}


def test_stamp_empty_when_no_identity():
    assert build_principal_stamp(user_id=None, service_account_id=None) == {}


def test_stamp_refuses_ambiguous_principal():
    uid = uuid4()
    sa = uuid4()
    try:
        build_principal_stamp(user_id=uid, service_account_id=sa)
    except ValueError as exc:
        assert "exactly one" in str(exc)
    else:
        raise AssertionError("expected ValueError")
