"""SSH fields on SandboxSpec default empty and never surface the key in env."""

from __future__ import annotations

from surogates.sandbox.base import SandboxSpec


def test_ssh_fields_default_empty():
    spec = SandboxSpec()
    assert spec.ssh_targets == []
    assert spec.ssh_key_material == {}


def test_ssh_fields_are_independent_instances():
    a = SandboxSpec()
    b = SandboxSpec()
    a.ssh_targets.append({"alias": "x"})
    a.ssh_key_material["k"] = {"private_key": "p", "passphrase": ""}
    assert b.ssh_targets == []
    assert b.ssh_key_material == {}


def test_ssh_key_material_never_in_env():
    spec = SandboxSpec(
        ssh_key_material={"prod": {"private_key": "SECRET", "passphrase": ""}},
    )
    assert "SECRET" not in repr(spec.env)
    assert spec.env == {}
