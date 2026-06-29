"""Tests for memory_object_key in surogates.runtime.memory_protocol."""
from __future__ import annotations

import pytest


def test_boundary_keys_under_boundaries():
    from surogates.runtime.memory_protocol import memory_object_key

    assert memory_object_key(
        storage_key_prefix="p/a", user_id="u1", target="memory", boundary="public",
    ) == "p/a/boundaries/public/memory.json"
    assert memory_object_key(
        storage_key_prefix="p/a/", user_id=None, target="user", boundary="slack:c:G1",
    ) == "p/a/boundaries/slack:c:G1/user.json"


def test_no_boundary_is_unchanged():
    from surogates.runtime.memory_protocol import memory_object_key

    assert memory_object_key(
        storage_key_prefix="p/a", user_id="u1", target="memory",
    ) == "p/a/users/u1/memory.json"
    assert memory_object_key(
        storage_key_prefix="p/a", user_id=None, target="memory",
    ) == "p/a/shared/memory.json"


def test_unknown_target_still_rejected():
    from surogates.runtime.memory_protocol import memory_object_key

    with pytest.raises(ValueError):
        memory_object_key(storage_key_prefix="p/a", user_id="u1", target="bogus")
