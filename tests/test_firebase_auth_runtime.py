"""Tests for the Surogates runtime auth settings + Firebase helper."""

from __future__ import annotations

import pytest

from surogates.tenant.auth.firebase import firebase_auth_provider_name


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
