"""Unit tests for the coding-agent credential bundle (no DB)."""

from __future__ import annotations

from surogates.coding_agents.credentials import (
    PROVIDERS,
    CRED_NAME,
    CredentialBundle,
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
