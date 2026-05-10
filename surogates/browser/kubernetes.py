"""Kubernetes backend for the agent browser."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from kubernetes_asyncio import client, config

from surogates.browser.base import (
    BrowserEndpoint,
    BrowserSpec,
    BrowserStatus,
    BrowserUnavailableError,
)

logger = logging.getLogger(__name__)

SERVICE_PORT_REST = 10001
SERVICE_PORT_CDP = 9222
SERVICE_PORT_LIVE_VIEW = 443
TARGET_PORT_LIVE_VIEW_NOVNC = 6080


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

    def _build_pod_manifest(
        self,
        *,
        browser_id: str,
        pod_name: str,
        session_id: str,
        org_id: str,
        user_id: str,
        spec: BrowserSpec,
    ) -> client.V1Pod:
        """Build the browser pod manifest."""
        labels = {
            "app": "surogates-browser",
            "surogates.ai/browser-id": browser_id,
            "surogates.ai/session-id": session_id,
            "surogates.ai/org-id": org_id,
            "surogates.ai/user-id": user_id,
        }
        env_vars = [
            client.V1EnvVar(name=key, value=value)
            for key, value in sorted(spec.env.items())
        ]
        container = client.V1Container(
            name="browser",
            image=spec.image or self._image,
            image_pull_policy="IfNotPresent",
            ports=[
                client.V1ContainerPort(container_port=SERVICE_PORT_REST, name="rest"),
                client.V1ContainerPort(container_port=SERVICE_PORT_CDP, name="cdp"),
                client.V1ContainerPort(
                    container_port=TARGET_PORT_LIVE_VIEW_NOVNC,
                    name="novnc",
                ),
            ],
            resources=client.V1ResourceRequirements(
                requests={"cpu": spec.cpu, "memory": spec.memory},
                limits={"cpu": spec.cpu_limit, "memory": spec.memory_limit},
            ),
            env=env_vars,
        )

        return client.V1Pod(
            metadata=client.V1ObjectMeta(
                name=pod_name,
                namespace=self._namespace,
                labels=labels,
                annotations={
                    "surogates.ai/created-at": datetime.now(timezone.utc).isoformat(),
                },
            ),
            spec=client.V1PodSpec(
                service_account_name=self._service_account,
                active_deadline_seconds=spec.active_deadline_seconds,
                restart_policy="Never",
                containers=[container],
            ),
        )

    def _build_service_manifest(
        self,
        *,
        browser_id: str,
        service_name: str,
        session_id: str,
        org_id: str,
        user_id: str,
    ) -> client.V1Service:
        """Build the ClusterIP Service manifest for a browser pod."""
        labels = {
            "app": "surogates-browser",
            "surogates.ai/browser-id": browser_id,
            "surogates.ai/session-id": session_id,
            "surogates.ai/org-id": org_id,
            "surogates.ai/user-id": user_id,
        }
        return client.V1Service(
            metadata=client.V1ObjectMeta(
                name=service_name,
                namespace=self._namespace,
                labels=labels,
            ),
            spec=client.V1ServiceSpec(
                type="ClusterIP",
                selector={
                    "app": "surogates-browser",
                    "surogates.ai/browser-id": browser_id,
                },
                ports=[
                    client.V1ServicePort(
                        name="rest",
                        port=SERVICE_PORT_REST,
                        target_port=SERVICE_PORT_REST,
                    ),
                    client.V1ServicePort(
                        name="cdp",
                        port=SERVICE_PORT_CDP,
                        target_port=SERVICE_PORT_CDP,
                    ),
                    client.V1ServicePort(
                        name="live-view",
                        port=SERVICE_PORT_LIVE_VIEW,
                        target_port=TARGET_PORT_LIVE_VIEW_NOVNC,
                    ),
                ],
            ),
        )
