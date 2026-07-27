"""``_sign_in_provider_from_claims`` extracts the Firebase sign-in method
(``firebase.sign_in_provider``) that the web app uses to decide whether to
offer password reset."""

from __future__ import annotations

from surogates.api.routes.auth import _sign_in_provider_from_claims


def test_extracts_password_provider():
    claims = {"sub": "u", "firebase": {"sign_in_provider": "password"}}
    assert _sign_in_provider_from_claims(claims) == "password"


def test_extracts_federated_provider():
    claims = {"firebase": {"sign_in_provider": "google.com"}}
    assert _sign_in_provider_from_claims(claims) == "google.com"


def test_missing_firebase_block_returns_none():
    assert _sign_in_provider_from_claims({"sub": "u"}) is None


def test_firebase_block_without_method_returns_none():
    assert _sign_in_provider_from_claims({"firebase": {}}) is None


def test_non_dict_firebase_block_returns_none():
    assert _sign_in_provider_from_claims({"firebase": "nope"}) is None


def test_empty_method_returns_none():
    assert _sign_in_provider_from_claims({"firebase": {"sign_in_provider": ""}}) is None
