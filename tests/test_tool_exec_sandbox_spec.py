"""Tests for the K8s/S3 sandbox spec builder used by tool execution.

Covers the workspace-sharing fix: the mount source MUST be derived from
:func:`sandbox_session_key` (the root id) rather than the immediate
``session.id`` — otherwise a sandbox reprovisioned under a delegation
child or loop iteration would mount an empty per-session prefix.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from surogates.harness.tool_exec import (
    _WORKSPACE_MOUNT_PATH,
    _build_session_sandbox_spec,
)
from surogates.sandbox.pool import sandbox_session_key


def _session(*, id_=None, parent_id=None, config=None):
    return SimpleNamespace(
        id=id_ or uuid4(),
        parent_id=parent_id,
        config=config or {},
    )


def test_root_session_mounts_its_own_prefix():
    sid = uuid4()
    session = _session(id_=sid, config={"storage_bucket": "agent-a-bucket"})
    owner = sandbox_session_key(session)
    spec = _build_session_sandbox_spec(session, tenant=SimpleNamespace(), sandbox_owner=owner)

    sources = [r.source_ref for r in spec.resources]
    assert sources == [f"s3://agent-a-bucket/{sid}/"]
    mount_paths = [r.mount_path for r in spec.resources]
    assert mount_paths == [_WORKSPACE_MOUNT_PATH]


def test_delegation_child_mounts_root_prefix_via_sandbox_root_session_id():
    """The grandparent → parent → child chain must mount grandparent's prefix.

    This is the regression: before the fix, the source_ref used
    ``session.id`` (the child's id) while the pool owner key resolved
    to the root.  A pod reprovisioned under the child would mount an
    empty prefix.
    """
    root_id = uuid4()
    child = _session(
        id_=uuid4(),
        parent_id=uuid4(),
        config={
            "storage_bucket": "agent-a-bucket",
            "sandbox_root_session_id": str(root_id),
        },
    )
    owner = sandbox_session_key(child)
    spec = _build_session_sandbox_spec(child, tenant=SimpleNamespace(), sandbox_owner=owner)

    assert owner == str(root_id)
    sources = [r.source_ref for r in spec.resources]
    assert sources == [f"s3://agent-a-bucket/{root_id}/"]


def test_legacy_child_without_root_falls_back_to_parent_id():
    """Pre-deploy delegation children (no sandbox_root_session_id) still share.

    ``sandbox_session_key`` falls back to ``parent_id`` when the cached
    root is absent.  Mount source must use the same fallback.
    """
    parent_id = uuid4()
    child = _session(
        id_=uuid4(),
        parent_id=parent_id,
        config={"storage_bucket": "agent-a-bucket"},
    )
    owner = sandbox_session_key(child)
    spec = _build_session_sandbox_spec(child, tenant=SimpleNamespace(), sandbox_owner=owner)

    assert owner == str(parent_id)
    sources = [r.source_ref for r in spec.resources]
    assert sources == [f"s3://agent-a-bucket/{parent_id}/"]


def test_no_storage_bucket_emits_no_workspace_mount():
    session = _session(config={})
    owner = sandbox_session_key(session)
    spec = _build_session_sandbox_spec(session, tenant=SimpleNamespace(), sandbox_owner=owner)

    s3_resources = [r for r in spec.resources if r.source_ref.startswith("s3://")]
    assert s3_resources == []


def test_existing_workspace_mount_on_tenant_spec_is_not_duplicated():
    """A baseline spec that already has /workspace mounted must not get a second one.

    The duplicate guard checks ``mount_path``, not just any S3 resource —
    an unrelated S3 mount at a different path must not suppress the
    workspace mount.
    """
    from surogates.sandbox.base import Resource, SandboxSpec

    pre_existing = Resource(
        source_ref="s3://preset-bucket/preset/",
        mount_path=_WORKSPACE_MOUNT_PATH,
    )
    tenant = SimpleNamespace(
        sandbox_spec=SandboxSpec(resources=[pre_existing]),
    )
    session = _session(config={"storage_bucket": "agent-a-bucket"})
    owner = sandbox_session_key(session)
    spec = _build_session_sandbox_spec(session, tenant=tenant, sandbox_owner=owner)

    workspace_mounts = [r for r in spec.resources if r.mount_path == _WORKSPACE_MOUNT_PATH]
    assert len(workspace_mounts) == 1
    assert workspace_mounts[0].source_ref == "s3://preset-bucket/preset/"


def test_unrelated_s3_resource_does_not_suppress_workspace_mount():
    """An S3 resource at a non-workspace mount path must not suppress workspace setup.

    Regression for an over-broad guard: previously the code skipped the
    workspace append whenever ANY S3 resource was present, even one
    unrelated to the workspace.
    """
    from surogates.sandbox.base import Resource, SandboxSpec

    unrelated = Resource(
        source_ref="s3://other-bucket/preset/",
        mount_path="/preset",
    )
    tenant = SimpleNamespace(
        sandbox_spec=SandboxSpec(resources=[unrelated]),
    )
    sid = uuid4()
    session = _session(id_=sid, config={"storage_bucket": "agent-a-bucket"})
    owner = sandbox_session_key(session)
    spec = _build_session_sandbox_spec(session, tenant=tenant, sandbox_owner=owner)

    by_mount = {r.mount_path: r.source_ref for r in spec.resources}
    assert by_mount["/preset"] == "s3://other-bucket/preset/"
    assert by_mount[_WORKSPACE_MOUNT_PATH] == f"s3://agent-a-bucket/{sid}/"


def test_baseline_tenant_spec_is_not_mutated():
    """Building a session spec must not mutate the shared tenant baseline.

    Two different sessions on the same tenant context must each get
    their own correct workspace mount — the first call's mount must
    not bleed into the tenant baseline and end up applied to the
    second session.
    """
    from surogates.sandbox.base import SandboxSpec

    baseline = SandboxSpec()
    tenant = SimpleNamespace(sandbox_spec=baseline)

    session_a = _session(config={"storage_bucket": "bucket-a"})
    spec_a = _build_session_sandbox_spec(
        session_a, tenant=tenant, sandbox_owner=sandbox_session_key(session_a),
    )
    assert any(r.mount_path == _WORKSPACE_MOUNT_PATH for r in spec_a.resources)
    # Baseline is untouched after the first build.
    assert baseline.resources == []
    assert "_passthrough_done" not in baseline.env

    session_b = _session(config={"storage_bucket": "bucket-b"})
    spec_b = _build_session_sandbox_spec(
        session_b, tenant=tenant, sandbox_owner=sandbox_session_key(session_b),
    )
    workspace_b = next(r for r in spec_b.resources if r.mount_path == _WORKSPACE_MOUNT_PATH)
    # Critical: session B's mount points at session B's bucket, not A's.
    assert workspace_b.source_ref.startswith("s3://bucket-b/")


def test_env_passthrough_baseline_is_not_mutated():
    """Env passthrough must not bake the sentinel into the shared baseline."""
    from surogates.sandbox.base import SandboxSpec

    baseline = SandboxSpec()
    tenant = SimpleNamespace(sandbox_spec=baseline)
    session = _session(config={"storage_bucket": "agent-a-bucket"})

    spec = _build_session_sandbox_spec(
        session, tenant=tenant, sandbox_owner=sandbox_session_key(session),
    )
    assert spec.env.get("_passthrough_done") == "1"
    # Baseline env stays clean — second session would otherwise skip
    # passthrough entirely because the sentinel was permanently set.
    assert "_passthrough_done" not in baseline.env


def test_no_tenant_baseline_reads_sandbox_settings_env(monkeypatch):
    """Without a tenant baseline, SUROGATES_SANDBOX_DEFAULT_* env wins.

    Regression: previously fell through to ``SandboxSpec()`` dataclass
    defaults, ignoring the helm chart's
    ``SUROGATES_SANDBOX_DEFAULT_CPU_LIMIT`` (and friends).  Bumping the
    chart's ``sandbox.resources`` had no effect on the running pod.
    """
    monkeypatch.setenv("SUROGATES_SANDBOX_DEFAULT_CPU", "3")
    monkeypatch.setenv("SUROGATES_SANDBOX_DEFAULT_CPU_LIMIT", "11")
    monkeypatch.setenv("SUROGATES_SANDBOX_DEFAULT_MEMORY", "5Gi")
    monkeypatch.setenv("SUROGATES_SANDBOX_DEFAULT_MEMORY_LIMIT", "13Gi")

    session = _session(config={"storage_bucket": "agent-a-bucket"})
    spec = _build_session_sandbox_spec(
        session, tenant=SimpleNamespace(), sandbox_owner=sandbox_session_key(session),
    )
    assert spec.cpu == "3"
    assert spec.cpu_limit == "11"
    assert spec.memory == "5Gi"
    assert spec.memory_limit == "13Gi"


def test_no_tenant_baseline_no_env_uses_aligned_defaults():
    """Without env overrides, fallback matches the documented spec defaults."""
    from surogates.sandbox.base import SandboxSpec

    session = _session(config={})
    spec = _build_session_sandbox_spec(
        session, tenant=SimpleNamespace(), sandbox_owner=sandbox_session_key(session),
    )
    fresh = SandboxSpec()
    assert spec.cpu == fresh.cpu
    assert spec.cpu_limit == fresh.cpu_limit
    assert spec.memory == fresh.memory
    assert spec.memory_limit == fresh.memory_limit


def test_spec_sets_session_id_and_workspace_path():
    session = _session(
        config={
            "storage_bucket": "agent-bucket",
            "workspace_path": "/data/agent-bucket/sessions/root-1",
        },
    )

    spec = _build_session_sandbox_spec(
        session, tenant=SimpleNamespace(), sandbox_owner="root-1",
    )

    assert spec.session_id == "root-1"
    assert spec.workspace_path == "/data/agent-bucket/sessions/root-1"


def test_spec_workspace_path_none_when_absent():
    session = _session(config={"storage_bucket": "agent-bucket"})

    spec = _build_session_sandbox_spec(
        session, tenant=SimpleNamespace(), sandbox_owner="root-1",
    )

    assert spec.session_id == "root-1"
    assert spec.workspace_path is None


def test_managed_channel_mounts_boundary_workspace_prefix():
    sid = uuid4()
    session = _session(
        id_=sid,
        config={
            "storage_bucket": "agent-a-bucket",
            "storage_key_prefix": "project/agent",
            "memory_boundary": "slack:c:G1",
            "workspace_boundary": "slack:c:G1",
        },
    )

    spec = _build_session_sandbox_spec(
        session,
        tenant=SimpleNamespace(),
        sandbox_owner=sandbox_session_key(session),
    )

    sources = [r.source_ref for r in spec.resources]
    assert sources == [
        "s3://agent-a-bucket/project/agent/boundaries/slack:c:G1/workspace/"
    ]


def test_child_mounts_parent_boundary_workspace_prefix():
    parent_id = uuid4()
    child = _session(
        id_=uuid4(),
        parent_id=parent_id,
        config={
            "storage_bucket": "agent-a-bucket",
            "storage_key_prefix": "project/agent",
            "sandbox_root_session_id": str(parent_id),
            "workspace_boundary": "slack:c:G1",
        },
    )

    spec = _build_session_sandbox_spec(
        child,
        tenant=SimpleNamespace(),
        sandbox_owner=sandbox_session_key(child),
    )

    sources = [r.source_ref for r in spec.resources]
    assert sources == [
        "s3://agent-a-bucket/project/agent/boundaries/slack:c:G1/workspace/"
    ]
