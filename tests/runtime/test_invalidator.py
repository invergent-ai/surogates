"""Tests for the Redis pub/sub invalidator.

Plan 1 / Task 17.  Surogate-ops publishes
``agent.runtime_config_changed:<agent_id>`` (and Plan 3 will publish
``agent.bundle_changed:<agent_id>``) on Redis whenever an admin
updates the per-agent runtime config / bundle.  The invalidator
listens on the pattern, parses the agent_id out of the channel name,
and evicts the corresponding entry from the in-process cache.

The handler is split out so it is testable without a real Redis
connection — ``run_invalidator`` is the long-running coroutine that
plugs into a real ``redis.pubsub()`` listener.
"""

from __future__ import annotations

from unittest.mock import MagicMock


def test_handler_invalidates_runtime_config_changed():
    from surogates.runtime.invalidator import handle_invalidation_message

    cache = MagicMock()
    handle_invalidation_message(
        cache,
        channel="agent.runtime_config_changed:a-1",
        payload=b"",
    )
    cache.invalidate.assert_called_once_with("a-1")


def test_handler_invalidates_bundle_changed():
    """Bundle invalidation lands in Plan 3 (file artifacts) but we
    pre-route the channel here so the wiring exists when the bundle
    accessor is added."""
    from surogates.runtime.invalidator import handle_invalidation_message

    cache = MagicMock()
    handle_invalidation_message(
        cache,
        channel="agent.bundle_changed:a-2",
        payload=b"",
    )
    cache.invalidate.assert_called_once_with("a-2")


def test_handler_ignores_unrelated_channels():
    from surogates.runtime.invalidator import handle_invalidation_message

    cache = MagicMock()
    handle_invalidation_message(
        cache, channel="some.other.channel", payload=b"",
    )
    cache.invalidate.assert_not_called()


def test_handler_ignores_empty_agent_id():
    """A malformed message ``agent.runtime_config_changed:`` must not
    blow up the listener.  We swallow rather than raise so a bad
    publisher cannot crash every shared-runtime pod."""
    from surogates.runtime.invalidator import handle_invalidation_message

    cache = MagicMock()
    handle_invalidation_message(
        cache,
        channel="agent.runtime_config_changed:",
        payload=b"",
    )
    cache.invalidate.assert_not_called()


def test_handler_tolerates_channel_with_colons_in_agent_id():
    """UUID agent IDs do not contain colons today, but the channel
    parser splits exactly once so an agent id with a trailing colon
    is still routed (Plan 6 channel routing might key on multi-part
    identifiers later)."""
    from surogates.runtime.invalidator import handle_invalidation_message

    cache = MagicMock()
    handle_invalidation_message(
        cache,
        channel="agent.runtime_config_changed:agent:with:colons",
        payload=b"",
    )
    cache.invalidate.assert_called_once_with("agent:with:colons")


def test_invalidation_channels_constant_is_exported():
    """Plans 3, 6, 7 publish on additional channels; keep the constant
    importable so they extend it in one place."""
    from surogates.runtime.invalidator import INVALIDATION_CHANNELS

    assert "agent.runtime_config_changed:" in INVALIDATION_CHANNELS
    assert "agent.bundle_changed:" in INVALIDATION_CHANNELS
