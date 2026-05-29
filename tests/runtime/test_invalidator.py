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


def test_handler_routes_bundle_changed_to_runtime_cache():
    """Bundle invalidation lands in Plan 3 (file artifacts) but we
    pre-route the channel through the runtime-config cache so the wiring
    exists when the bundle accessor is added."""
    from surogates.runtime.invalidator import handle_invalidation_message

    rt = MagicMock()
    handle_invalidation_message(
        channel="agent.bundle_changed:a-2",
        payload=b"",
        runtime_config_cache=rt,
    )
    rt.invalidate.assert_called_once_with("a-2")


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


def test_invalidation_channels_constant_exports_all_four_prefixes():
    """Plans 3 / 6 / 7 will publish on additional channels; keep the
    constant importable so they extend it in one place."""
    from surogates.runtime.invalidator import INVALIDATION_CHANNELS

    assert "agent.runtime_config_changed:" in INVALIDATION_CHANNELS
    assert "agent.bundle_changed:" in INVALIDATION_CHANNELS
    assert "project.firebase_config_changed:" in INVALIDATION_CHANNELS
    assert "agent.slug_changed:" in INVALIDATION_CHANNELS
