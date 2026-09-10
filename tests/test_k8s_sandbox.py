"""Tests for surogates.sandbox.kubernetes.K8sSandbox.

Uses mocks for the kubernetes-asyncio API since tests don't run in a cluster.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web

from surogates.sandbox.base import (
    SandboxSpec,
    SandboxStatus,
    SandboxUnavailableError,
)
from surogates.sandbox.kubernetes import K8sSandbox, _PodEntry


@pytest.fixture()
def sandbox() -> K8sSandbox:
    """Create a K8sSandbox with mocked K8s API."""
    return K8sSandbox(
        namespace="test-ns",
        service_account="test-sa",
        pod_ready_timeout=5,
        executor_port=8071,
        storage_settings=MagicMock(endpoint="http://minio:9000", access_key="key", secret_key="secret", region=""),
        s3fs_image="s3fs:test",
    )


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


class TestProvisionCapturesEndpoint:
    """provision() stores the daemon endpoint (pod IP + token) on the entry."""

    async def test_pod_ip_and_token_stored(self, sandbox: K8sSandbox):
        api = MagicMock()
        api.create_namespaced_pod = AsyncMock()
        pod = MagicMock()
        pod.status.pod_ip = "10.42.0.99"
        api.read_namespaced_pod = AsyncMock(return_value=pod)

        with patch.object(sandbox, "_get_api", AsyncMock(return_value=api)), \
             patch.object(sandbox, "_create_s3_secret", AsyncMock()), \
             patch.object(sandbox, "_wait_for_ready", AsyncMock()):
            sandbox_id = await sandbox.provision(SandboxSpec())

        entry = sandbox._pods[sandbox_id]
        assert entry.pod_ip == "10.42.0.99"
        assert len(entry.token) >= 32

    async def test_missing_pod_ip_fails_provision(self, sandbox: K8sSandbox):
        from surogates.sandbox.base import SandboxUnavailableError

        api = MagicMock()
        api.create_namespaced_pod = AsyncMock()
        pod = MagicMock()
        pod.status.pod_ip = None
        api.read_namespaced_pod = AsyncMock(return_value=pod)
        api.delete_namespaced_pod = AsyncMock()

        with patch.object(sandbox, "_get_api", AsyncMock(return_value=api)), \
             patch.object(sandbox, "_create_s3_secret", AsyncMock()), \
             patch.object(sandbox, "_delete_secret_safe", AsyncMock()), \
             patch.object(sandbox, "_wait_for_ready", AsyncMock()):
            with pytest.raises(SandboxUnavailableError):
                await sandbox.provision(SandboxSpec())


def _entry_for(sandbox: K8sSandbox, *, port: int, timeout: int = 5) -> _PodEntry:
    entry = _PodEntry(
        sandbox_id="sb-test",
        pod_name="sandbox-test",
        secret_name="secret-test",
        namespace="test-ns",
        spec=SandboxSpec(timeout=timeout),
        pod_ip="127.0.0.1",
        token="tok-abc",
        status=SandboxStatus.RUNNING,
    )
    sandbox._pods["sb-test"] = entry
    sandbox._executor_port = port
    return entry


async def _serve(handler) -> tuple[web.AppRunner, int]:
    app = web.Application()
    app.router.add_post("/execute", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0, shutdown_timeout=0.5)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, port


class TestExecuteHttp:
    """execute() reaches the in-pod daemon over HTTP and classifies failures."""

    async def test_result_passthrough_and_auth_header(self, sandbox: K8sSandbox):
        seen = {}

        async def handler(request):
            seen["auth"] = request.headers.get("Authorization")
            seen["body"] = await request.json()
            return web.Response(text='{"ok": true}', content_type="application/json")

        runner, port = await _serve(handler)
        try:
            _entry_for(sandbox, port=port)
            result = await sandbox.execute("sb-test", "list_files", '{"pattern": "*"}')
            assert json.loads(result) == {"ok": True}
            assert seen["auth"] == "Bearer tok-abc"
            assert seen["body"] == {
                "name": "list_files",
                "args": {"pattern": "*"},
                "timeout": 5,
            }
        finally:
            await runner.cleanup()
            await sandbox.aclose()

    async def test_connect_refused_marks_failed_and_raises(self, sandbox: K8sSandbox):
        entry = _entry_for(sandbox, port=1)  # nothing listens on port 1
        with pytest.raises(SandboxUnavailableError):
            await sandbox.execute("sb-test", "list_files", "{}")
        assert entry.status == SandboxStatus.FAILED
        await sandbox.aclose()

    async def test_401_marks_failed_and_raises(self, sandbox: K8sSandbox):
        async def handler(request):
            return web.Response(status=401, text="unauthorized")

        runner, port = await _serve(handler)
        try:
            entry = _entry_for(sandbox, port=port)
            with pytest.raises(SandboxUnavailableError):
                await sandbox.execute("sb-test", "list_files", "{}")
            assert entry.status == SandboxStatus.FAILED
        finally:
            await runner.cleanup()
            await sandbox.aclose()

    async def test_client_timeout_returns_timed_out(self, sandbox: K8sSandbox):
        async def handler(request):
            await asyncio.sleep(30)
            return web.Response(text="{}")

        runner, port = await _serve(handler)
        try:
            entry = _entry_for(sandbox, port=port, timeout=1)
            # Client budget = spec.timeout + 5; shrink it so the test is fast.
            entry.spec.timeout = -4  # total budget = 1s
            result = json.loads(await sandbox.execute("sb-test", "list_files", "{}"))
            assert result["timed_out"] is True
            assert entry.status == SandboxStatus.RUNNING  # tool-level, not infra
        finally:
            await runner.cleanup()
            await sandbox.aclose()

    async def test_500_returns_error_result(self, sandbox: K8sSandbox):
        async def handler(request):
            return web.Response(status=500, text="kaboom")

        runner, port = await _serve(handler)
        try:
            entry = _entry_for(sandbox, port=port)
            result = json.loads(await sandbox.execute("sb-test", "list_files", "{}"))
            assert result["exit_code"] == -1
            assert "500" in result["stderr"]
            assert entry.status == SandboxStatus.RUNNING
        finally:
            await runner.cleanup()
            await sandbox.aclose()
