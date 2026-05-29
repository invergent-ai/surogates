"""Tests for the Redis pub/sub invalidator.

Plan 1 / Task 17 + Plan 1b / Task 7.  Surogate-ops publishes on Redis
whenever an admin mutates per-agent runtime config, file bundle,
project Firebase config, or agent slug.  The invalidator listens on
the patterns, parses the identifier out of the channel name, and
evicts the corresponding entry from the matching cache.

The handler is split out so it is testable without a real Redis
connection — ``run_invalidator`` is the long-running coroutine that
plugs into a real ``redis.pubsub()`` listener.
"""

from __future__ import annotations

from unittest.mock import MagicMock


def test_handler_routes_runtime_config_changed_to_runtime_cache():
    from surogates.runtime.invalidator import handle_invalidation_message

    rt = MagicMock()
    fb = MagicMock()
    sl = MagicMock()
    handle_invalidation_message(
        channel="agent.runtime_config_changed:a-1",
        payload=b"",
        runtime_config_cache=rt,
        firebase_cache=fb,
        slug_cache=sl,
    )
    rt.invalidate.assert_called_once_with("a-1")
    fb.invalidate.assert_not_called()
    sl.invalidate.assert_not_called()


def test_handler_routes_bundle_changed_to_file_bundle_cache():
    """Plan 3 / Task 8.  agent.bundle_changed: was pre-routed by
    Plan 1b Task 7 to runtime_config_cache as a transitional
    target; Plan 3 retargets it to the new file_bundle_cache."""
    from surogates.runtime.invalidator import handle_invalidation_message

    rt = MagicMock()
    fb = MagicMock()
    handle_invalidation_message(
        channel="agent.bundle_changed:a-2",
        payload=b"",
        runtime_config_cache=rt,
        file_bundle_cache=fb,
    )
    fb.invalidate.assert_called_once_with("a-2")
    rt.invalidate.assert_not_called()


def test_handler_routes_firebase_changed_to_firebase_cache():
    from surogates.runtime.invalidator import handle_invalidation_message

    rt = MagicMock()
    fb = MagicMock()
    handle_invalidation_message(
        channel="project.firebase_config_changed:p-1",
        payload=b"",
        runtime_config_cache=rt,
        firebase_cache=fb,
    )
    fb.invalidate.assert_called_once_with("p-1")
    rt.invalidate.assert_not_called()


def test_handler_routes_slug_changed_to_slug_cache():
    from surogates.runtime.invalidator import handle_invalidation_message

    sl = MagicMock()
    handle_invalidation_message(
        channel="agent.slug_changed:acme",
        payload=b"",
        slug_cache=sl,
    )
    sl.invalidate.assert_called_once_with("acme")


def test_handler_ignores_unrelated_channels():
    from surogates.runtime.invalidator import handle_invalidation_message

    rt = MagicMock()
    fb = MagicMock()
    sl = MagicMock()
    handle_invalidation_message(
        channel="some.other.channel",
        payload=b"",
        runtime_config_cache=rt,
        firebase_cache=fb,
        slug_cache=sl,
    )
    rt.invalidate.assert_not_called()
    fb.invalidate.assert_not_called()
    sl.invalidate.assert_not_called()


def test_handler_ignores_empty_identifier():
    """A malformed message ``agent.runtime_config_changed:`` must not
    blow up the listener.  We swallow rather than raise so a bad
    publisher cannot crash every shared-runtime pod."""
    from surogates.runtime.invalidator import handle_invalidation_message

    rt = MagicMock()
    handle_invalidation_message(
        channel="agent.runtime_config_changed:",
        payload=b"",
        runtime_config_cache=rt,
    )
    rt.invalidate.assert_not_called()


def test_handler_tolerates_channel_with_colons_in_identifier():
    """UUID identifiers do not contain colons today, but the channel
    parser splits exactly once so an identifier with embedded colons
    is still routed."""
    from surogates.runtime.invalidator import handle_invalidation_message

    rt = MagicMock()
    handle_invalidation_message(
        channel="agent.runtime_config_changed:agent:with:colons",
        payload=b"",
        runtime_config_cache=rt,
    )
    rt.invalidate.assert_called_once_with("agent:with:colons")


def test_handler_skips_routing_when_target_cache_is_none():
    """A pod that hasn't wired the slug cache yet must not raise when
    a slug.changed message arrives — silently skip."""
    from surogates.runtime.invalidator import handle_invalidation_message

    handle_invalidation_message(
        channel="agent.slug_changed:acme",
        payload=b"",
        # slug_cache deliberately omitted
    )  # must not raise


def test_invalidation_channels_constant_exports_all_five_prefixes():
    """Plans 3 / 4 / 6 / 7 will publish on additional channels; keep the
    constant importable so they extend it in one place."""
    from surogates.runtime.invalidator import INVALIDATION_CHANNELS

    assert "agent.runtime_config_changed:" in INVALIDATION_CHANNELS
    assert "agent.bundle_changed:" in INVALIDATION_CHANNELS
    assert "project.firebase_config_changed:" in INVALIDATION_CHANNELS
    assert "agent.slug_changed:" in INVALIDATION_CHANNELS
    assert "user.memory_changed:" in INVALIDATION_CHANNELS


def test_handler_routes_channel_routing_changed_to_channel_routing_cache():
    """Plan 6 / Task 2.  Admin CRUD on the channel_routing table
    publishes channel_routing_changed:<kind>:<identifier> on
    Redis; the shared adapter pod invalidates its cache so the
    next inbound event for that channel sees the new routing."""
    from surogates.runtime.invalidator import handle_invalidation_message

    crc = MagicMock()
    handle_invalidation_message(
        channel="channel_routing_changed:slack:A0123ABCD",
        payload=b"",
        channel_routing_cache=crc,
    )
    crc.invalidate.assert_called_once_with("slack:A0123ABCD")


def test_invalidation_channels_includes_channel_routing_changed():
    from surogates.runtime.invalidator import INVALIDATION_CHANNELS

    assert "channel_routing_changed:" in INVALIDATION_CHANNELS


def test_handler_routes_mcp_servers_changed_to_mcp_server_cache():
    """Plan 5 / Task 7.  Admin CRUD on the per-tenant MCP server
    registry publishes agent.mcp_servers_changed:<agent_id> on
    Redis; the proxy invalidates its cache so the next call sees
    the new server list."""
    from surogates.runtime.invalidator import handle_invalidation_message

    mc = MagicMock()
    handle_invalidation_message(
        channel="agent.mcp_servers_changed:a-1",
        payload=b"",
        mcp_server_cache=mc,
    )
    mc.invalidate.assert_called_once_with("a-1")


def test_invalidation_channels_includes_mcp_servers_changed():
    """Plan 5 / Task 7.  The constant must list the new prefix so
    `run_invalidator` psubscribes to it from the lifespan."""
    from surogates.runtime.invalidator import INVALIDATION_CHANNELS

    assert "agent.mcp_servers_changed:" in INVALIDATION_CHANNELS


def test_handler_routes_user_memory_changed_to_memory_cache():
    """Plan 4 / Task 2.  When a worker writes to a user's memory it
    publishes user.memory_changed:<org_id>:<user_id> on Redis; other
    workers serving the same user invalidate their L1 entry so the
    next read fetches the new bytes from R2."""
    from surogates.runtime.invalidator import handle_invalidation_message

    mc = MagicMock()
    handle_invalidation_message(
        channel="user.memory_changed:o-1:u-1",
        payload=b"",
        memory_cache=mc,
    )
    # The identifier is the everything-after-the-prefix string.
    mc.invalidate.assert_called_once_with("o-1:u-1")
