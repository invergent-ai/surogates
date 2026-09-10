"""Tests for surogates.browser.kubernetes.K8sBrowserBackend.

Uses mocks for kubernetes-asyncio so the suite runs without a cluster.
The real-cluster integration test lives at
``tests/integration/test_browser_e2e_k8s.py`` behind the ``browser_e2e_k8s``
marker.
"""

from __future__ import annotations

from types import SimpleNamespace
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
        storage_settings=type(
            "StorageSettings",
            (),
            {
                "access_key": "access",
                "secret_key": "secret",
                "endpoint": "https://s3.eu-west-1.amazonaws.com",
                "region": "",
            },
        )(),
        s3fs_image="s3fs:test",
        s3_endpoint="http://s3.svc:9000",
    )


class TestProvision:
    async def test_provision_creates_pod_and_service(
        self,
        backend: K8sBrowserBackend,
        monkeypatch,
    ) -> None:
        api = MagicMock()
        api.create_namespaced_secret = AsyncMock()
        api.create_namespaced_pod = AsyncMock()
        api.create_namespaced_service = AsyncMock()

        async def fake_get_api() -> MagicMock:
            return api

        async def fake_wait_ready(api_inner, pod_name: str) -> None:
            return None

        async def fake_wait_endpoint(rest_url: str) -> None:
            return None

        monkeypatch.setattr(backend, "_get_api", fake_get_api)
        monkeypatch.setattr(backend, "_wait_for_ready", fake_wait_ready)
        monkeypatch.setattr(backend, "_wait_for_endpoint", fake_wait_endpoint)

        spec = BrowserSpec(
            image="kernel-headful:test",
            workspace_source_ref="s3://agent-bucket/sessions/sess-1",
        )
        bid, endpoint = await backend.provision(
            spec,
            session_id="sess-1",
            org_id="org-1",
            user_id="user-1",
        )

        assert len(bid) == 32
        prefix = f"browser-{bid[:12]}.test-ns.svc.cluster.local."
        assert endpoint.rest_url == f"http://{prefix}:10001"
        assert endpoint.cdp_url == f"ws://{prefix}:9222"
        assert endpoint.live_view_url == f"ws://{prefix}:443"

        assert api.create_namespaced_secret.call_count == 1
        assert api.create_namespaced_pod.call_count == 1
        assert api.create_namespaced_service.call_count == 1
        assert backend._pods[bid].status == BrowserStatus.RUNNING

    async def test_provision_rolls_back_pod_on_service_failure(
        self,
        backend: K8sBrowserBackend,
        monkeypatch,
    ) -> None:
        api = MagicMock()
        api.create_namespaced_secret = AsyncMock()
        api.create_namespaced_pod = AsyncMock()
        api.create_namespaced_service = AsyncMock(
            side_effect=ApiException(status=500, reason="boom"),
        )
        api.delete_namespaced_pod = AsyncMock()
        api.delete_namespaced_secret = AsyncMock()

        async def fake_get_api() -> MagicMock:
            return api

        async def fake_wait_ready(api_inner, pod_name: str) -> None:
            return None

        monkeypatch.setattr(backend, "_get_api", fake_get_api)
        monkeypatch.setattr(backend, "_wait_for_ready", fake_wait_ready)

        with pytest.raises(BrowserUnavailableError):
            await backend.provision(
                BrowserSpec(workspace_source_ref="s3://agent-bucket/sessions/s"),
                session_id="s",
                org_id="o",
                user_id="u",
            )

        assert api.delete_namespaced_pod.call_count == 1
        assert api.delete_namespaced_secret.call_count == 1
        assert backend._pods == {}

    async def test_provision_rolls_back_when_pod_never_ready(
        self,
        backend: K8sBrowserBackend,
        monkeypatch,
    ) -> None:
        api = MagicMock()
        api.create_namespaced_secret = AsyncMock()
        api.create_namespaced_pod = AsyncMock()
        api.create_namespaced_service = AsyncMock()
        api.delete_namespaced_pod = AsyncMock()
        api.delete_namespaced_service = AsyncMock()
        api.delete_namespaced_secret = AsyncMock()

        async def fake_get_api() -> MagicMock:
            return api

        async def fake_wait_ready(api_inner, pod_name: str) -> None:
            raise RuntimeError("did not become ready")

        monkeypatch.setattr(backend, "_get_api", fake_get_api)
        monkeypatch.setattr(backend, "_wait_for_ready", fake_wait_ready)

        with pytest.raises(BrowserUnavailableError):
            await backend.provision(
                BrowserSpec(workspace_source_ref="s3://agent-bucket/sessions/s"),
                session_id="s",
                org_id="o",
                user_id="u",
            )

        assert api.delete_namespaced_service.call_count == 1
        assert api.delete_namespaced_pod.call_count == 1
        assert api.delete_namespaced_secret.call_count == 1
        assert backend._pods == {}

    async def test_provision_uses_custom_cluster_domain(self, monkeypatch) -> None:
        backend = K8sBrowserBackend(
            namespace="ns-1",
            cluster_domain="surogate.local",
        )

        api = MagicMock()
        api.create_namespaced_pod = AsyncMock()
        api.create_namespaced_service = AsyncMock()

        async def fake_get_api() -> MagicMock:
            return api

        async def fake_wait_ready(api_inner, pod_name: str) -> None:
            return None

        async def fake_wait_endpoint(rest_url: str) -> None:
            return None

        monkeypatch.setattr(backend, "_get_api", fake_get_api)
        monkeypatch.setattr(backend, "_wait_for_ready", fake_wait_ready)
        monkeypatch.setattr(backend, "_wait_for_endpoint", fake_wait_endpoint)

        _, endpoint = await backend.provision(
            BrowserSpec(),
            session_id="s",
            org_id="o",
            user_id="u",
        )

        assert ".ns-1.svc.surogate.local.:10001" in endpoint.rest_url
        assert endpoint.rest_url.endswith(":10001")

    async def test_provision_rolls_back_when_endpoint_unreachable(
        self,
        backend: K8sBrowserBackend,
        monkeypatch,
    ) -> None:
        api = MagicMock()
        api.create_namespaced_secret = AsyncMock()
        api.create_namespaced_pod = AsyncMock()
        api.create_namespaced_service = AsyncMock()
        api.delete_namespaced_pod = AsyncMock()
        api.delete_namespaced_service = AsyncMock()
        api.delete_namespaced_secret = AsyncMock()

        async def fake_get_api() -> MagicMock:
            return api

        async def fake_wait_ready(api_inner, pod_name: str) -> None:
            return None

        async def fake_wait_endpoint(rest_url: str) -> None:
            raise RuntimeError("connection timeout")

        monkeypatch.setattr(backend, "_get_api", fake_get_api)
        monkeypatch.setattr(backend, "_wait_for_ready", fake_wait_ready)
        monkeypatch.setattr(backend, "_wait_for_endpoint", fake_wait_endpoint)

        with pytest.raises(BrowserUnavailableError) as exc_info:
            await backend.provision(
                BrowserSpec(workspace_source_ref="s3://agent-bucket/sessions/s"),
                session_id="s",
                org_id="o",
                user_id="u",
            )

        assert exc_info.value.classification == "endpoint-propagation"
        assert api.delete_namespaced_service.call_count == 1
        assert api.delete_namespaced_pod.call_count == 1
        assert api.delete_namespaced_secret.call_count == 1
        assert backend._pods == {}


class TestProtocolAlignment:
    async def test_pool_forwards_session_to_k8s_provision(
        self,
        backend: K8sBrowserBackend,
        monkeypatch,
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

        async def fake_wait_endpoint(rest_url: str) -> None:
            return None

        monkeypatch.setattr(backend, "_get_api", fake_get_api)
        monkeypatch.setattr(backend, "_wait_for_ready", fake_wait_ready)
        monkeypatch.setattr(backend, "_wait_for_endpoint", fake_wait_endpoint)

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


class TestDestroy:
    async def test_destroy_deletes_service_and_pod(
        self,
        backend: K8sBrowserBackend,
        monkeypatch,
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
            secret_name=None,
            namespace="test-ns",
            spec=BrowserSpec(),
            endpoint=BrowserEndpoint(rest_url="r", cdp_url="c", live_view_url="l"),
        )

        await backend.destroy("bid")

        assert api.delete_namespaced_service.call_count == 1
        assert api.delete_namespaced_pod.call_count == 1
        assert "bid" not in backend._pods


    async def test_destroy_for_session_deletes_labeled_resources(
        self,
        backend: K8sBrowserBackend,
        monkeypatch,
    ) -> None:
        pod = SimpleNamespace(
            metadata=SimpleNamespace(
                name="browser-abcdef123456",
                labels={
                    "app": "surogates-browser",
                    "surogates.ai/browser-id": "abcdef1234567890",
                    "surogates.ai/session-id": "sess-x",
                },
            ),
        )
        service = SimpleNamespace(
            metadata=SimpleNamespace(name="browser-abcdef123456"),
        )
        api = MagicMock()
        api.list_namespaced_pod = AsyncMock(return_value=MagicMock(items=[pod]))
        api.list_namespaced_service = AsyncMock(return_value=MagicMock(items=[service]))
        api.delete_namespaced_pod = AsyncMock()
        api.delete_namespaced_service = AsyncMock()
        api.delete_namespaced_secret = AsyncMock()

        async def fake_get_api() -> MagicMock:
            return api

        monkeypatch.setattr(backend, "_get_api", fake_get_api)

        await backend.destroy_for_session("sess-x")

        selector = "app=surogates-browser,surogates.ai/session-id=sess-x"
        api.list_namespaced_pod.assert_awaited_once_with(
            "test-ns",
            label_selector=selector,
        )
        api.list_namespaced_service.assert_awaited_once_with(
            "test-ns",
            label_selector=selector,
        )
        api.delete_namespaced_service.assert_awaited_once_with(
            "browser-abcdef123456",
            "test-ns",
        )
        api.delete_namespaced_pod.assert_awaited_once_with(
            "browser-abcdef123456",
            "test-ns",
            grace_period_seconds=0,
        )
        api.delete_namespaced_secret.assert_awaited_once_with(
            "browser-s3-abcdef123456",
            "test-ns",
        )
