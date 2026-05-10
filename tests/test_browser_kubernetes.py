"""Tests for surogates.browser.kubernetes.K8sBrowserBackend.

Uses mocks for kubernetes-asyncio so the suite runs without a cluster.
The real-cluster integration test lives at
``tests/integration/test_browser_e2e_k8s.py`` behind the ``browser_e2e_k8s``
marker.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from kubernetes_asyncio.client import ApiException

from surogates.browser.base import (
    BrowserEndpoint,
    BrowserSpec,
    BrowserStatus,
    BrowserUnavailableError,
)
from surogates.browser.kubernetes import K8sBrowserBackend


@pytest.fixture()
def backend() -> K8sBrowserBackend:
    return K8sBrowserBackend(
        namespace="test-ns",
        service_account="test-browser-sa",
        pod_ready_timeout=5,
        image="kernel-headful:test",
    )


class TestSkeleton:
    def test_construct(self, backend: K8sBrowserBackend) -> None:
        assert backend._namespace == "test-ns"
        assert backend._service_account == "test-browser-sa"
        assert backend._pod_ready_timeout == 5
        assert backend._image == "kernel-headful:test"
        assert backend._pods == {}

    async def test_get_api_caches(self, backend: K8sBrowserBackend, monkeypatch) -> None:
        from kubernetes_asyncio import client as k8s_client, config as k8s_config

        monkeypatch.setattr(k8s_config, "load_incluster_config", lambda: None)
        api = await backend._get_api()
        api2 = await backend._get_api()
        assert api is api2
        assert isinstance(api, k8s_client.CoreV1Api)


class TestBuildPodManifest:
    def test_pod_manifest_has_identity_labels_and_container_spec(
        self, backend: K8sBrowserBackend,
    ) -> None:
        spec = BrowserSpec(
            image="kernel-headful:test",
            cpu="500m",
            memory="1Gi",
            cpu_limit="1",
            memory_limit="2Gi",
            active_deadline_seconds=1800,
            env={"EXTRA": "value"},
        )
        pod = backend._build_pod_manifest(
            browser_id="browser-id",
            pod_name="browser-abc123",
            session_id="session-1",
            org_id="org-1",
            user_id="user-1",
            spec=spec,
        )

        assert pod.metadata.name == "browser-abc123"
        assert pod.metadata.namespace == "test-ns"
        assert pod.metadata.labels == {
            "app": "surogates-browser",
            "surogates.ai/browser-id": "browser-id",
            "surogates.ai/session-id": "session-1",
            "surogates.ai/org-id": "org-1",
            "surogates.ai/user-id": "user-1",
        }
        assert "surogates.ai/created-at" in pod.metadata.annotations

        assert pod.spec.service_account_name == "test-browser-sa"
        assert pod.spec.restart_policy == "Never"
        assert pod.spec.active_deadline_seconds == 1800

        c = pod.spec.containers[0]
        assert c.name == "browser"
        assert c.image == "kernel-headful:test"
        assert c.image_pull_policy == "IfNotPresent"
        assert c.resources.requests == {"cpu": "500m", "memory": "1Gi"}
        assert c.resources.limits == {"cpu": "1", "memory": "2Gi"}
        assert [(p.name, p.container_port) for p in c.ports] == [
            ("rest", 10001),
            ("cdp", 9222),
            ("novnc", 6080),
        ]
        assert c.readiness_probe is not None
        assert c.readiness_probe.http_get.path == "/spec.json"
        assert c.readiness_probe.http_get.port == 10001
        assert c.readiness_probe.period_seconds == 2
        assert c.readiness_probe.failure_threshold == 30
        assert {e.name: e.value for e in c.env}["EXTRA"] == "value"

    def test_pod_manifest_uses_backend_image_when_spec_image_blank(
        self, backend: K8sBrowserBackend,
    ) -> None:
        spec = BrowserSpec(image="")
        pod = backend._build_pod_manifest(
            browser_id="browser-id",
            pod_name="browser-abc123",
            session_id="session-1",
            org_id="org-1",
            user_id="user-1",
            spec=spec,
        )

        assert pod.spec.containers[0].image == "kernel-headful:test"


class TestBuildServiceManifest:
    def test_service_manifest_targets_browser_pod_labels(
        self, backend: K8sBrowserBackend,
    ) -> None:
        svc = backend._build_service_manifest(
            browser_id="browser-id",
            service_name="browser-abc123",
            session_id="session-1",
            org_id="org-1",
            user_id="user-1",
        )

        assert svc.metadata.name == "browser-abc123"
        assert svc.metadata.namespace == "test-ns"
        assert svc.metadata.labels == {
            "app": "surogates-browser",
            "surogates.ai/browser-id": "browser-id",
            "surogates.ai/session-id": "session-1",
            "surogates.ai/org-id": "org-1",
            "surogates.ai/user-id": "user-1",
        }
        assert svc.spec.type == "ClusterIP"
        assert svc.spec.selector == {
            "app": "surogates-browser",
            "surogates.ai/browser-id": "browser-id",
        }
        assert [(p.name, p.port, p.target_port) for p in svc.spec.ports] == [
            ("rest", 10001, 10001),
            ("cdp", 9222, 9222),
            ("live-view", 443, 6080),
        ]


class TestProvision:
    async def test_provision_creates_pod_and_service(
        self, backend: K8sBrowserBackend, monkeypatch,
    ) -> None:
        api = MagicMock()
        api.create_namespaced_pod = AsyncMock()
        api.create_namespaced_service = AsyncMock()

        async def fake_get_api() -> MagicMock:
            return api

        async def fake_wait_ready(api_inner, pod_name: str) -> None:
            return None

        monkeypatch.setattr(backend, "_get_api", fake_get_api)
        monkeypatch.setattr(backend, "_wait_for_ready", fake_wait_ready)

        spec = BrowserSpec(image="kernel-headful:test")
        bid, endpoint = await backend.provision(
            spec,
            session_id="sess-1",
            org_id="org-1",
            user_id="user-1",
        )

        assert len(bid) == 32
        prefix = f"browser-{bid[:12]}.test-ns.svc"
        assert endpoint.rest_url == f"http://{prefix}:10001"
        assert endpoint.cdp_url == f"ws://{prefix}:9222"
        assert endpoint.live_view_url == f"ws://{prefix}:443"

        assert api.create_namespaced_pod.call_count == 1
        assert api.create_namespaced_service.call_count == 1
        assert backend._pods[bid].status == BrowserStatus.RUNNING

    async def test_provision_rolls_back_pod_on_service_failure(
        self, backend: K8sBrowserBackend, monkeypatch,
    ) -> None:
        api = MagicMock()
        api.create_namespaced_pod = AsyncMock()
        api.create_namespaced_service = AsyncMock(
            side_effect=ApiException(status=500, reason="boom"),
        )
        api.delete_namespaced_pod = AsyncMock()

        async def fake_get_api() -> MagicMock:
            return api

        async def fake_wait_ready(api_inner, pod_name: str) -> None:
            return None

        monkeypatch.setattr(backend, "_get_api", fake_get_api)
        monkeypatch.setattr(backend, "_wait_for_ready", fake_wait_ready)

        with pytest.raises(BrowserUnavailableError):
            await backend.provision(
                BrowserSpec(),
                session_id="s",
                org_id="o",
                user_id="u",
            )

        assert api.delete_namespaced_pod.call_count == 1
        assert backend._pods == {}

    async def test_provision_rolls_back_when_pod_never_ready(
        self, backend: K8sBrowserBackend, monkeypatch,
    ) -> None:
        api = MagicMock()
        api.create_namespaced_pod = AsyncMock()
        api.create_namespaced_service = AsyncMock()
        api.delete_namespaced_pod = AsyncMock()
        api.delete_namespaced_service = AsyncMock()

        async def fake_get_api() -> MagicMock:
            return api

        async def fake_wait_ready(api_inner, pod_name: str) -> None:
            raise RuntimeError("did not become ready")

        monkeypatch.setattr(backend, "_get_api", fake_get_api)
        monkeypatch.setattr(backend, "_wait_for_ready", fake_wait_ready)

        with pytest.raises(BrowserUnavailableError):
            await backend.provision(
                BrowserSpec(),
                session_id="s",
                org_id="o",
                user_id="u",
            )

        assert api.delete_namespaced_service.call_count == 1
        assert api.delete_namespaced_pod.call_count == 1
        assert backend._pods == {}


class TestProtocolAlignment:
    async def test_pool_forwards_session_to_k8s_provision(
        self, backend: K8sBrowserBackend, monkeypatch,
    ) -> None:
        from surogates.browser.pool import BrowserPool
        from surogates.browser.registry import BrowserEntry

        api = MagicMock()
        api.create_namespaced_pod = AsyncMock()
        api.create_namespaced_service = AsyncMock()

        async def fake_get_api() -> MagicMock:
            return api

        async def fake_wait_ready(api_inner, pod_name: str) -> None:
            return None

        monkeypatch.setattr(backend, "_get_api", fake_get_api)
        monkeypatch.setattr(backend, "_wait_for_ready", fake_wait_ready)

        class FakeRegistry:
            def __init__(self) -> None:
                self.entries: dict[str, BrowserEntry] = {}

            async def set(self, entry: BrowserEntry) -> None:
                self.entries[entry.session_id] = entry

            async def get(self, session_id: str) -> BrowserEntry | None:
                return self.entries.get(session_id)

            async def delete(self, session_id: str) -> None:
                self.entries.pop(session_id, None)

        pool = BrowserPool(backend=backend, registry=FakeRegistry())  # type: ignore[arg-type]
        await pool.ensure(
            session_id="sess-7",
            org_id="org-7",
            user_id="user-7",
            spec=BrowserSpec(),
        )

        pod_arg = api.create_namespaced_pod.call_args.args[1]
        assert pod_arg.metadata.labels["surogates.ai/session-id"] == "sess-7"
        assert pod_arg.metadata.labels["surogates.ai/org-id"] == "org-7"
        assert pod_arg.metadata.labels["surogates.ai/user-id"] == "user-7"


class TestStatus:
    async def test_status_running(self, backend: K8sBrowserBackend, monkeypatch) -> None:
        backend._pods["bid"] = MagicMock(
            pod_name="browser-bid",
            namespace="test-ns",
            status=BrowserStatus.PENDING,
        )
        running_pod = MagicMock()
        running_pod.status.phase = "Running"
        running_pod.status.conditions = [MagicMock(type="Ready", status="True")]
        api = MagicMock()
        api.read_namespaced_pod = AsyncMock(return_value=running_pod)

        async def fake_get_api() -> MagicMock:
            return api

        monkeypatch.setattr(backend, "_get_api", fake_get_api)
        assert await backend.status("bid") == BrowserStatus.RUNNING
        assert backend._pods["bid"].status == BrowserStatus.RUNNING

    async def test_status_pending_when_phase_pending(
        self, backend: K8sBrowserBackend, monkeypatch,
    ) -> None:
        backend._pods["bid"] = MagicMock(
            pod_name="browser-bid",
            namespace="test-ns",
            status=BrowserStatus.PENDING,
        )
        pending_pod = MagicMock()
        pending_pod.status.phase = "Pending"
        pending_pod.status.conditions = []
        api = MagicMock()
        api.read_namespaced_pod = AsyncMock(return_value=pending_pod)

        async def fake_get_api() -> MagicMock:
            return api

        monkeypatch.setattr(backend, "_get_api", fake_get_api)
        assert await backend.status("bid") == BrowserStatus.PENDING

    async def test_status_terminated_when_pod_404(
        self, backend: K8sBrowserBackend, monkeypatch,
    ) -> None:
        backend._pods["bid"] = MagicMock(
            pod_name="browser-bid",
            namespace="test-ns",
            status=BrowserStatus.RUNNING,
        )
        api = MagicMock()
        api.read_namespaced_pod = AsyncMock(side_effect=ApiException(status=404))

        async def fake_get_api() -> MagicMock:
            return api

        monkeypatch.setattr(backend, "_get_api", fake_get_api)
        assert await backend.status("bid") == BrowserStatus.TERMINATED
        assert "bid" not in backend._pods

    async def test_status_unknown_returns_terminated(
        self, backend: K8sBrowserBackend,
    ) -> None:
        assert await backend.status("never") == BrowserStatus.TERMINATED


class TestDestroy:
    async def test_destroy_deletes_service_and_pod(
        self, backend: K8sBrowserBackend, monkeypatch,
    ) -> None:
        from surogates.browser.kubernetes import _PodEntry

        api = MagicMock()
        api.delete_namespaced_pod = AsyncMock()
        api.delete_namespaced_service = AsyncMock()

        async def fake_get_api() -> MagicMock:
            return api

        monkeypatch.setattr(backend, "_get_api", fake_get_api)
        backend._pods["bid"] = _PodEntry(
            browser_id="bid",
            pod_name="browser-bid",
            service_name="browser-bid",
            namespace="test-ns",
            spec=BrowserSpec(),
            endpoint=BrowserEndpoint(rest_url="r", cdp_url="c", live_view_url="l"),
        )

        await backend.destroy("bid")

        assert api.delete_namespaced_service.call_count == 1
        assert api.delete_namespaced_pod.call_count == 1
        assert "bid" not in backend._pods

    async def test_destroy_unknown_is_noop(self, backend: K8sBrowserBackend) -> None:
        await backend.destroy("never")

    async def test_destroy_swallows_404(
        self, backend: K8sBrowserBackend, monkeypatch,
    ) -> None:
        from surogates.browser.kubernetes import _PodEntry

        api = MagicMock()
        api.delete_namespaced_pod = AsyncMock(side_effect=ApiException(status=404))
        api.delete_namespaced_service = AsyncMock(side_effect=ApiException(status=404))

        async def fake_get_api() -> MagicMock:
            return api

        monkeypatch.setattr(backend, "_get_api", fake_get_api)
        backend._pods["bid"] = _PodEntry(
            browser_id="bid",
            pod_name="browser-bid",
            service_name="browser-bid",
            namespace="test-ns",
            spec=BrowserSpec(),
            endpoint=BrowserEndpoint(rest_url="r", cdp_url="c", live_view_url="l"),
        )

        await backend.destroy("bid")

        assert "bid" not in backend._pods


class TestFindBySession:
    async def test_find_entry_returns_tenant_metadata(
        self, backend: K8sBrowserBackend, monkeypatch,
    ) -> None:
        pod = MagicMock()
        pod.metadata.name = "browser-abcdef123456"
        pod.metadata.labels = {
            "app": "surogates-browser",
            "surogates.ai/browser-id": "abcdef1234567890",
            "surogates.ai/session-id": "sess-x",
            "surogates.ai/org-id": "org-x",
            "surogates.ai/user-id": "user-x",
        }
        api = MagicMock()
        api.list_namespaced_pod = AsyncMock(return_value=MagicMock(items=[pod]))

        async def fake_get_api() -> MagicMock:
            return api

        monkeypatch.setattr(backend, "_get_api", fake_get_api)
        entry = await backend.find_entry_by_session("sess-x")

        assert entry is not None
        assert entry.session_id == "sess-x"
        assert entry.org_id == "org-x"
        assert entry.user_id == "user-x"
        assert entry.rest_url == "http://browser-abcdef123456.test-ns.svc:10001"
        assert entry.cdp_url == "ws://browser-abcdef123456.test-ns.svc:9222"
        assert entry.live_view_url == "ws://browser-abcdef123456.test-ns.svc:443"

    async def test_find_returns_endpoint(
        self, backend: K8sBrowserBackend, monkeypatch,
    ) -> None:
        pod = MagicMock()
        pod.metadata.name = "browser-abcdef123456"
        pod.metadata.labels = {
            "app": "surogates-browser",
            "surogates.ai/browser-id": "abcdef1234567890",
            "surogates.ai/session-id": "sess-x",
            "surogates.ai/org-id": "org-x",
            "surogates.ai/user-id": "user-x",
        }
        api = MagicMock()
        api.list_namespaced_pod = AsyncMock(return_value=MagicMock(items=[pod]))

        async def fake_get_api() -> MagicMock:
            return api

        monkeypatch.setattr(backend, "_get_api", fake_get_api)
        result = await backend.find_by_session("sess-x")

        assert result is not None
        bid, endpoint = result
        assert bid == "abcdef1234567890"
        assert endpoint.rest_url == "http://browser-abcdef123456.test-ns.svc:10001"
        assert endpoint.cdp_url == "ws://browser-abcdef123456.test-ns.svc:9222"
        assert endpoint.live_view_url == "ws://browser-abcdef123456.test-ns.svc:443"

    async def test_find_returns_none_when_no_match(
        self, backend: K8sBrowserBackend, monkeypatch,
    ) -> None:
        api = MagicMock()
        api.list_namespaced_pod = AsyncMock(return_value=MagicMock(items=[]))

        async def fake_get_api() -> MagicMock:
            return api

        monkeypatch.setattr(backend, "_get_api", fake_get_api)
        assert await backend.find_by_session("sess-missing") is None

    async def test_find_uses_correct_label_selector(
        self, backend: K8sBrowserBackend, monkeypatch,
    ) -> None:
        api = MagicMock()
        api.list_namespaced_pod = AsyncMock(return_value=MagicMock(items=[]))

        async def fake_get_api() -> MagicMock:
            return api

        monkeypatch.setattr(backend, "_get_api", fake_get_api)
        await backend.find_by_session("sess-y")

        kwargs = api.list_namespaced_pod.call_args.kwargs
        selector = kwargs.get("label_selector", "")
        assert "app=surogates-browser" in selector
        assert "surogates.ai/session-id=sess-y" in selector
