"""Tests for HubSettings.

Hub endpoint + auth come from env vars
(SUROGATES_HUB_*) — consistent with the rest of the surogates
config surface.
"""

from __future__ import annotations

from surogates.config import HubSettings


def test_hub_settings_reads_env_vars(monkeypatch):
    monkeypatch.setenv("SUROGATES_HUB_ENDPOINT", "https://hub.example.com")
    monkeypatch.setenv("SUROGATES_HUB_USERNAME", "acme-runtime")
    monkeypatch.setenv("SUROGATES_HUB_PASSWORD", "secret-token")
    s = HubSettings()
    assert s.endpoint == "https://hub.example.com"
    assert s.username == "acme-runtime"
    assert s.password == "secret-token"


def test_hub_settings_defaults_empty(monkeypatch):
    """When SUROGATES_HUB_* vars are unset the worker treats Hub as
    disabled and falls back to legacy filesystem reads."""
    for key in (
        "SUROGATES_HUB_ENDPOINT",
        "SUROGATES_HUB_USERNAME",
        "SUROGATES_HUB_PASSWORD",
    ):
        monkeypatch.delenv(key, raising=False)
    s = HubSettings()
    assert s.endpoint == ""
    assert s.username == ""
    assert s.password == ""
