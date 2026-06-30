"""The worker keys R2 memory by the session's boundary for channel sessions."""
from __future__ import annotations

from types import SimpleNamespace

from surogates.orchestrator.worker import _build_r2_memory_keys


def test_channel_session_memory_keys_are_boundary_scoped():
    s = SimpleNamespace(channel="slack", config={"memory_boundary": "slack:c:G1"}, id="s1")
    keys = _build_r2_memory_keys(session=s, storage_key_prefix="p/a", user_id="u1")
    assert keys["memory"] == "p/a/boundaries/slack:c:G1/memory.json"
    assert "users/u1" not in keys["memory"]  # the leak path is gone


def test_two_private_channels_same_user_isolated():
    a = SimpleNamespace(channel="slack", config={"memory_boundary": "slack:c:G1"}, id="s1")
    b = SimpleNamespace(channel="slack", config={"memory_boundary": "slack:c:G2"}, id="s2")
    ka = _build_r2_memory_keys(session=a, storage_key_prefix="p/a", user_id="u1")
    kb = _build_r2_memory_keys(session=b, storage_key_prefix="p/a", user_id="u1")
    assert ka["memory"] != kb["memory"]  # same user, different private channels


def test_older_channel_session_without_boundary_is_fail_closed():
    s = SimpleNamespace(
        channel="telegram",
        config={"channel_session_key": "agent:telegram:group:-100"},
        id="s1",
    )
    keys = _build_r2_memory_keys(session=s, storage_key_prefix="p/a", user_id="u1")
    assert keys["memory"] == "p/a/boundaries/telegram:iso:agent:telegram:group:-100/memory.json"
    assert "users/u1" not in keys["memory"]


def test_web_session_stays_per_user():
    s = SimpleNamespace(channel="web", config={}, id="s1")
    keys = _build_r2_memory_keys(session=s, storage_key_prefix="p/a", user_id="u1")
    assert keys["memory"] == "p/a/users/u1/memory.json"
