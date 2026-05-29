"""Plan 6 / Task 7.  Shared-mode SlackAdapter resolves the
inbound event's app_id -> agent via the ChannelRoutingCache
+ the per-tenant token via the CredentialVault.
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
    """The handler hits the cache (not the DB) and the vault
    (not settings.slack.bot_token)."""
    from surogates.channels.slack import SharedSlackInbound

    cache = _FakeCache({
        "slack:A0123ABCD": {
            "org_id": "o-1", "agent_id": "a-1",
            "api_web_url": "https://web.acme",
        },
    })
    vault = _FakeVault({
        "vault://slack_bot_token_A0123ABCD": "xoxb-real",
    })

    handler = SharedSlackInbound(
        channel_routing_cache=cache, vault=vault,
    )
    routing, token = await handler.resolve(app_id="A0123ABCD")
    assert routing["org_id"] == "o-1"
    assert routing["agent_id"] == "a-1"
    assert routing["api_web_url"] == "https://web.acme"
    assert token == "xoxb-real"
    assert cache.gets == ["slack:A0123ABCD"]
    # The token resolution went through the canonical vault
    # entry point (Plan 2 Task 16 source-level regression).
    assert vault.calls == [
        ("vault://slack_bot_token_A0123ABCD", "o-1"),
    ]


@pytest.mark.asyncio
async def test_resolve_inbound_missing_routing_returns_none():
    """A Slack event from a workspace we don't serve must NOT
    crash the adapter; the handler returns (None, None) so the
    caller drops the event cleanly."""
    from surogates.channels.slack import SharedSlackInbound

    cache = _FakeCache({})  # negative-memoise covers this
    vault = _FakeVault({})

    handler = SharedSlackInbound(
        channel_routing_cache=cache, vault=vault,
    )
    routing, token = await handler.resolve(app_id="A_unknown")
    assert routing is None
    assert token is None
    # Crucially: even though the routing was None, the vault was
    # NOT consulted.  A blind vault lookup for an unknown app_id
    # would generate spurious credential.access audit rows.
    assert vault.calls == []


@pytest.mark.asyncio
async def test_resolve_inbound_routing_with_missing_token():
    """The routing row exists but the bot token is not in the
    vault (e.g. admin set up the routing but hasn't uploaded the
    credential yet).  The handler returns (routing, None) so the
    adapter can surface a structured 'channel misconfigured'
    error rather than crashing."""
    from surogates.channels.slack import SharedSlackInbound

    cache = _FakeCache({
        "slack:A0123ABCD": {
            "org_id": "o-1", "agent_id": "a-1",
        },
    })
    vault = _FakeVault({})  # no token configured

    handler = SharedSlackInbound(
        channel_routing_cache=cache, vault=vault,
    )
    routing, token = await handler.resolve(app_id="A0123ABCD")
    assert routing["agent_id"] == "a-1"
    assert token is None
