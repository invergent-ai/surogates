"""Kubernetes backend for the agent browser."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from kubernetes_asyncio import client, config

from surogates.browser.base import (
    BrowserEndpoint,
    BrowserSpec,
    BrowserStatus,
    BrowserUnavailableError,
)

logger = logging.getLogger(__name__)


@dataclass
class _PodEntry:
    browser_id: str
    pod_name: str
    service_name: str
    namespace: str
    spec: BrowserSpec
    endpoint: BrowserEndpoint
    status: BrowserStatus = BrowserStatus.PENDING


class K8sBrowserBackend:
    """Runs one browser pod and ClusterIP Service per browser session."""

    def __init__(
        self,
        *,
        namespace: str = "surogates",
        service_account: str = "surogates-browser",
        pod_ready_timeout: int = 60,
        image: str = "ghcr.io/onkernel/chromium-headful:stable",
    ) -> None:
        self._namespace = namespace
        self._service_account = service_account
        self._pod_ready_timeout = pod_ready_timeout
        self._image = image
        self._pods: dict[str, _PodEntry] = {}
        self._api: client.CoreV1Api | None = None

    async def _get_api(self) -> client.CoreV1Api:
        """Return a cached Kubernetes CoreV1Api client."""
        if self._api is None:
            try:
                config.load_incluster_config()
            except config.ConfigException:
                try:
                    await config.load_kube_config()
                except Exception as exc:
                    raise BrowserUnavailableError(
                        "Kubernetes browser backend unavailable: could not "
                        f"load kubeconfig: {exc}",
                    ) from exc
            self._api = client.CoreV1Api()
        return self._api
