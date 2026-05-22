"""Tests for the _build_browser_backend resolver path for "fleet"."""
from __future__ import annotations

import pytest

from surogates.browser.composite import CompositeFallbackBackend
from surogates.browser.fleet import FleetBackend
from surogates.browser.process import ProcessBrowserBackend
from surogates.config import BrowserSettings
from surogates.orchestrator.worker import _build_browser_backend


def test_default_is_process_backend(monkeypatch):
    monkeypatch.delenv("SUROGATES_BROWSER_BACKEND", raising=False)
    backend = _build_browser_backend(BrowserSettings())
    assert isinstance(backend, ProcessBrowserBackend)


def test_fleet_requires_worker_token():
    settings = BrowserSettings(backend="fleet", fleet_worker_token="")
    with pytest.raises(RuntimeError, match="fleet_worker_token"):
        _build_browser_backend(settings)


def test_fleet_with_kubernetes_fallback_wraps_in_composite():
    settings = BrowserSettings(
        backend="fleet",
        fleet_worker_token="tok",
        fleet_fallback_backend="kubernetes",
    )
    backend = _build_browser_backend(settings)
    assert isinstance(backend, CompositeFallbackBackend)
    assert isinstance(backend.primary, FleetBackend)


def test_fleet_with_process_fallback():
    settings = BrowserSettings(
        backend="fleet",
        fleet_worker_token="tok",
        fleet_fallback_backend="process",
    )
    backend = _build_browser_backend(settings)
    assert isinstance(backend, CompositeFallbackBackend)
    assert isinstance(backend.primary, FleetBackend)
    assert isinstance(backend.fallback, ProcessBrowserBackend)


def test_fleet_with_no_fallback_returns_bare_fleet_backend():
    settings = BrowserSettings(
        backend="fleet",
        fleet_worker_token="tok",
        fleet_fallback_backend="none",
    )
    backend = _build_browser_backend(settings)
    assert isinstance(backend, FleetBackend)


def test_fleet_backend_receives_storage_settings():
    """Storage settings flow into FleetBackend so it can derive S3 creds."""
    class _Storage:
        access_key = "AK"
        secret_key = "SK"
        region = "auto"
        endpoint = "https://x/"

    settings = BrowserSettings(
        backend="fleet",
        fleet_worker_token="tok",
        fleet_fallback_backend="none",
    )
    backend = _build_browser_backend(settings, storage_settings=_Storage())
    assert isinstance(backend, FleetBackend)
    assert backend.storage_settings.access_key == "AK"
