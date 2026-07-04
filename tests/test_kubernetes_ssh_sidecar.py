"""The k8s sandbox adds an isolated ssh-agent sidecar for SSH sessions."""

from __future__ import annotations

import pytest

from surogates.sandbox.base import SandboxSpec, SandboxUnavailableError
from surogates.sandbox.kubernetes import K8sSandbox


def _backend(**kw):
    return K8sSandbox(
        namespace="ns", service_account="sa", pod_ready_timeout=1,
        executor_port=8080, storage_settings=None, s3fs_image="img",
        s3_endpoint="", mcp_proxy_url="", **kw,
    )


def _ssh_spec():
    return SandboxSpec(
        env={"SSH_AUTH_SOCK": "/ssh-agent/auth.sock",
             "SUROGATES_SSH_TARGETS": "[]", "ORG_ID": "0", "USER_ID": "0"},
        ssh_key_material={"prod": {"private_key": "SECRET", "passphrase": ""}},
    )


def test_manifest_has_isolated_ssh_sidecar():
    pod = _backend()._build_pod_manifest(
        "sid123456789a", "sandbox-x", "sec", _ssh_spec(),
        executor_token="t", ssh_secret_name="sandbox-ssh-x",
    )
    names = {c.name for c in pod.spec.containers}
    assert "ssh-agent" in names
    assert pod.spec.automount_service_account_token is False
    assert not getattr(pod.spec, "share_process_namespace", None)
    # fsGroup = the sandbox uid so the sidecar reads the group-readable key.
    assert pod.spec.security_context.fs_group == 1000


def test_sidecar_is_hardened_and_key_only_in_sidecar():
    pod = _backend()._build_pod_manifest(
        "sid1", "sandbox-x", "sec", _ssh_spec(),
        executor_token="t", ssh_secret_name="sandbox-ssh-x",
    )
    sidecar = next(c for c in pod.spec.containers if c.name == "ssh-agent")
    sc = sidecar.security_context
    assert sc.run_as_non_root is True
    assert sc.read_only_root_filesystem is True
    assert sc.allow_privilege_escalation is False
    assert sc.capabilities.drop == ["ALL"]

    main = next(c for c in pod.spec.containers if c.name == "sandbox")
    sidecar_vols = {m.name for m in sidecar.volume_mounts}
    main_vols = {m.name for m in main.volume_mounts}
    # keys mounted ONLY in the sidecar; socket shared with the main container
    assert "ssh-keys" in sidecar_vols and "ssh-keys" not in main_vols
    assert "ssh-agent-sock" in sidecar_vols and "ssh-agent-sock" in main_vols
    # the main container must NOT mount the workspace-less key volume
    assert "workspace" in main_vols and "workspace" not in sidecar_vols


def test_socket_volume_is_in_memory_and_keys_are_0440():
    pod = _backend()._build_pod_manifest(
        "sid1", "sandbox-x", "sec", _ssh_spec(),
        executor_token="t", ssh_secret_name="sandbox-ssh-x",
    )
    sock = next(v for v in pod.spec.volumes if v.name == "ssh-agent-sock")
    assert sock.empty_dir.medium == "Memory"
    keys = next(v for v in pod.spec.volumes if v.name == "ssh-keys")
    assert keys.secret.secret_name == "sandbox-ssh-x"
    # Group-readable so the sidecar's fsGroup gid can read the key (the
    # volume is mounted only in the sidecar, so this is not an exposure).
    assert keys.secret.default_mode == 0o440


def test_sidecar_runs_as_sandbox_uid_for_agent_access():
    # ssh-agent enforces a same-uid client check: the sidecar MUST run as the
    # sandbox container's uid, or the main container's ssh can't use the socket.
    pod = _backend()._build_pod_manifest(
        "sid1", "sandbox-x", "sec", _ssh_spec(),
        executor_token="t", ssh_secret_name="sandbox-ssh-x",
    )
    sidecar = next(c for c in pod.spec.containers if c.name == "ssh-agent")
    assert sidecar.security_context.run_as_user == 1000
    # No socket-widening chmod — same-uid ownership is sufficient.
    assert "chmod 0660" not in sidecar.command[-1]


def test_no_sidecar_without_key_material():
    spec = SandboxSpec(env={"ORG_ID": "0", "USER_ID": "0"})
    pod = _backend()._build_pod_manifest(
        "sid", "sandbox-x", "sec", spec, executor_token="t", ssh_secret_name=None,
    )
    assert {c.name for c in pod.spec.containers} == {"sandbox", "s3fs"}
    assert pod.spec.automount_service_account_token is None
    assert not any(v.name == "ssh-keys" for v in pod.spec.volumes)
    # Non-ssh pods carry no pod-level security context (fsGroup only exists
    # to bridge the ssh sidecar/main-container key + socket sharing).
    assert getattr(pod.spec, "security_context", None) is None


def test_require_ssh_egress_enforced_fails_closed():
    with pytest.raises(SandboxUnavailableError):
        _backend(ssh_egress_enforced=False)._require_ssh_egress_enforced(_ssh_spec())


def test_require_ssh_egress_enforced_allows_when_enforced():
    # Should not raise.
    _backend(ssh_egress_enforced=True)._require_ssh_egress_enforced(_ssh_spec())


def test_require_ssh_egress_enforced_noop_without_ssh():
    _backend(ssh_egress_enforced=False)._require_ssh_egress_enforced(SandboxSpec())
