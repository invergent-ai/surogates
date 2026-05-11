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
        storage_settings=MagicMock(endpoint="http://minio:9000", access_key="key", secret_key="secret", region=""),
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
        spec = SandboxSpec(
            resources=[
                Resource(
                    source_ref="s3://agent-test/sessions/session-123/",
                    mount_path="/workspace",
                ),
            ],
        )
        pod = sandbox._build_pod_manifest("id", "pod", "secret", spec)
        s3fs = pod.spec.containers[1]
        # The s3fs env should contain the bucket path.
        env_map = {e.name: e.value for e in s3fs.env}
        assert env_map["S3_BUCKET_PATH"] == "agent-test:/sessions/session-123"

    def test_s3fs_env_includes_region(self, sandbox: K8sSandbox):
        # s3fs needs S3_REGION; without it the sidecar runs with
        # ``-o endpoint=garage`` (its hardcoded entrypoint default) and AWS
        # rejects the pre-mount service check, leaving the pod NotReady.
        pod = sandbox._build_pod_manifest("id", "pod", "secret", SandboxSpec())
        s3fs = pod.spec.containers[1]
        env_map = {e.name: e.value for e in s3fs.env}
        assert "S3_REGION" in env_map


class TestResolveS3Region:
    """``S3_REGION`` is the SigV4 signing label s3fs sends.  Mismatching
    it against the actual bucket region makes AWS S3 abort the mount."""

    def _sb(self, *, region: str = "", endpoint: str = "") -> K8sSandbox:
        return K8sSandbox(
            namespace="ns", service_account="sa",
            storage_settings=MagicMock(
                endpoint=endpoint, access_key="k", secret_key="s",
                region=region,
            ),
            s3_endpoint=endpoint,
            s3fs_image="s3fs:test",
        )

    def test_explicit_region_wins(self):
        sb = self._sb(region="ap-southeast-2", endpoint="https://s3.eu-central-1.amazonaws.com")
        assert sb._resolve_s3_region("https://s3.eu-central-1.amazonaws.com") == "ap-southeast-2"

    def test_aws_endpoint_yields_region(self):
        sb = self._sb()
        assert sb._resolve_s3_region("https://s3.eu-central-1.amazonaws.com") == "eu-central-1"

    def test_aws_legacy_dash_endpoint(self):
        sb = self._sb()
        assert sb._resolve_s3_region("https://s3-us-west-2.amazonaws.com") == "us-west-2"

    def test_bare_aws_endpoint_falls_back_to_platform_default(self):
        # Bare ``s3.amazonaws.com`` carries no region in the host;
        # platform default applies.
        sb = self._sb()
        assert sb._resolve_s3_region("https://s3.amazonaws.com") == K8sSandbox._DEFAULT_REGION

    def test_non_aws_endpoint_falls_back_to_platform_default(self):
        # Garage/MinIO ignore the region label, so the platform default
        # is fine — they sign and route by URL, not by region.
        sb = self._sb()
        assert sb._resolve_s3_region("http://garage.surogates.svc:3900") == K8sSandbox._DEFAULT_REGION

    def test_empty_endpoint_falls_back_to_platform_default(self):
        sb = self._sb()
        assert sb._resolve_s3_region("") == K8sSandbox._DEFAULT_REGION

    def test_platform_default_region_is_eu_central_1(self):
        # Platform deployment lives in eu-central-1; AWS rejects the
        # mount when the SigV4 region label doesn't match the bucket's.
        assert K8sSandbox._DEFAULT_REGION == "eu-central-1"

    def test_no_storage_settings_falls_back(self):
        sb = K8sSandbox(namespace="ns", service_account="sa", s3fs_image="x")
        assert sb._resolve_s3_region("https://s3.eu-central-1.amazonaws.com") == "eu-central-1"


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


class TestDestroy:
    """Destroying a sandbox deletes its Kubernetes resources."""

    async def test_destroy_force_deletes_pod_and_secret(self, sandbox: K8sSandbox):
        api = MagicMock()
        api.delete_namespaced_pod = AsyncMock()
        api.delete_namespaced_secret = AsyncMock()
        sandbox._api = api
        sandbox._pods["abc"] = _PodEntry(
            sandbox_id="abc",
            pod_name="sandbox-abc",
            secret_name="secret-abc",
            namespace="test-ns",
            spec=SandboxSpec(),
        )

        await sandbox.destroy("abc")

        api.delete_namespaced_pod.assert_awaited_once_with(
            "sandbox-abc",
            "test-ns",
            grace_period_seconds=0,
        )
        api.delete_namespaced_secret.assert_awaited_once_with(
            "secret-abc",
            "test-ns",
        )
        assert "abc" not in sandbox._pods


class TestStatusUnknown:
    """Status of unknown sandbox returns TERMINATED."""

    async def test_status_unknown(self, sandbox: K8sSandbox):
        result = await sandbox.status("nonexistent")
        assert result == SandboxStatus.TERMINATED


class TestStatusReadFailures:
    """``status()`` must not flap to FAILED on transient API errors --
    that triggers the pool destroy/reprovision loop on a healthy pod."""

    async def test_404_marks_terminated_and_evicts_entry(
        self, sandbox: K8sSandbox,
    ):
        from kubernetes_asyncio.client import ApiException
        entry = _PodEntry(
            sandbox_id="sid", pod_name="pod-x", secret_name="sec-x",
            namespace="test-ns", spec=SandboxSpec(),
            status=SandboxStatus.RUNNING,
        )
        sandbox._pods["sid"] = entry
        api = AsyncMock()
        api.read_namespaced_pod.side_effect = ApiException(status=404, reason="Not Found")
        sandbox._api = api

        result = await sandbox.status("sid")
        assert result == SandboxStatus.TERMINATED
        assert "sid" not in sandbox._pods  # evicted

    async def test_403_returns_cached_status_not_failed(
        self, sandbox: K8sSandbox,
    ):
        # Reproduces the destroy/reprovision loop bug: a transient
        # status-read 403 should NOT flap a healthy pod to FAILED.
        from kubernetes_asyncio.client import ApiException
        entry = _PodEntry(
            sandbox_id="sid", pod_name="pod-x", secret_name="sec-x",
            namespace="test-ns", spec=SandboxSpec(),
            status=SandboxStatus.RUNNING,
        )
        sandbox._pods["sid"] = entry
        api = AsyncMock()
        api.read_namespaced_pod.side_effect = ApiException(status=403, reason="Forbidden")
        sandbox._api = api

        result = await sandbox.status("sid")
        assert result == SandboxStatus.RUNNING  # cached, NOT FAILED
        assert "sid" in sandbox._pods  # still tracked

    async def test_500_returns_cached_status_not_failed(
        self, sandbox: K8sSandbox,
    ):
        from kubernetes_asyncio.client import ApiException
        entry = _PodEntry(
            sandbox_id="sid", pod_name="pod-x", secret_name="sec-x",
            namespace="test-ns", spec=SandboxSpec(),
            status=SandboxStatus.PENDING,
        )
        sandbox._pods["sid"] = entry
        api = AsyncMock()
        api.read_namespaced_pod.side_effect = ApiException(status=500, reason="Server Error")
        sandbox._api = api

        result = await sandbox.status("sid")
        assert result == SandboxStatus.PENDING

    async def test_uses_pods_not_pods_status_endpoint(
        self, sandbox: K8sSandbox,
    ):
        # Worker RBAC grants ``pods`` (verb=get) but not ``pods/status``;
        # this asserts we call the cheaper-RBAC endpoint.
        entry = _PodEntry(
            sandbox_id="sid", pod_name="pod-x", secret_name="sec-x",
            namespace="test-ns", spec=SandboxSpec(),
        )
        sandbox._pods["sid"] = entry
        pod = MagicMock()
        pod.status.phase = "Running"
        pod.status.conditions = [MagicMock(type="Ready", status="True")]
        api = AsyncMock()
        api.read_namespaced_pod.return_value = pod
        sandbox._api = api

        await sandbox.status("sid")
        api.read_namespaced_pod.assert_called_once_with("pod-x", "test-ns")
        api.read_namespaced_pod_status.assert_not_called()


class TestFailureClassification:
    """provision/execute infra failures raise SandboxUnavailableError with
    a triage-friendly reason rather than leaking raw stack traces."""

    def test_classify_create_pod_403_extracts_message(
        self, sandbox: K8sSandbox,
    ):
        from kubernetes_asyncio.client import ApiException
        from surogates.sandbox.kubernetes import K8sSandbox as KS

        exc = ApiException(status=403, reason="Forbidden")
        exc.body = json.dumps({
            "message": 'serviceaccount "surogates-sandbox" not found',
        })
        reason = KS._classify_create_pod_failure(exc)
        assert "RBAC" in reason
        assert "surogates-sandbox" in reason

    def test_classify_create_pod_404_namespace(
        self, sandbox: K8sSandbox,
    ):
        from kubernetes_asyncio.client import ApiException
        from surogates.sandbox.kubernetes import K8sSandbox as KS

        exc = ApiException(status=404, reason="Not Found")
        exc.body = json.dumps({"message": 'namespace "missing" not found'})
        reason = KS._classify_create_pod_failure(exc)
        assert "missing" in reason
        assert "404" not in reason  # 404 is summarized, not raw

    def test_classify_create_pod_unknown_status_includes_code(
        self, sandbox: K8sSandbox,
    ):
        from kubernetes_asyncio.client import ApiException
        from surogates.sandbox.kubernetes import K8sSandbox as KS

        exc = ApiException(status=500, reason="Internal")
        exc.body = json.dumps({"message": "etcd timeout"})
        reason = KS._classify_create_pod_failure(exc)
        assert "500" in reason
        assert "etcd timeout" in reason

    def test_classify_exec_403_calls_out_pod_exec_permission(
        self, sandbox: K8sSandbox,
    ):
        from surogates.sandbox.kubernetes import K8sSandbox as KS

        class _FakeWsErr(Exception):
            status = 403

        reason = KS._classify_exec_failure("sandbox-abc", _FakeWsErr())
        assert "sandbox-abc" in reason
        assert "pods/exec" in reason

    def test_classify_exec_404_says_pod_missing(
        self, sandbox: K8sSandbox,
    ):
        from surogates.sandbox.kubernetes import K8sSandbox as KS

        class _FakeWsErr(Exception):
            status = 404

        reason = KS._classify_exec_failure("sandbox-xyz", _FakeWsErr())
        assert "sandbox-xyz" in reason
        assert "not found" in reason.lower()

    async def test_provision_pod_create_403_raises_sandbox_unavailable(
        self, sandbox: K8sSandbox,
    ):
        from kubernetes_asyncio.client import ApiException
        from surogates.sandbox.base import SandboxUnavailableError

        api = AsyncMock()
        body = json.dumps({"message": 'serviceaccount "x" not found'})
        api.create_namespaced_pod.side_effect = ApiException(
            status=403, reason="Forbidden",
        )
        api.create_namespaced_pod.side_effect.body = body
        api.delete_namespaced_secret = AsyncMock()
        api.create_namespaced_secret = AsyncMock()
        sandbox._api = api

        with pytest.raises(SandboxUnavailableError) as ctx:
            await sandbox.provision(SandboxSpec())
        assert "RBAC" in str(ctx.value)


class TestSandboxUnavailableResult:
    """The shared result helper yields a recognisable error envelope."""

    def test_envelope_shape(self):
        from surogates.sandbox.base import sandbox_unavailable_result

        out = json.loads(
            sandbox_unavailable_result(
                "Pod creation forbidden", tools_affected=["terminal"],
            )
        )
        assert out["error"] == "sandbox_unavailable"
        assert out["reason"] == "Pod creation forbidden"
        assert out["tools_affected"] == ["terminal"]
        assert "Do not retry sandbox tools" in out["guidance"]

    def test_omits_tools_affected_when_unset(self):
        from surogates.sandbox.base import sandbox_unavailable_result

        out = json.loads(sandbox_unavailable_result("x"))
        assert "tools_affected" not in out
