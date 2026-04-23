"""Tests for :func:`sandbox_session_key` — the helper that lets delegation
children share their parent's sandbox workspace.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from surogates.sandbox.pool import sandbox_session_key


def test_root_session_uses_its_own_id():
    sid = uuid4()
    session = SimpleNamespace(id=sid, parent_id=None)
    assert sandbox_session_key(session) == str(sid)


def test_delegation_child_uses_parent_id():
    parent_id = uuid4()
    child_id = uuid4()
    child = SimpleNamespace(id=child_id, parent_id=parent_id)
    # Child session map-operations go to the parent's entry so
    # ensure()/execute() land on the shared sandbox.
    assert sandbox_session_key(child) == str(parent_id)


def test_explicit_uuid_parent_coerces_to_string():
    """parent_id as a UUID (not str) -- str() converts it."""
    parent_id = uuid4()
    child = SimpleNamespace(id=uuid4(), parent_id=parent_id)
    result = sandbox_session_key(child)
    assert result == str(parent_id)
    assert isinstance(result, str)


def test_missing_parent_attribute_falls_back_to_id():
    """A plain object without parent_id should still work."""
    sid = uuid4()
    session = SimpleNamespace(id=sid)
    assert sandbox_session_key(session) == str(sid)


def test_sandbox_root_session_id_wins_over_parent_id():
    """The cached root in config takes precedence -- covers multi-hop chains.

    Scenario: grandparent G → parent P → child C.  When delegate.py
    creates C, it stamps C.config['sandbox_root_session_id'] = G.id
    (copied from P.config, which was in turn copied from G's creation
    context).  sandbox_session_key must return G.id, not P.id.
    """
    grandparent_id = uuid4()
    parent_id = uuid4()
    child_id = uuid4()
    child = SimpleNamespace(
        id=child_id,
        parent_id=parent_id,  # immediate parent is P
        config={"sandbox_root_session_id": str(grandparent_id)},
    )
    assert sandbox_session_key(child) == str(grandparent_id)


def test_empty_config_falls_back_to_parent_id():
    """Missing sandbox_root_session_id key falls back to parent resolution."""
    parent_id = uuid4()
    child = SimpleNamespace(
        id=uuid4(), parent_id=parent_id, config={},
    )
    assert sandbox_session_key(child) == str(parent_id)
