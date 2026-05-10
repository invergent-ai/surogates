"""Kubernetes backend for the agent browser."""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from kubernetes_asyncio import client, config, watch
from kubernetes_asyncio.client import ApiException

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

    async def provision(
        self,
        spec: BrowserSpec,
        *,
        session_id: str,
        org_id: str,
        user_id: str,
    ) -> tuple[str, BrowserEndpoint]:
        """Create a browser pod and Service, then wait for readiness."""
        api = await self._get_api()
        browser_id = uuid.uuid4().hex
        suffix = browser_id[:12]
        pod_name = f"browser-{suffix}"
        service_name = f"browser-{suffix}"
        endpoint = BrowserEndpoint(
            rest_url=f"http://{service_name}.{self._namespace}.svc:{SERVICE_PORT_REST}",
            cdp_url=f"ws://{service_name}.{self._namespace}.svc:{SERVICE_PORT_CDP}",
            live_view_url=(
                f"ws://{service_name}.{self._namespace}.svc:{SERVICE_PORT_LIVE_VIEW}"
            ),
        )

        pod_manifest = self._build_pod_manifest(
            browser_id=browser_id,
            pod_name=pod_name,
            session_id=session_id,
            org_id=org_id,
            user_id=user_id,
            spec=spec,
        )
        try:
            await api.create_namespaced_pod(self._namespace, pod_manifest)
        except ApiException as exc:
            raise BrowserUnavailableError(
                f"Failed to create browser pod {pod_name}: {exc}",
            ) from exc

        service_manifest = self._build_service_manifest(
            browser_id=browser_id,
            service_name=service_name,
            session_id=session_id,
            org_id=org_id,
            user_id=user_id,
        )
        try:
            await api.create_namespaced_service(self._namespace, service_manifest)
        except ApiException as exc:
            await self._delete_pod_safe(api, pod_name)
            raise BrowserUnavailableError(
                f"Failed to create browser service {service_name}: {exc}",
            ) from exc

        try:
            await self._wait_for_ready(api, pod_name)
        except Exception as exc:
            await self._delete_service_safe(api, service_name)
            await self._delete_pod_safe(api, pod_name)
            raise BrowserUnavailableError(
                f"Browser pod {pod_name} did not become ready: {exc}",
                classification="readiness",
            ) from exc

        self._pods[browser_id] = _PodEntry(
            browser_id=browser_id,
            pod_name=pod_name,
            service_name=service_name,
            namespace=self._namespace,
            spec=spec,
            endpoint=endpoint,
            status=BrowserStatus.RUNNING,
        )
        logger.info(
            "Provisioned K8s browser %s for session %s (pod %s, service %s)",
            browser_id,
            session_id,
            pod_name,
            service_name,
        )
        return browser_id, endpoint

    async def status(self, browser_id: str) -> BrowserStatus:
        """Read the pod phase and map it to a browser lifecycle status."""
        entry = self._pods.get(browser_id)
        if entry is None:
            return BrowserStatus.TERMINATED

        api = await self._get_api()
        try:
            pod = await api.read_namespaced_pod(entry.pod_name, self._namespace)
        except ApiException as exc:
            if exc.status == 404:
                self._pods.pop(browser_id, None)
                return BrowserStatus.TERMINATED
            logger.warning(
                "Status check for browser %s failed (HTTP %s); trusting cached %s",
                browser_id,
                exc.status,
                entry.status,
            )
            return entry.status

        entry.status = self._map_pod_status(pod)
        return entry.status

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

    async def _wait_for_ready(self, api: client.CoreV1Api, pod_name: str) -> None:
        """Watch the pod until it has a Ready condition or timeout."""
        pod_watch = watch.Watch()
        try:
            async with asyncio.timeout(self._pod_ready_timeout):
                async for event in pod_watch.stream(
                    api.list_namespaced_pod,
                    namespace=self._namespace,
                    field_selector=f"metadata.name={pod_name}",
                    timeout_seconds=self._pod_ready_timeout,
                ):
                    pod = event["object"]
                    if self._is_pod_ready(pod):
                        return
                    phase = pod.status.phase if pod.status else "Unknown"
                    if phase in {"Failed", "Succeeded"}:
                        raise RuntimeError(
                            f"Browser pod {pod_name} entered {phase} phase",
                        )
        except TimeoutError:
            raise RuntimeError(
                f"Browser pod {pod_name} did not become ready within "
                f"{self._pod_ready_timeout}s",
            )
        finally:
            pod_watch.stop()

    async def _delete_pod_safe(self, api: client.CoreV1Api, pod_name: str) -> None:
        try:
            await api.delete_namespaced_pod(
                pod_name,
                self._namespace,
                grace_period_seconds=5,
            )
        except ApiException as exc:
            if exc.status != 404:
                logger.warning("Failed to delete browser pod %s: %s", pod_name, exc)

    async def _delete_service_safe(
        self,
        api: client.CoreV1Api,
        service_name: str,
    ) -> None:
        try:
            await api.delete_namespaced_service(service_name, self._namespace)
        except ApiException as exc:
            if exc.status != 404:
                logger.warning(
                    "Failed to delete browser service %s: %s",
                    service_name,
                    exc,
                )

    @staticmethod
    def _is_pod_ready(pod: client.V1Pod) -> bool:
        if not pod.status or not pod.status.conditions:
            return False
        return any(
            condition.type == "Ready" and condition.status == "True"
            for condition in pod.status.conditions
        )

    @staticmethod
    def _map_pod_status(pod: client.V1Pod) -> BrowserStatus:
        if not pod.status:
            return BrowserStatus.PENDING
        phase = pod.status.phase
        if phase == "Running" and K8sBrowserBackend._is_pod_ready(pod):
            return BrowserStatus.RUNNING
        if phase == "Pending":
            return BrowserStatus.PENDING
        if phase in {"Failed", "Unknown"}:
            return BrowserStatus.FAILED
        if phase == "Succeeded":
            return BrowserStatus.TERMINATED
        return BrowserStatus.PENDING

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
