"""Tests for worker browser backend bootstrap selection."""

from __future__ import annotations

from surogates.config import BrowserSettings
from surogates.orchestrator.worker import _build_browser_backend


class TestBuildBrowserBackend:
    def test_process_backend_uses_process_settings(self) -> None:
        backend = _build_browser_backend(
            BrowserSettings(
                backend="process",
                image="browser:process",
                rest_port_base=40000,
                cdp_port_base=41000,
                live_view_port_base=42000,
            )
        )

        assert backend._image == "browser:process"
        assert backend._rest_port_base == 40000
        assert backend._cdp_port_base == 41000
        assert backend._live_view_port_base == 42000

    def test_kubernetes_backend_uses_k8s_settings(self) -> None:
        backend = _build_browser_backend(
            BrowserSettings(
                backend="kubernetes",
                image="browser:k8s",
                k8s_namespace="browser-ns",
                k8s_service_account="browser-sa",
                pod_ready_timeout=99,
            )
        )

        assert backend._image == "browser:k8s"
        assert backend._namespace == "browser-ns"
        assert backend._service_account == "browser-sa"
        assert backend._pod_ready_timeout == 99
