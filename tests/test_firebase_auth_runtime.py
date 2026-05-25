"""Tests for the Surogates runtime auth settings + Firebase helper."""

from __future__ import annotations

import pytest

from surogates.config import Settings
from surogates.tenant.auth.firebase import firebase_auth_provider_name


def test_auth_settings_parse_enabled_providers(monkeypatch):
    """Env vars produced by the Helm chart parse cleanly into AuthSettings."""
    monkeypatch.setenv("SUROGATES_AUTH_SELF_REGISTRATION_ENABLED", "true")
    monkeypatch.setenv("SUROGATES_AUTH_FIREBASE_PROJECT_ID", "builder-firebase")
    monkeypatch.setenv("SUROGATES_AUTH_FIREBASE_API_KEY", "public-key")
    monkeypatch.setenv(
        "SUROGATES_AUTH_FIREBASE_AUTH_DOMAIN", "builder.firebaseapp.com",
    )
    monkeypatch.setenv(
        "SUROGATES_AUTH_FIREBASE_ENABLED_PROVIDERS", "google,password",
    )

    settings = Settings()

    assert settings.auth.self_registration_enabled is True
    assert settings.auth.firebase_project_id == "builder-firebase"
    assert settings.auth.providers == ("google", "password")
    assert settings.auth.firebase_configured is True


def test_auth_settings_default_to_disabled(monkeypatch):
    """No env vars ⇒ self-registration off, providers empty, not configured."""
    for key in (
        "SUROGATES_AUTH_SELF_REGISTRATION_ENABLED",
        "SUROGATES_AUTH_FIREBASE_PROJECT_ID",
        "SUROGATES_AUTH_FIREBASE_API_KEY",
        "SUROGATES_AUTH_FIREBASE_AUTH_DOMAIN",
        "SUROGATES_AUTH_FIREBASE_ENABLED_PROVIDERS",
    ):
        monkeypatch.delenv(key, raising=False)

    settings = Settings()

    assert settings.auth.self_registration_enabled is False
    assert settings.auth.providers == ()
    assert settings.auth.firebase_configured is False


def test_auth_settings_ignore_unknown_providers(monkeypatch):
    """Providers we don't support are silently dropped so the chart can
    ship a forward-compatible string without crashing the API."""
    monkeypatch.setenv(
        "SUROGATES_AUTH_FIREBASE_ENABLED_PROVIDERS", "google,twitter,password",
    )

    settings = Settings()

    assert settings.auth.providers == ("google", "password")


def test_firebase_provider_name_is_project_scoped():
    assert (
        firebase_auth_provider_name("builder-firebase")
        == "firebase:builder-firebase"
    )


def test_firebase_provider_name_strips_whitespace():
    assert (
        firebase_auth_provider_name("  builder  ")
        == "firebase:builder"
    )


def test_firebase_provider_name_rejects_empty_project():
    with pytest.raises(ValueError):
        firebase_auth_provider_name("")


def test_firebase_provider_name_rejects_whitespace_only():
    with pytest.raises(ValueError):
        firebase_auth_provider_name("   ")
