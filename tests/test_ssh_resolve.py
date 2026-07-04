"""Unit tests for SSH key resolution and target config rendering (no DB)."""

from __future__ import annotations

import json

import pytest

from surogates.ssh_access.resolve import (
    build_known_hosts,
    build_ssh_config,
    render_ssh_targets_prompt,
    resolve_ssh_key,
    ssh_key_names,
    ssh_key_ref,
    validate_targets,
)

_T = {
    "alias": "deploy", "host": "deploy.example.com", "port": 22,
    "user": "ubuntu", "key_name": "prod",
    "host_key": "deploy.example.com ssh-ed25519 AAAA",
}


def test_ref_format():
    assert ssh_key_ref("prod") == "vault://ssh_key:prod"


def test_validate_rejects_duplicate_alias():
    with pytest.raises(ValueError):
        validate_targets([_T, {**_T, "host": "b.example.com"}])


def test_validate_rejects_missing_fields():
    with pytest.raises(ValueError):
        validate_targets([{"alias": "x", "host": "", "key_name": "k"}])


def test_validate_rejects_bad_port():
    with pytest.raises(ValueError):
        validate_targets([{**_T, "port": 0}])


def test_validate_normalizes_blank_user_to_none():
    out = validate_targets([{**_T, "user": "  "}])
    assert out[0]["user"] is None
    assert out[0]["port"] == 22


def test_config_pins_socket_and_strict_checking():
    cfg = build_ssh_config(validate_targets([_T]))
    assert "Host deploy" in cfg
    assert "HostName deploy.example.com" in cfg
    assert "User ubuntu" in cfg
    assert "IdentityAgent ${SSH_AUTH_SOCK}" in cfg
    assert "StrictHostKeyChecking yes" in cfg
    assert "IdentitiesOnly yes" in cfg


def test_config_empty_for_no_targets():
    assert build_ssh_config([]) == ""


def test_known_hosts_only_from_host_key():
    assert build_known_hosts([_T]).strip() == _T["host_key"]
    assert build_known_hosts([{**_T, "host_key": None}]) == ""


def test_prompt_lists_aliases_and_is_empty_when_none():
    assert "deploy" in render_ssh_targets_prompt([_T])
    assert "ubuntu@deploy.example.com" in render_ssh_targets_prompt([_T])
    assert render_ssh_targets_prompt([]) == ""


def test_key_names_distinct():
    assert ssh_key_names([_T, {**_T, "alias": "x"}]) == ["prod"]


class _FakeVault:
    def __init__(self, bundles):
        self._b = bundles
        self.calls = []

    async def resolve_ref(self, ref, *, org_id, user_id=None, service_account_id=None):
        self.calls.append((ref, org_id, user_id, service_account_id))
        name = ref.split("vault://ssh_key:", 1)[-1]
        return self._b.get(name)


@pytest.mark.asyncio
async def test_resolve_ssh_key_service_account_principal():
    vault = _FakeVault({"prod": json.dumps({"private_key": "SECRET", "passphrase": "pw"})})
    bundle = await resolve_ssh_key(
        vault, org_id="o1", name="prod", user_id="u1", service_account_id="sa1",
    )
    assert bundle is not None
    assert bundle.private_key == "SECRET"
    # SA wins over user, and user_id is nulled.
    ref, org, uid, said = vault.calls[0]
    assert ref == "vault://ssh_key:prod"
    assert org == "o1" and uid is None and said == "sa1"


@pytest.mark.asyncio
async def test_resolve_ssh_key_missing_returns_none():
    vault = _FakeVault({})
    assert await resolve_ssh_key(vault, org_id="o1", name="nope") is None
