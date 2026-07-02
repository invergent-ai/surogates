"""Multi-party sessions load no per-user memory; DMs/web still do."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from surogates.orchestrator.worker import _memory_user_id


def _session(multi_party):
    return SimpleNamespace(config={"multi_party": multi_party})


def test_multi_party_session_has_no_user_memory():
    uid = uuid4()
    assert _memory_user_id(_session(True), SimpleNamespace(user_id=uid)) is None


def test_single_party_session_uses_acting_user():
    uid = uuid4()
    assert _memory_user_id(_session(False), SimpleNamespace(user_id=uid)) == str(uid)


def test_service_account_principal_has_no_user_memory():
    assert _memory_user_id(_session(False), SimpleNamespace(user_id=None)) is None
