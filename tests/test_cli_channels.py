"""Tests for the ``surogates channels`` CLI subcommand.

Covers:
- ``channels`` subcommand parses with and without the optional ``kind`` arg.
- ``cmd_channels`` invokes a patched ``run_channels``.
- ``ChannelsSettings`` defaults are sane and match the contract.
- ``enabled_platforms(settings)`` reads ``settings.channels.<kind>.enabled``.
- ``build_channels_app`` returns an app with ``/health`` and builds with
  zero registered platforms (no network, no uvicorn server in tests).
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from surogates.channels.base import SendResult
from surogates.channels.inbound import InboundMessage
from surogates.channels.registry import (
    ChannelDescriptor,
    ChannelRegistry,
    VerificationResult,
)


# ---------------------------------------------------------------------------
# Shared fakes
# ---------------------------------------------------------------------------


class _FakePlatform:
    """Minimal ChannelPlatform implementation for testing."""

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
        return "ws_id"

    def verify(self, request, body, *, creds) -> bool | VerificationResult:
        return True

    def parse(self, body) -> InboundMessage | None:
        return None

    async def send(self, item, *, creds) -> SendResult:
        return SendResult(success=True)


@dataclass
class _FakePlatformConfig:
    enabled: bool


class _FakeSettings:
    """Settings stub for enabled_platforms tests."""

    def __init__(self, enabled_kinds: set[str]) -> None:
        self._enabled = enabled_kinds

    @property
    def channels(self) -> dict:
        return {
            kind: _FakePlatformConfig(enabled=kind in self._enabled)
            for kind in ("fake",)
        }


# ---------------------------------------------------------------------------
# CLI parser tests
# ---------------------------------------------------------------------------


class TestChannelsSubcommand:
    def _parse(self, *args: str) -> argparse.Namespace:
        from surogates.cli.main import build_parser
        return build_parser().parse_args(["channels", *args])

    def test_channels_subcommand_parses_no_kind(self):
        args = self._parse()
        assert args.command == "channels"
        assert getattr(args, "kind", None) is None

    def test_channels_subcommand_parses_with_kind(self):
        args = self._parse("slack")
        assert args.command == "channels"
        assert args.kind == "slack"

    def test_channels_subcommand_is_in_commands(self):
        from surogates.cli.main import COMMANDS
        assert "channels" in COMMANDS

    def test_cmd_channels_invokes_run_channels(self):
        """cmd_channels calls run_channels (patched to avoid IO)."""
        from surogates.cli.main import cmd_channels

        called_with: list[Any] = []

        async def _fake_run(settings, kind=None):
            called_with.append(kind)

        with (
            patch("surogates.config.load_settings") as mock_load,
            patch("surogates.channels.runner.run_channels", _fake_run),
        ):
            mock_load.return_value = MagicMock(
                log_level="INFO",
                channels=MagicMock(port=8001),
            )
            args = argparse.Namespace(command="channels", kind=None)
            cmd_channels(args)

        assert called_with == [None]

    def test_cmd_channels_passes_kind(self):
        from surogates.cli.main import cmd_channels

        called_with: list[Any] = []

        async def _fake_run(settings, kind=None):
            called_with.append(kind)

        with (
            patch("surogates.config.load_settings") as mock_load,
            patch("surogates.channels.runner.run_channels", _fake_run),
        ):
            mock_load.return_value = MagicMock(
                log_level="INFO",
                channels=MagicMock(port=8001),
            )
            args = argparse.Namespace(command="channels", kind="slack")
            cmd_channels(args)

        assert called_with == ["slack"]


# ---------------------------------------------------------------------------
# ChannelsSettings tests
# ---------------------------------------------------------------------------


class TestChannelsSettings:
    def test_default_port(self):
        from surogates.config import ChannelsSettings
        s = ChannelsSettings()
        assert s.port == 8001

    def test_default_public_url_is_empty(self):
        from surogates.config import ChannelsSettings
        s = ChannelsSettings()
        assert s.public_url == ""

    def test_channels_attr_on_settings(self):
        """Settings has a ``channels`` attribute of type ChannelsSettings."""
        from surogates.config import Settings, ChannelsSettings
        s = Settings()
        assert hasattr(s, "channels")
        assert isinstance(s.channels, ChannelsSettings)


class TestChannelsSettingsEnvEnablement:
    """Per-kind enablement is independent and reads the kind-scoped env var.

    Regression: a shared ``ChannelKindSettings`` with an empty ``env_prefix``
    read a bare ``ENABLED`` (colliding across kinds) and never saw the
    ``SUROGATES_CHANNELS_<KIND>_ENABLED`` key produced by the YAML loader, so
    no platform could ever be enabled via config — the channels process mounted
    zero routes and silently delivered nothing.
    """

    def test_slack_enabled_without_telegram(self, monkeypatch):
        monkeypatch.setenv("SUROGATES_CHANNELS_SLACK_ENABLED", "true")
        monkeypatch.delenv("SUROGATES_CHANNELS_TELEGRAM_ENABLED", raising=False)
        from surogates.config import ChannelsSettings
        s = ChannelsSettings()
        assert s.slack.enabled is True
        assert s.telegram.enabled is False

    def test_telegram_enabled_without_slack(self, monkeypatch):
        monkeypatch.setenv("SUROGATES_CHANNELS_TELEGRAM_ENABLED", "true")
        monkeypatch.delenv("SUROGATES_CHANNELS_SLACK_ENABLED", raising=False)
        from surogates.config import ChannelsSettings
        s = ChannelsSettings()
        assert s.telegram.enabled is True
        assert s.slack.enabled is False

    def test_disabled_by_default(self, monkeypatch):
        monkeypatch.delenv("SUROGATES_CHANNELS_SLACK_ENABLED", raising=False)
        monkeypatch.delenv("SUROGATES_CHANNELS_TELEGRAM_ENABLED", raising=False)
        monkeypatch.delenv("SUROGATES_CHANNELS_WEBSITE_ENABLED", raising=False)
        from surogates.config import ChannelsSettings
        s = ChannelsSettings()
        assert s.slack.enabled is False
        assert s.telegram.enabled is False
        assert s.website.enabled is False


class TestBuiltinPlatformRegistration:
    """``import surogates.channels.platforms`` registers every built-in.

    ``run_channels`` imports the *package* for its registration side effect and
    then asks the registry which platforms are enabled.  Tested in a fresh
    interpreter so a submodule another test already imported can't mask an empty
    package ``__init__`` (the regression: the package imported nothing, the
    registry stayed empty, and the channels process mounted zero routes).
    """

    def test_package_import_registers_slack_and_telegram(self):
        import subprocess
        import sys

        code = (
            "import surogates.channels.platforms\n"
            "from surogates.channels.registry import registry\n"
            "print(sorted(registry._platforms))\n"
        )
        out = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=True,
        )
        kinds = out.stdout.strip()
        assert "slack" in kinds, out.stderr
        assert "telegram" in kinds, out.stderr


class TestChannelsSettingsEnabledPlatforms:
    """ChannelsSettings integrates with ChannelRegistry.enabled_platforms."""

    def test_enabled_when_config_says_enabled(self):
        reg = ChannelRegistry()
        platform = _FakePlatform()
        reg.register(platform)

        settings = _FakeSettings(enabled_kinds={"fake"})
        result = reg.enabled_platforms(settings)
        assert platform in result

    def test_disabled_when_config_says_disabled(self):
        reg = ChannelRegistry()
        platform = _FakePlatform()
        reg.register(platform)

        settings = _FakeSettings(enabled_kinds=set())
        result = reg.enabled_platforms(settings)
        assert result == []


# ---------------------------------------------------------------------------
# build_channels_app tests
# ---------------------------------------------------------------------------


class TestBuildChannelsApp:
    """build_channels_app must work with zero registered platforms."""

    def _make_deps(self):
        """Provide all required fakes for build_channels_app."""
        redis = MagicMock()
        session_factory = MagicMock()
        vault = MagicMock()
        platform_client = MagicMock()
        cache = MagicMock()
        delivery_service = MagicMock()
        session_store = MagicMock()
        return dict(
            redis=redis,
            session_factory=session_factory,
            vault=vault,
            platform_client=platform_client,
            cache=cache,
            delivery_service=delivery_service,
            session_store=session_store,
        )

    def test_build_channels_app_returns_fastapi_app(self):
        from surogates.channels.runner import build_channels_app
        from fastapi import FastAPI

        # Use a fresh empty registry to ensure zero-platforms case.
        empty_reg = ChannelRegistry()
        settings = MagicMock()
        settings.channels = {}

        result = build_channels_app(settings, registry=empty_reg, **self._make_deps())
        app, delivery_dispatcher, reconciler = result
        assert isinstance(app, FastAPI)

    def test_health_route_returns_200(self):
        from surogates.channels.runner import build_channels_app

        empty_reg = ChannelRegistry()
        settings = MagicMock()
        settings.channels = {}

        app, _, _ = build_channels_app(settings, registry=empty_reg, **self._make_deps())
        client = TestClient(app)
        resp = client.get("/health")
        assert resp.status_code == 200

    def test_no_crash_with_zero_platforms(self):
        """Building with empty registry must not raise."""
        from surogates.channels.runner import build_channels_app

        empty_reg = ChannelRegistry()
        settings = MagicMock()
        settings.channels = {}

        # Should not raise.
        app, delivery, reconciler = build_channels_app(
            settings, registry=empty_reg, **self._make_deps()
        )
        assert delivery is not None
        assert reconciler is not None

    def test_delivery_dispatcher_returned(self):
        from surogates.channels.runner import build_channels_app
        from surogates.channels.dispatcher import ChannelDeliveryDispatcher

        empty_reg = ChannelRegistry()
        settings = MagicMock()
        settings.channels = {}

        _, delivery, _ = build_channels_app(settings, registry=empty_reg, **self._make_deps())
        assert isinstance(delivery, ChannelDeliveryDispatcher)

    def test_reconciler_returned(self):
        from surogates.channels.runner import build_channels_app
        from surogates.channels.dispatcher import ChannelWebhookReconciler

        empty_reg = ChannelRegistry()
        settings = MagicMock()
        settings.channels = {}

        _, _, reconciler = build_channels_app(settings, registry=empty_reg, **self._make_deps())
        assert isinstance(reconciler, ChannelWebhookReconciler)


# ---------------------------------------------------------------------------
# Import sanity tests
# ---------------------------------------------------------------------------


class TestImports:
    def test_cli_main_imports_cleanly(self):
        import surogates.cli.main  # noqa: F401

    def test_channels_dispatcher_imports_cleanly(self):
        import surogates.channels.dispatcher  # noqa: F401

    def test_channels_runner_imports_cleanly(self):
        import surogates.channels.runner  # noqa: F401

    def test_run_channels_is_importable(self):
        from surogates.channels.runner import run_channels  # noqa: F401

    def test_build_channels_app_is_importable(self):
        from surogates.channels.runner import build_channels_app  # noqa: F401
