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
