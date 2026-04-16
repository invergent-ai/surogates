"""Tests for surogates.sandbox.kubernetes.K8sSandbox.

Uses mocks for the kubernetes-asyncio API since tests don't run in a cluster.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from surogates.sandbox.base import SandboxSpec, SandboxStatus
from surogates.sandbox.kubernetes import K8sSandbox, _PodEntry


@pytest.fixture()
def sandbox() -> K8sSandbox:
    """Create a K8sSandbox with mocked K8s API."""
    return K8sSandbox(
        namespace="test-ns",
        service_account="test-sa",
        pod_ready_timeout=5,
        executor_path="/usr/local/bin/tool-executor",
        storage_settings=MagicMock(endpoint="http://minio:9000", access_key="key", secret_key="secret"),
        s3fs_image="s3fs:test",
    )


class TestBuildPodManifest:
    """Pod manifest construction."""

    def test_basic_manifest(self, sandbox: K8sSandbox):
        spec = SandboxSpec(
            image="test-image:latest",
            cpu="250m",
            memory="256Mi",
            cpu_limit="1",
            memory_limit="512Mi",
            env={"FOO": "bar"},
        )
        pod = sandbox._build_pod_manifest("abc123", "sandbox-abc123", "secret-abc", spec)

        assert pod.metadata.name == "sandbox-abc123"
        assert pod.metadata.namespace == "test-ns"
        assert pod.metadata.labels["app"] == "surogates-sandbox"
        assert pod.metadata.labels["surogates.ai/sandbox-id"] == "abc123"
        assert pod.spec.service_account_name == "test-sa"
        assert pod.spec.restart_policy == "Never"
        assert len(pod.spec.containers) == 2

        sandbox_container = pod.spec.containers[0]
        assert sandbox_container.name == "sandbox"
        assert sandbox_container.image == "test-image:latest"
        assert sandbox_container.resources.requests["cpu"] == "250m"
        assert sandbox_container.resources.requests["memory"] == "256Mi"
        assert sandbox_container.resources.limits["cpu"] == "1"
        assert sandbox_container.resources.limits["memory"] == "512Mi"

        s3fs_container = pod.spec.containers[1]
        assert s3fs_container.name == "s3fs"
        assert s3fs_container.image == "s3fs:test"
        assert s3fs_container.security_context.privileged is True

    def test_env_vars_passed(self, sandbox: K8sSandbox):
        spec = SandboxSpec(env={"MY_VAR": "my_value"})
        pod = sandbox._build_pod_manifest("id", "pod", "secret", spec)
        container = pod.spec.containers[0]
        env_names = {e.name: e.value for e in container.env}
        assert env_names["WORKSPACE_DIR"] == "/workspace"
        assert env_names["MY_VAR"] == "my_value"

    def test_s3_resource_parsed(self, sandbox: K8sSandbox):
        from surogates.sandbox.base import Resource
        spec = SandboxSpec(resources=[Resource(source_ref="s3://session-123", mount_path="/workspace")])
        pod = sandbox._build_pod_manifest("id", "pod", "secret", spec)
        s3fs = pod.spec.containers[1]
        # The s3fs env should contain the bucket name.
        env_map = {e.name: e.value for e in s3fs.env}
        assert env_map["S3_BUCKET"] == "session-123"


class TestStatusMapping:
    """Pod status → SandboxStatus mapping."""

    def test_running_ready(self):
        pod = MagicMock()
        pod.status.phase = "Running"
        pod.status.conditions = [MagicMock(type="Ready", status="True")]
        assert K8sSandbox._map_pod_status(pod) == SandboxStatus.RUNNING

    def test_running_not_ready(self):
        pod = MagicMock()
        pod.status.phase = "Running"
        pod.status.conditions = [MagicMock(type="Ready", status="False")]
        assert K8sSandbox._map_pod_status(pod) == SandboxStatus.PENDING

    def test_pending(self):
        pod = MagicMock()
        pod.status.phase = "Pending"
        pod.status.conditions = []
        assert K8sSandbox._map_pod_status(pod) == SandboxStatus.PENDING

    def test_failed(self):
        pod = MagicMock()
        pod.status.phase = "Failed"
        pod.status.conditions = []
        assert K8sSandbox._map_pod_status(pod) == SandboxStatus.FAILED

    def test_succeeded(self):
        pod = MagicMock()
        pod.status.phase = "Succeeded"
        pod.status.conditions = []
        assert K8sSandbox._map_pod_status(pod) == SandboxStatus.TERMINATED

    def test_no_status(self):
        pod = MagicMock()
        pod.status = None
        assert K8sSandbox._map_pod_status(pod) == SandboxStatus.PENDING


class TestResultJson:
    """Standard result JSON builder."""

    def test_success(self):
        result = json.loads(K8sSandbox._result_json(
            exit_code=0, stdout="hello", stderr="", truncated=False, timed_out=False,
        ))
        assert result["exit_code"] == 0
        assert result["stdout"] == "hello"
        assert result["timed_out"] is False

    def test_timeout(self):
        result = json.loads(K8sSandbox._result_json(
            exit_code=-1, stdout="", stderr="timed out", truncated=False, timed_out=True,
        ))
        assert result["timed_out"] is True
        assert result["exit_code"] == -1


class TestGetEntry:
    """Entry lookup."""

    def test_unknown_raises(self, sandbox: K8sSandbox):
        with pytest.raises(ValueError, match="Unknown sandbox"):
            sandbox._get_entry("nonexistent")

    def test_known_entry(self, sandbox: K8sSandbox):
        entry = _PodEntry(
            sandbox_id="abc",
            pod_name="sandbox-abc",
            secret_name="secret-abc",
            namespace="test-ns",
            spec=SandboxSpec(),
        )
        sandbox._pods["abc"] = entry
        assert sandbox._get_entry("abc") is entry


class TestDestroyUnknown:
    """Destroying an unknown sandbox should not raise."""

    async def test_destroy_unknown(self, sandbox: K8sSandbox):
        mock_api = AsyncMock()
        sandbox._api = mock_api
        await sandbox.destroy("nonexistent")
        # Should not raise, just log a warning.


class TestStatusUnknown:
    """Status of unknown sandbox returns TERMINATED."""

    async def test_status_unknown(self, sandbox: K8sSandbox):
        result = await sandbox.status("nonexistent")
        assert result == SandboxStatus.TERMINATED
