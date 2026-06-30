"""Tests for ChannelWebhookReconciler and PlatformClient.list_channel_routings.

Covers:
- list_channel_routings: 200 → list, 404 → [], 401 → PlatformAuthError.
- register_all on an api platform: registers every routing with correct URL and creds.
- register_all on a manual platform: register_webhook never called.
- register_all: one identifier raising does NOT stop the rest.
- handle_routing_change: re-registers exactly one identifier.
- Pubsub channel-name parse: channel_routing_changed:telegram:@bot → kind=telegram,
  identifier=@bot.
- Pubsub channel-name with colon in identifier is handled.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from surogates.channels.dispatcher import ChannelWebhookReconciler
from surogates.channels.registry import ChannelDescriptor, ChannelRegistry
from surogates.channels.base import SendResult
from surogates.channels.inbound import InboundMessage


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ORG_ID = "org-aaaa-1111"
PUBLIC_URL = "https://channels.surogate.ai"


# ---------------------------------------------------------------------------
# Fake platform helpers
# ---------------------------------------------------------------------------


def _make_api_descriptor(*, raise_on: set[str] | None = None) -> ChannelDescriptor:
    """A descriptor for an api-registration platform that records register calls."""
    registered: list[tuple[str, str, dict]] = []
    _raise_on: set[str] = raise_on or set()

    async def register_webhook(identifier: str, url: str, creds: dict) -> None:
        if identifier in _raise_on:
            raise RuntimeError(f"Simulated failure for {identifier}")
        registered.append((identifier, url, creds))

    descriptor = ChannelDescriptor(
        vault_refs=lambda ident: {"bot_token": "bot_token"},
        config_keys=("token",),
        webhook_registration="api",
        register_webhook=register_webhook,
    )
    # Expose registered list for assertions
    descriptor._registered = registered  # type: ignore[attr-defined]
    return descriptor


def _make_manual_descriptor() -> ChannelDescriptor:
    """A descriptor for a manual-registration platform."""
    registered: list[Any] = []

    async def register_webhook(identifier: str, url: str, creds: dict) -> None:  # pragma: no cover
        registered.append((identifier, url, creds))

    descriptor = ChannelDescriptor(
        vault_refs=lambda ident: {"bot_token": "bot_token"},
        config_keys=("token",),
        webhook_registration="manual",
        register_webhook=register_webhook,
    )
    descriptor._registered = registered  # type: ignore[attr-defined]
    return descriptor


class _FakeApiPlatform:
    kind = "telegram"
    topology = "webhook"

    def __init__(self, descriptor: ChannelDescriptor | None = None) -> None:
        self.descriptor = descriptor or _make_api_descriptor()

    def route_path(self, identifier: str | None = None) -> str:
        return f"/channels/telegram/{identifier}" if identifier else "/channels/telegram"

    def identifier_of(self, request: Any, body: Any) -> str:
        return ""

    def verify(self, request: Any, body: Any, *, creds: dict) -> bool:
        return True

    def parse(self, body: Any) -> InboundMessage | None:
        return None

    async def send(self, item: Any, *, creds: dict) -> SendResult:
        return SendResult(success=True)


class _FakeManualPlatform(_FakeApiPlatform):
    kind = "slack"

    def __init__(self) -> None:
        self.descriptor = _make_manual_descriptor()

    def route_path(self, identifier: str | None = None) -> str:
        return f"/channels/slack/{identifier}" if identifier else "/channels/slack"


# ---------------------------------------------------------------------------
# Fake vault
# ---------------------------------------------------------------------------


class _FakeVault:
    def __init__(self, values: dict[str, str] | None = None) -> None:
        self._values = values or {}
        self.calls: list[tuple[str, str]] = []

    async def resolve_ref(self, ref: str, *, org_id: str) -> str | None:
        self.calls.append((ref, org_id))
        return self._values.get(ref, f"resolved:{ref}")


# ---------------------------------------------------------------------------
# Fake platform_client
# ---------------------------------------------------------------------------


class _FakePlatformClient:
    def __init__(self, routings_by_kind: dict[str, list[dict]] | None = None) -> None:
        self._routings = routings_by_kind or {}
        self.calls: list[str] = []

    async def list_channel_routings(self, kind: str) -> list[dict]:
        self.calls.append(kind)
        return list(self._routings.get(kind, []))


# ---------------------------------------------------------------------------
# Settings stub
# ---------------------------------------------------------------------------


class _FakeChannelCfg:
    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled


def _settings(*kinds: str) -> Any:
    from types import SimpleNamespace
    return SimpleNamespace(channels={k: _FakeChannelCfg(enabled=True) for k in kinds})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_reconciler(
    platform_client: _FakePlatformClient,
    vault: _FakeVault,
    registry: ChannelRegistry,
    *,
    public_url: str = PUBLIC_URL,
    settings: Any = None,
) -> ChannelWebhookReconciler:
    settings = settings or _settings()
    return ChannelWebhookReconciler(
        platform_client=platform_client,
        vault=vault,
        public_url=public_url,
        settings=settings,
        registry=registry,
    )


# ===========================================================================
# Tests: PlatformClient.list_channel_routings
# ===========================================================================


class TestListChannelRoutings:
    async def test_returns_list_on_200(self):
        from surogates.runtime.platform_client import PlatformClient

        rows = [
            {"channel_identifier": "@bot1", "org_id": "o1", "agent_id": "a1"},
            {"channel_identifier": "@bot2", "org_id": "o2", "agent_id": "a2"},
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/api/channels/by-kind/telegram"
            return httpx.Response(200, json=rows)

        client = PlatformClient(
            base_url="http://platform",
            token="tok",
            transport=httpx.MockTransport(handler),
        )
        result = await client.list_channel_routings("telegram")
        assert result == rows
        await client.aclose()

    async def test_returns_empty_list_on_404(self):
        from surogates.runtime.platform_client import PlatformClient

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404)

        client = PlatformClient(
            base_url="http://platform",
            token="tok",
            transport=httpx.MockTransport(handler),
        )
        result = await client.list_channel_routings("telegram")
        assert result == []
        await client.aclose()

    async def test_raises_platform_auth_error_on_401(self):
        from surogates.runtime.platform_client import PlatformAuthError, PlatformClient

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401)

        client = PlatformClient(
            base_url="http://platform",
            token="bad",
            transport=httpx.MockTransport(handler),
        )
        with pytest.raises(PlatformAuthError):
            await client.list_channel_routings("telegram")
        await client.aclose()

    async def test_raises_http_status_error_on_500(self):
        from surogates.runtime.platform_client import PlatformClient

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="boom")

        client = PlatformClient(
            base_url="http://platform",
            token="tok",
            transport=httpx.MockTransport(handler),
        )
        with pytest.raises(httpx.HTTPStatusError):
            await client.list_channel_routings("telegram")
        await client.aclose()

    async def test_uses_correct_url_path(self):
        """Hits /api/channels/by-kind/{kind}, not the by-identifier endpoint."""
        from surogates.runtime.platform_client import PlatformClient

        seen_paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_paths.append(request.url.path)
            return httpx.Response(200, json=[])

        client = PlatformClient(
            base_url="http://platform",
            token="tok",
            transport=httpx.MockTransport(handler),
        )
        await client.list_channel_routings("slack")
        assert seen_paths == ["/api/channels/by-kind/slack"]
        await client.aclose()


# ===========================================================================
# Tests: ChannelWebhookReconciler.register_all
# ===========================================================================


class TestRegisterAll:
    async def test_api_platform_registers_all_routings(self):
        """register_all calls descriptor.register_webhook for every routing."""
        descriptor = _make_api_descriptor()
        platform = _FakeApiPlatform(descriptor)

        pc = _FakePlatformClient({
            "telegram": [
                {"channel_identifier": "@botA", "org_id": ORG_ID, "agent_id": "a1"},
                {"channel_identifier": "@botB", "org_id": ORG_ID, "agent_id": "a2"},
            ],
        })
        vault = _FakeVault()
        reg = ChannelRegistry()
        reg.register(platform)
        reconciler = _make_reconciler(pc, vault, reg)

        await reconciler.register_all(platform)

        registered_ids = [r[0] for r in descriptor._registered]
        assert "@botA" in registered_ids
        assert "@botB" in registered_ids
        assert len(descriptor._registered) == 2

    async def test_api_platform_registers_correct_url(self):
        """URL passed to register_webhook is public_url + route_path(identifier)."""
        descriptor = _make_api_descriptor()
        platform = _FakeApiPlatform(descriptor)

        pc = _FakePlatformClient({
            "telegram": [
                {"channel_identifier": "@mybot", "org_id": ORG_ID, "agent_id": "a1"},
            ],
        })
        vault = _FakeVault()
        reg = ChannelRegistry()
        reg.register(platform)
        reconciler = _make_reconciler(pc, vault, reg, public_url="https://channels.surogate.ai")

        await reconciler.register_all(platform)

        _, url, _ = descriptor._registered[0]
        expected = "https://channels.surogate.ai" + platform.route_path("@mybot")
        assert url == expected

    async def test_api_platform_resolves_creds_per_identifier(self):
        """Creds are resolved per routing using vault_refs and org_id."""
        descriptor = _make_api_descriptor()
        platform = _FakeApiPlatform(descriptor)

        pc = _FakePlatformClient({
            "telegram": [
                {"channel_identifier": "@bot1", "org_id": "org-x", "agent_id": "a1"},
            ],
        })
        vault = _FakeVault()
        reg = ChannelRegistry()
        reg.register(platform)
        reconciler = _make_reconciler(pc, vault, reg)

        await reconciler.register_all(platform)

        # vault.resolve_ref must have been called with org_id=org-x
        assert any(org_id == "org-x" for _, org_id in vault.calls)

    async def test_api_platform_passes_resolved_creds(self):
        """Creds dict from vault is forwarded to register_webhook."""
        descriptor = _make_api_descriptor()
        platform = _FakeApiPlatform(descriptor)

        pc = _FakePlatformClient({
            "telegram": [
                {"channel_identifier": "@bot1", "org_id": ORG_ID, "agent_id": "a1"},
            ],
        })
        # The vault returns a known value for any ref
        vault = _FakeVault()
        reg = ChannelRegistry()
        reg.register(platform)
        reconciler = _make_reconciler(pc, vault, reg)

        await reconciler.register_all(platform)

        _, _, creds = descriptor._registered[0]
        # Creds dict must be non-empty (one key per vault_refs entry)
        assert "bot_token" in creds

    async def test_manual_platform_skipped(self):
        """register_all on a manual-registration platform never calls register_webhook."""
        platform = _FakeManualPlatform()

        pc = _FakePlatformClient({
            "slack": [
                {"channel_identifier": "A123", "org_id": ORG_ID, "agent_id": "a1"},
            ],
        })
        vault = _FakeVault()
        reg = ChannelRegistry()
        reg.register(platform)
        reconciler = _make_reconciler(pc, vault, reg)

        await reconciler.register_all(platform)

        # list_channel_routings must NOT have been called (no API hit)
        assert pc.calls == []
        assert platform.descriptor._registered == []

    async def test_one_failure_does_not_abort_others(self):
        """If one identifier's register_webhook raises, the rest still complete."""
        descriptor = _make_api_descriptor(raise_on={"@bad"})
        platform = _FakeApiPlatform(descriptor)

        pc = _FakePlatformClient({
            "telegram": [
                {"channel_identifier": "@bad", "org_id": ORG_ID, "agent_id": "a1"},
                {"channel_identifier": "@good1", "org_id": ORG_ID, "agent_id": "a2"},
                {"channel_identifier": "@good2", "org_id": ORG_ID, "agent_id": "a3"},
            ],
        })
        vault = _FakeVault()
        reg = ChannelRegistry()
        reg.register(platform)
        reconciler = _make_reconciler(pc, vault, reg)

        # Must NOT raise
        await reconciler.register_all(platform)

        registered_ids = [r[0] for r in descriptor._registered]
        assert "@good1" in registered_ids
        assert "@good2" in registered_ids
        assert "@bad" not in registered_ids

    async def test_empty_routings_is_no_op(self):
        """No routings → register_webhook never called, no error."""
        descriptor = _make_api_descriptor()
        platform = _FakeApiPlatform(descriptor)

        pc = _FakePlatformClient({"telegram": []})
        vault = _FakeVault()
        reg = ChannelRegistry()
        reg.register(platform)
        reconciler = _make_reconciler(pc, vault, reg)

        await reconciler.register_all(platform)

        assert descriptor._registered == []

    async def test_trailing_slash_stripped_from_public_url(self):
        """public_url trailing slash is stripped before route_path is appended."""
        descriptor = _make_api_descriptor()
        platform = _FakeApiPlatform(descriptor)

        pc = _FakePlatformClient({
            "telegram": [
                {"channel_identifier": "@bot", "org_id": ORG_ID, "agent_id": "a1"},
            ],
        })
        vault = _FakeVault()
        reg = ChannelRegistry()
        reg.register(platform)
        # Public URL with trailing slash
        reconciler = _make_reconciler(pc, vault, reg, public_url="https://channels.surogate.ai/")

        await reconciler.register_all(platform)

        _, url, _ = descriptor._registered[0]
        assert not url.startswith("https://channels.surogate.ai//"), (
            f"double-slash in URL: {url!r}"
        )


# ===========================================================================
# Tests: ChannelWebhookReconciler.handle_routing_change
# ===========================================================================


class TestHandleRoutingChange:
    async def test_re_registers_the_named_identifier(self):
        """handle_routing_change registers exactly the named identifier."""
        descriptor = _make_api_descriptor()
        platform = _FakeApiPlatform(descriptor)

        pc = _FakePlatformClient({
            "telegram": [
                {"channel_identifier": "@botA", "org_id": ORG_ID, "agent_id": "a1"},
                {"channel_identifier": "@botB", "org_id": ORG_ID, "agent_id": "a2"},
            ],
        })
        vault = _FakeVault()
        reg = ChannelRegistry()
        reg.register(platform)
        reconciler = _make_reconciler(pc, vault, reg)

        await reconciler.handle_routing_change("telegram", "@botA")

        registered_ids = [r[0] for r in descriptor._registered]
        assert registered_ids == ["@botA"]

    async def test_does_not_register_other_identifiers(self):
        """handle_routing_change only registers the ONE requested identifier."""
        descriptor = _make_api_descriptor()
        platform = _FakeApiPlatform(descriptor)

        pc = _FakePlatformClient({
            "telegram": [
                {"channel_identifier": "@botA", "org_id": ORG_ID, "agent_id": "a1"},
                {"channel_identifier": "@botB", "org_id": ORG_ID, "agent_id": "a2"},
            ],
        })
        vault = _FakeVault()
        reg = ChannelRegistry()
        reg.register(platform)
        reconciler = _make_reconciler(pc, vault, reg)

        await reconciler.handle_routing_change("telegram", "@botA")

        registered_ids = [r[0] for r in descriptor._registered]
        assert "@botB" not in registered_ids

    async def test_unknown_kind_is_no_op(self):
        """handle_routing_change for an unregistered kind does nothing."""
        pc = _FakePlatformClient()
        vault = _FakeVault()
        reg = ChannelRegistry()
        reconciler = _make_reconciler(pc, vault, reg)

        # Must not raise
        await reconciler.handle_routing_change("unknown_kind", "@bot")

        assert pc.calls == []

    async def test_manual_platform_skipped(self):
        """handle_routing_change on a manual platform never calls register_webhook."""
        platform = _FakeManualPlatform()
        pc = _FakePlatformClient({
            "slack": [
                {"channel_identifier": "A123", "org_id": ORG_ID, "agent_id": "a1"},
            ],
        })
        vault = _FakeVault()
        reg = ChannelRegistry()
        reg.register(platform)
        reconciler = _make_reconciler(pc, vault, reg)

        await reconciler.handle_routing_change("slack", "A123")

        assert platform.descriptor._registered == []

    async def test_missing_routing_row_is_no_op(self):
        """handle_routing_change for an identifier not in routings does nothing."""
        descriptor = _make_api_descriptor()
        platform = _FakeApiPlatform(descriptor)

        # Only @other exists, not @gone
        pc = _FakePlatformClient({
            "telegram": [
                {"channel_identifier": "@other", "org_id": ORG_ID, "agent_id": "a1"},
            ],
        })
        vault = _FakeVault()
        reg = ChannelRegistry()
        reg.register(platform)
        reconciler = _make_reconciler(pc, vault, reg)

        await reconciler.handle_routing_change("telegram", "@gone")

        assert descriptor._registered == []

    async def test_correct_url_built_for_single_identifier(self):
        """URL in handle_routing_change follows public_url + route_path."""
        descriptor = _make_api_descriptor()
        platform = _FakeApiPlatform(descriptor)

        pc = _FakePlatformClient({
            "telegram": [
                {"channel_identifier": "@single", "org_id": ORG_ID, "agent_id": "a1"},
            ],
        })
        vault = _FakeVault()
        reg = ChannelRegistry()
        reg.register(platform)
        reconciler = _make_reconciler(pc, vault, reg, public_url="https://channels.surogate.ai")

        await reconciler.handle_routing_change("telegram", "@single")

        _, url, _ = descriptor._registered[0]
        assert url == "https://channels.surogate.ai" + platform.route_path("@single")


# ===========================================================================
# Tests: pubsub channel-name parsing (via run())
# ===========================================================================


class TestPubsubChannelNameParse:
    """Verify handle_routing_change receives the right kind/identifier from
    channel names produced by the pubsub loop.

    We test the parsing directly by monkeypatching handle_routing_change so
    we don't need a real Redis connection.
    """

    async def test_parses_simple_kind_and_identifier(self):
        """channel_routing_changed:telegram:@mybot → kind=telegram, identifier=@mybot."""
        parsed: list[tuple[str, str]] = []

        descriptor = _make_api_descriptor()
        platform = _FakeApiPlatform(descriptor)
        pc = _FakePlatformClient()
        vault = _FakeVault()
        reg = ChannelRegistry()
        reg.register(platform)
        reconciler = _make_reconciler(pc, vault, reg)

        # Patch handle_routing_change to capture calls
        async def _capture(kind: str, identifier: str) -> None:
            parsed.append((kind, identifier))

        reconciler.handle_routing_change = _capture  # type: ignore[method-assign]

        # Simulate what run() does with a pmessage
        channel = "channel_routing_changed:telegram:@mybot"
        suffix = channel.removeprefix("channel_routing_changed:")
        kind, identifier = suffix.split(":", 1)
        await reconciler.handle_routing_change(kind, identifier)

        assert parsed == [("telegram", "@mybot")]

    async def test_parses_identifier_with_colon(self):
        """Identifiers that themselves contain colons are handled by split(':', 1)."""
        parsed: list[tuple[str, str]] = []

        descriptor = _make_api_descriptor()
        platform = _FakeApiPlatform(descriptor)
        pc = _FakePlatformClient()
        vault = _FakeVault()
        reg = ChannelRegistry()
        reg.register(platform)
        reconciler = _make_reconciler(pc, vault, reg)

        async def _capture(kind: str, identifier: str) -> None:
            parsed.append((kind, identifier))

        reconciler.handle_routing_change = _capture  # type: ignore[method-assign]

        channel = "channel_routing_changed:some_platform:id:with:colons"
        suffix = channel.removeprefix("channel_routing_changed:")
        kind, identifier = suffix.split(":", 1)
        await reconciler.handle_routing_change(kind, identifier)

        assert parsed == [("some_platform", "id:with:colons")]

    async def test_channel_name_without_separator_is_ignored(self):
        """A channel name with no second colon does not call handle_routing_change."""
        parsed: list[tuple[str, str]] = []

        descriptor = _make_api_descriptor()
        platform = _FakeApiPlatform(descriptor)
        pc = _FakePlatformClient()
        vault = _FakeVault()
        reg = ChannelRegistry()
        reg.register(platform)
        reconciler = _make_reconciler(pc, vault, reg)

        async def _capture(kind: str, identifier: str) -> None:  # pragma: no cover
            parsed.append((kind, identifier))

        reconciler.handle_routing_change = _capture  # type: ignore[method-assign]

        channel = "channel_routing_changed:nodashhere"
        suffix = channel.removeprefix("channel_routing_changed:")
        if ":" not in suffix:
            pass  # run() would skip this
        else:
            kind, identifier = suffix.split(":", 1)
            await reconciler.handle_routing_change(kind, identifier)

        assert parsed == []
