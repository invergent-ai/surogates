"""Unit tests for the SSH key vault bundle (no DB)."""

from __future__ import annotations

import pytest

from surogates.ssh_access.bundle import SSHKeyBundle, validate_private_key

_KEY = "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----\n"


def test_roundtrip_preserves_fields():
    bundle = SSHKeyBundle(private_key=_KEY, passphrase="pw")
    restored = SSHKeyBundle.from_json(bundle.to_json())
    assert restored.private_key == _KEY
    assert restored.passphrase == "pw"
    assert restored.version == 1


def test_roundtrip_without_passphrase():
    bundle = SSHKeyBundle(private_key=_KEY)
    restored = SSHKeyBundle.from_json(bundle.to_json())
    assert restored.passphrase is None


def test_status_never_leaks_secret():
    status = SSHKeyBundle(private_key=_KEY, passphrase="pw").status()
    assert "private_key" not in status
    assert "passphrase" not in status
    assert status["connected"] is True
    assert status["has_passphrase"] is True


def test_validate_rejects_non_key():
    with pytest.raises(ValueError):
        validate_private_key("not a key")


def test_validate_accepts_and_ensures_trailing_newline():
    # _KEY already ends with a newline; surrounding whitespace is stripped and
    # exactly one trailing newline is guaranteed (ssh-add requires it).
    assert validate_private_key(f"  {_KEY}  ") == _KEY
    assert validate_private_key(_KEY.strip()) == _KEY
