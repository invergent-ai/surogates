"""Unit tests for the coding-agent credential bundle (no DB)."""

from __future__ import annotations

import pytest

from surogates.coding_agents.credentials import (
    PROVIDERS,
    CRED_NAME,
    CredentialBundle,
    CredentialError,
    validate_pasted,
)


def test_providers_and_names():
    assert PROVIDERS == ("anthropic", "openai")
    assert CRED_NAME["anthropic"] == "code_cred:anthropic"
    assert CRED_NAME["openai"] == "code_cred:openai"


def test_bundle_round_trip():
    bundle = CredentialBundle(
        provider="anthropic",
        auth_mode="oauth",
        token_kind="setup_token",
        oauth_token="sk-ant-oat01-abc",
    )
    restored = CredentialBundle.from_json(bundle.to_json())
    assert restored == bundle


def test_bundle_status_hides_secret():
    bundle = CredentialBundle(
        provider="openai", auth_mode="api_key", api_key="sk-secret",
    )
    status = bundle.status()
    assert status == {
        "provider": "openai",
        "connected": True,
        "auth_mode": "api_key",
        "expires_at": None,
    }
    assert "sk-secret" not in str(status)


def test_validate_anthropic_oauth_ok():
    bundle = validate_pasted("anthropic", "oauth", "  sk-ant-oat01-xyz  ")
    assert bundle.provider == "anthropic"
    assert bundle.auth_mode == "oauth"
    assert bundle.token_kind == "setup_token"
    assert bundle.oauth_token == "sk-ant-oat01-xyz"  # trimmed


def test_validate_anthropic_oauth_rejects_api_key():
    with pytest.raises(CredentialError, match="setup-token|setup token"):
        validate_pasted("anthropic", "oauth", "sk-ant-api03-nope")


def test_validate_anthropic_api_key_ok():
    bundle = validate_pasted("anthropic", "api_key", "sk-ant-api03-abc")
    assert bundle.auth_mode == "api_key"
    assert bundle.api_key == "sk-ant-api03-abc"


def test_validate_openai_oauth_ok():
    auth_json = '{"auth_mode":"chatgpt","tokens":{"access_token":"tok","refresh_token":"r","account_id":"a"}}'
    bundle = validate_pasted("openai", "oauth", auth_json)
    assert bundle.provider == "openai"
    assert bundle.auth_mode == "oauth"
    assert bundle.auth_json["tokens"]["access_token"] == "tok"


def test_validate_openai_oauth_rejects_non_json():
    with pytest.raises(CredentialError, match="auth.json"):
        validate_pasted("openai", "oauth", "not-json")


def test_validate_openai_oauth_rejects_missing_access_token():
    with pytest.raises(CredentialError, match="access_token"):
        validate_pasted("openai", "oauth", '{"tokens":{}}')


def test_validate_openai_api_key_ok():
    bundle = validate_pasted("openai", "api_key", "sk-proj-abc")
    assert bundle.api_key == "sk-proj-abc"


def test_validate_openai_api_key_rejects_anthropic_key():
    with pytest.raises(CredentialError):
        validate_pasted("openai", "api_key", "sk-ant-api03-abc")


def test_validate_rejects_unknown_provider_and_mode():
    with pytest.raises(CredentialError, match="provider"):
        validate_pasted("google", "oauth", "x")
    with pytest.raises(CredentialError, match="mode"):
        validate_pasted("openai", "magic", "x")


def test_validate_rejects_empty():
    with pytest.raises(CredentialError, match="empty"):
        validate_pasted("anthropic", "oauth", "   ")
