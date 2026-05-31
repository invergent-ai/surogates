"""Shared-mode TelegramAdapter resolves the
inbound update's bot username -> agent via the
ChannelRoutingCache + the per-tenant token via the CredentialVault.
"""

from __future__ import annotations

import pytest


class _FakeCache:
    def __init__(self, routing):
        self._routing = routing
        self.gets = []

    async def get(self, key):
        self.gets.append(key)
        return self._routing.get(key)


class _FakeVault:
    def __init__(self, mapping):
        self._mapping = mapping
        self.calls = []

    async def resolve_ref(self, ref, *, org_id, user_id=None):
        self.calls.append((ref, org_id))
        return self._mapping.get(ref)


@pytest.mark.asyncio
async def test_resolve_inbound_uses_cache_and_vault():
    from surogates.channels.telegram import SharedTelegramInbound

    cache = _FakeCache({
        "telegram:@my_bot": {
            "org_id": "o-1", "agent_id": "a-1",
        },
    })
    vault = _FakeVault({
        "vault://telegram_bot_token_@my_bot": "1234:abc",
    })
    handler = SharedTelegramInbound(
        channel_routing_cache=cache, vault=vault,
    )
    routing, token = await handler.resolve(bot_username="@my_bot")
    assert routing["agent_id"] == "a-1"
    assert token == "1234:abc"
    assert cache.gets == ["telegram:@my_bot"]
    assert vault.calls == [
        ("vault://telegram_bot_token_@my_bot", "o-1"),
    ]


@pytest.mark.asyncio
async def test_resolve_inbound_missing_returns_none():
    from surogates.channels.telegram import SharedTelegramInbound

    cache = _FakeCache({})
    vault = _FakeVault({})
    handler = SharedTelegramInbound(
        channel_routing_cache=cache, vault=vault,
    )
    routing, token = await handler.resolve(bot_username="@unknown")
    assert routing is None
    assert token is None
    # Vault NOT consulted for unknown bot usernames -- same
    # contract as SharedSlackInbound.
    assert vault.calls == []


@pytest.mark.asyncio
async def test_resolve_inbound_routing_with_missing_token():
    """Admin set up the routing but didn't upload the bot token
    yet.  The handler returns (routing, None) so the adapter
    surfaces a structured misconfigured error."""
    from surogates.channels.telegram import SharedTelegramInbound

    cache = _FakeCache({
        "telegram:@my_bot": {"org_id": "o-1", "agent_id": "a-1"},
    })
    vault = _FakeVault({})

    handler = SharedTelegramInbound(
        channel_routing_cache=cache, vault=vault,
    )
    routing, token = await handler.resolve(bot_username="@my_bot")
    assert routing["agent_id"] == "a-1"
    assert token is None
