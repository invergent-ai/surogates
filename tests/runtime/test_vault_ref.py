"""Tests for vault://-style credential references.

AgentRuntimeContext carries every API key as a
``vault://<credential_id>`` reference, never the raw value.  This
keeps secrets off the runtime-config payload (which travels over
HTTP to the worker) and means rotation lands by updating the vault
row, not by republishing config.
"""

from __future__ import annotations

import pytest

from surogates.tenant.credentials import (
    InvalidVaultRef, parse_vault_ref,
)


def test_parse_vault_ref_extracts_uuid_form():
    assert parse_vault_ref("vault://acme-key") == "acme-key"
    assert parse_vault_ref(
        "vault://550e8400-e29b-41d4-a716-446655440000"
    ) == "550e8400-e29b-41d4-a716-446655440000"


def test_parse_vault_ref_rejects_missing_scheme():
    with pytest.raises(InvalidVaultRef):
        parse_vault_ref("just-a-name")


def test_parse_vault_ref_rejects_wrong_scheme():
    """Reject ``https://`` / ``s3://`` / etc. — the worker must not
    accidentally fetch a URL when it meant a vault credential."""
    with pytest.raises(InvalidVaultRef):
        parse_vault_ref("https://acme-key")


def test_parse_vault_ref_rejects_empty_credential():
    with pytest.raises(InvalidVaultRef):
        parse_vault_ref("vault://")


def test_parse_vault_ref_rejects_empty_input():
    with pytest.raises(InvalidVaultRef):
        parse_vault_ref("")
