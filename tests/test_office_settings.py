"""OfficeSettings: off unless configured, and whole when on."""

from __future__ import annotations

import os

import pytest
from pydantic import ValidationError

from surogates.config import OfficeSettings

SECRET = "s" * 32


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in list(os.environ):
        if name.startswith("SUROGATES_OFFICE_"):
            monkeypatch.delenv(name)


def test_off_by_default():
    assert OfficeSettings().enabled is False


def test_on_needs_every_url_and_both_secrets(monkeypatch):
    monkeypatch.setenv("SUROGATES_OFFICE_PUBLIC_URL", "https://office.example")
    with pytest.raises(ValidationError):
        OfficeSettings()


def test_short_secrets_are_refused(monkeypatch):
    monkeypatch.setenv("SUROGATES_OFFICE_PUBLIC_URL", "https://office.example")
    monkeypatch.setenv("SUROGATES_OFFICE_INTERNAL_URL", "http://docs.internal")
    monkeypatch.setenv("SUROGATES_OFFICE_CALLBACK_BASE_URL", "https://api.example")
    monkeypatch.setenv("SUROGATES_OFFICE_JWT_SECRET", "short")
    monkeypatch.setenv("SUROGATES_OFFICE_TOKEN_SECRET", SECRET)
    with pytest.raises(ValidationError):
        OfficeSettings()


def test_on(monkeypatch):
    monkeypatch.setenv("SUROGATES_OFFICE_PUBLIC_URL", "https://office.example")
    monkeypatch.setenv("SUROGATES_OFFICE_INTERNAL_URL", "http://docs.internal")
    monkeypatch.setenv("SUROGATES_OFFICE_CALLBACK_BASE_URL", "https://api.example")
    monkeypatch.setenv("SUROGATES_OFFICE_JWT_SECRET", SECRET)
    monkeypatch.setenv("SUROGATES_OFFICE_TOKEN_SECRET", SECRET)
    settings = OfficeSettings()
    assert settings.enabled is True
    assert settings.internal_url == "http://docs.internal"


def test_runtime_office_is_optional_but_whole_when_set(monkeypatch):
    monkeypatch.setenv("SUROGATES_OFFICE_PUBLIC_URL", "https://office.example")
    monkeypatch.setenv("SUROGATES_OFFICE_INTERNAL_URL", "http://docs.internal")
    monkeypatch.setenv("SUROGATES_OFFICE_CALLBACK_BASE_URL", "https://api.example")
    monkeypatch.setenv("SUROGATES_OFFICE_JWT_SECRET", SECRET)
    monkeypatch.setenv("SUROGATES_OFFICE_TOKEN_SECRET", SECRET)
    assert OfficeSettings().agent_editing is False
    monkeypatch.setenv("SUROGATES_OFFICE_RUNTIME_OFFICE_URL", "http://runtime-office.test")
    with pytest.raises(ValidationError, match="runtime_office_token"):
        OfficeSettings()
    monkeypatch.setenv("SUROGATES_OFFICE_RUNTIME_OFFICE_TOKEN", SECRET)
    assert OfficeSettings().agent_editing is True
