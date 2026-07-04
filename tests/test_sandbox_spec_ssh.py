"""The sandbox spec builder resolves SSH keys without leaking them to env."""

from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from surogates.harness.tool_exec import _build_session_sandbox_spec


def _session(config):
    return SimpleNamespace(id=str(uuid4()), agent_id="a1", config=config)


def _tenant(org_id="o1", user_id="u1", service_account_id=None):
    return SimpleNamespace(
        org_id=org_id, user_id=user_id, service_account_id=service_account_id,
    )


class _FakeVault:
    def __init__(self, bundles):
        self._b = bundles  # {name: json}
        self.calls = []

    async def resolve_ref(self, ref, *, org_id, user_id=None, service_account_id=None):
        self.calls.append((ref, org_id, user_id, service_account_id))
        name = ref.split("vault://ssh_key:", 1)[-1]
        return self._b.get(name)


_TARGET = {
    "alias": "deploy", "host": "deploy.example.com", "port": 22,
    "user": "ubuntu", "key_name": "prod",
    "host_key": "deploy.example.com ssh-ed25519 AAAA",
}


@pytest.mark.asyncio
async def test_spec_populates_ssh_without_key_in_env():
    session = _session({"ssh_targets": [_TARGET]})
    vault = _FakeVault(
        {"prod": json.dumps({"private_key": "SECRETKEY", "passphrase": None})},
    )
    spec = await _build_session_sandbox_spec(
        session, _tenant(), session.id, credential_vault=vault,
    )
    assert spec.env["SSH_AUTH_SOCK"] == "/ssh-agent/auth.sock"
    assert "deploy" in spec.env["SUROGATES_SSH_TARGETS"]
    assert spec.env["SUROGATES_SSH_KNOWN_HOSTS"].strip() == _TARGET["host_key"]
    assert spec.ssh_key_material["prod"]["private_key"] == "SECRETKEY"
    # The secret must never appear in env, and the targets JSON must not carry
    # the host key (that travels via known_hosts).
    assert "SECRETKEY" not in json.dumps(spec.env)
    assert "host_key" not in spec.env["SUROGATES_SSH_TARGETS"]


@pytest.mark.asyncio
async def test_no_targets_no_ssh_env():
    spec = await _build_session_sandbox_spec(
        _session({}), _tenant(), "sid", credential_vault=None,
    )
    assert "SSH_AUTH_SOCK" not in spec.env
    assert spec.ssh_key_material == {}
    assert spec.ssh_targets == []


@pytest.mark.asyncio
async def test_unpinned_target_fails_closed():
    session = _session({"ssh_targets": [{**_TARGET, "host_key": None}]})
    vault = _FakeVault({"prod": json.dumps({"private_key": "K"})})
    spec = await _build_session_sandbox_spec(
        session, _tenant(), session.id, credential_vault=vault,
    )
    assert "SSH_AUTH_SOCK" not in spec.env
    assert spec.ssh_key_material == {}


@pytest.mark.asyncio
async def test_unresolved_key_drops_target():
    session = _session({"ssh_targets": [_TARGET]})
    vault = _FakeVault({})  # key "prod" not present
    spec = await _build_session_sandbox_spec(
        session, _tenant(), session.id, credential_vault=vault,
    )
    assert "SSH_AUTH_SOCK" not in spec.env
    assert spec.ssh_targets == []


@pytest.mark.asyncio
async def test_service_account_principal_used_for_resolution():
    session = _session({"ssh_targets": [_TARGET]})
    vault = _FakeVault({"prod": json.dumps({"private_key": "K"})})
    await _build_session_sandbox_spec(
        session, _tenant(user_id="u1", service_account_id="sa1"), session.id,
        credential_vault=vault,
    )
    _ref, _org, uid, said = vault.calls[0]
    assert uid is None and said == "sa1"
