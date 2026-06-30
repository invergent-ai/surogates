"""Tests for the ChannelPlatform protocol registry.

Covers:
- register(platform) + get(kind) round-trip with a fake platform
- enabled_platforms(settings) filters by channels.<kind>.enabled flags
- duplicate-kind registration raises ValueError
"""

from __future__ import annotations

import dataclasses
import pytest

from surogates.channels.registry import (
    ChannelDescriptor,
    ChannelPlatform,
    ChannelRegistry,
    VerificationResult,
)
from surogates.channels.base import SendResult
from surogates.channels.inbound import InboundMessage


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _make_inbound_message(**kw) -> InboundMessage:
    defaults = dict(
        kind="text",
        identifier="C123",
        thread_key=None,
        platform_user_id="U1",
        user_name="alice",
        text="hello",
        media_urls=[],
        media_types=[],
        is_dm=False,
        is_mention=True,
        ts="1000.0001",
        source={},
    )
    defaults.update(kw)
    return InboundMessage(**defaults)


class _FakePlatform:
    """Minimal ChannelPlatform implementation (no optional members)."""

    kind = "fake"
    topology = "webhook"

    descriptor = ChannelDescriptor(
        vault_refs=lambda ident: {"token": f"fake/{ident}/token"},
        config_keys=("fake_token",),
        webhook_registration="manual",
    )

    def route_path(self, identifier=None) -> str:
        if identifier:
            return f"/channels/fake/{identifier}"
        return "/channels/fake"

    def identifier_of(self, request, body) -> str:
        return body.get("channel", "default")

    def verify(self, request, body, *, creds) -> bool | VerificationResult:
        return True

    def parse(self, body, *, creds=None) -> InboundMessage | None:
        return _make_inbound_message()

    async def send(self, item, *, creds) -> SendResult:
        return SendResult(success=True)


class _FakePlatformWithOptionals(_FakePlatform):
    """Fake platform that also exposes optional members."""

    kind = "fake_optional"
    interactive_paths = ("/channels/fake_optional/actions",)

    async def handle_non_message_update(self, body, *, routing, creds, deps) -> bool:
        return False


class _AnotherPlatform(_FakePlatform):
    """Second fake platform for multi-platform tests."""

    kind = "another"


class _FakeSettings:
    """Duck-typed settings object for enabled_platforms tests."""

    def __init__(self, enabled_kinds: set[str]) -> None:
        self._enabled = enabled_kinds

    @property
    def channels(self) -> dict:
        return {
            kind: _FakeChannelConfig(enabled=kind in self._enabled)
            for kind in ("fake", "another", "fake_optional")
        }


@dataclasses.dataclass
class _FakeChannelConfig:
    enabled: bool


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRegisterAndGet:
    def test_register_and_get_round_trip(self):
        reg = ChannelRegistry()
        platform = _FakePlatform()
        reg.register(platform)
        retrieved = reg.get("fake")
        assert retrieved is platform

    def test_get_unknown_kind_returns_none(self):
        reg = ChannelRegistry()
        assert reg.get("unknown") is None

    def test_duplicate_kind_raises(self):
        reg = ChannelRegistry()
        reg.register(_FakePlatform())
        with pytest.raises(ValueError, match="already registered"):
            reg.register(_FakePlatform())

    def test_register_multiple_distinct_kinds(self):
        reg = ChannelRegistry()
        fake = _FakePlatform()
        another = _AnotherPlatform()
        reg.register(fake)
        reg.register(another)
        assert reg.get("fake") is fake
        assert reg.get("another") is another

    def test_platform_with_optional_members_registers(self):
        reg = ChannelRegistry()
        p = _FakePlatformWithOptionals()
        reg.register(p)
        assert reg.get("fake_optional") is p


class TestEnabledPlatforms:
    def test_enabled_platforms_returns_enabled_only(self):
        reg = ChannelRegistry()
        fake = _FakePlatform()
        another = _AnotherPlatform()
        reg.register(fake)
        reg.register(another)
        settings = _FakeSettings(enabled_kinds={"fake"})
        result = reg.enabled_platforms(settings)
        assert fake in result
        assert another not in result

    def test_enabled_platforms_all_enabled(self):
        reg = ChannelRegistry()
        fake = _FakePlatform()
        another = _AnotherPlatform()
        reg.register(fake)
        reg.register(another)
        settings = _FakeSettings(enabled_kinds={"fake", "another"})
        result = reg.enabled_platforms(settings)
        assert set(result) == {fake, another}

    def test_enabled_platforms_none_enabled(self):
        reg = ChannelRegistry()
        reg.register(_FakePlatform())
        settings = _FakeSettings(enabled_kinds=set())
        result = reg.enabled_platforms(settings)
        assert result == []

    def test_enabled_platforms_skips_missing_config_keys(self):
        """Platforms with no entry in settings.channels are excluded (not enabled)."""
        reg = ChannelRegistry()

        class _UnknownPlatform(_FakePlatform):
            kind = "not_in_settings"

        reg.register(_UnknownPlatform())
        settings = _FakeSettings(enabled_kinds={"fake"})
        result = reg.enabled_platforms(settings)
        assert result == []


class TestVerificationResult:
    def test_defaults(self):
        v = VerificationResult(accepted=True)
        assert v.accepted is True
        assert v.response_body is None
        assert v.status_code == 200

    def test_with_body_and_status(self):
        v = VerificationResult(accepted=True, response_body={"challenge": "abc"}, status_code=200)
        assert v.response_body == {"challenge": "abc"}

    def test_rejected(self):
        v = VerificationResult(accepted=False, status_code=403)
        assert v.accepted is False
        assert v.status_code == 403


class TestChannelDescriptor:
    def test_vault_refs_callable(self):
        d = ChannelDescriptor(
            vault_refs=lambda ident: {"token": f"slack/{ident}/token"},
            config_keys=("slack_token",),
            webhook_registration="api",
        )
        assert d.vault_refs("C123") == {"token": "slack/C123/token"}
        assert d.config_keys == ("slack_token",)
        assert d.webhook_registration == "api"

    def test_register_webhook_optional(self):
        d = ChannelDescriptor(
            vault_refs=lambda ident: {},
            config_keys=(),
            webhook_registration="manual",
        )
        assert d.register_webhook is None


class TestModuleLevelRegistry:
    def test_module_registry_is_accessible(self):
        """The module exposes a module-level registry instance."""
        import surogates.channels.registry as reg_mod
        assert hasattr(reg_mod, "registry")
        assert isinstance(reg_mod.registry, ChannelRegistry)
