"""
The shared adapter inbound resolvers go through the
ChannelRoutingCache (not raw DB queries) and the CredentialVault
(not process-wide settings).  A future refactor that bypassed
either would re-introduce the cross-tenant blast radius this
plan eliminates.
"""

from __future__ import annotations

import inspect

from surogates.channels.slack import SharedSlackInbound
from surogates.channels.telegram import SharedTelegramInbound


def _normalised(src: str) -> str:
    """Strip whitespace so the regression survives reformatting
    (line breaks, spaces inside method-chain calls, etc.)."""
    return "".join(src.split())


def test_shared_slack_inbound_uses_channel_routing_cache():
    src = inspect.getsource(SharedSlackInbound)
    # The cache is held on the instance as ``self._cache`` (the
    # constructor parameter name is ``channel_routing_cache`` but
    # the attribute is private).  Either spelling is acceptable;
    # what matters is that a cache GET happens inside resolve().
    assert "channel_routing_cache" in src
    normalised = _normalised(src)
    assert "self._cache.get(" in normalised


def test_shared_slack_inbound_uses_resolve_channel_token():
    src = inspect.getsource(SharedSlackInbound)
    assert "resolve_channel_token" in src


def test_shared_telegram_inbound_uses_channel_routing_cache():
    src = inspect.getsource(SharedTelegramInbound)
    assert "channel_routing_cache" in src
    normalised = _normalised(src)
    assert "self._cache.get(" in normalised


def test_shared_telegram_inbound_uses_resolve_channel_token():
    src = inspect.getsource(SharedTelegramInbound)
    assert "resolve_channel_token" in src
