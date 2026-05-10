"""Foundation tests: event types and config settings for the agent browser."""

from __future__ import annotations

import os

from surogates.session.events import EventType


def test_browser_event_types_exist() -> None:
    assert EventType.BROWSER_PROVISIONED.value == "browser.provisioned"
    assert EventType.BROWSER_DESTROYED.value == "browser.destroyed"


def test_browser_settings_defaults(monkeypatch) -> None:
    for key in list(os.environ):
        if key.startswith("SUROGATES_BROWSER_"):
            monkeypatch.delenv(key, raising=False)

    from surogates.config import BrowserSettings

    s = BrowserSettings()
    assert not hasattr(s, "enabled")
    assert s.backend == "process"
    assert s.image == "ghcr.io/onkernel/chromium-headful:stable"
    assert s.rest_port_base == 30000
    assert s.cdp_port_base == 31000
    assert s.live_view_port_base == 32000
    assert s.live_view_mode == "novnc"
    assert s.pod_ready_timeout == 60
    assert s.active_deadline_seconds == 3600
    assert s.cpu == "1"
    assert s.memory == "2Gi"
    assert s.cpu_limit == "2"
    assert s.memory_limit == "4Gi"


def test_browser_settings_env_override(monkeypatch) -> None:
    monkeypatch.setenv("SUROGATES_BROWSER_REST_PORT_BASE", "40000")

    from surogates.config import BrowserSettings

    s = BrowserSettings()
    assert s.rest_port_base == 40000


def test_settings_includes_browser(monkeypatch) -> None:
    for key in list(os.environ):
        if key.startswith("SUROGATES_"):
            monkeypatch.delenv(key, raising=False)

    from surogates.config import Settings

    s = Settings()
    assert s.browser.backend == "process"
