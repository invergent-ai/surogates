"""Kubernetes backend for the agent browser."""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import httpx
from kubernetes_asyncio import client, config, watch
from kubernetes_asyncio.client import ApiException

from surogates.browser.base import (
    BrowserEndpoint,
    BrowserSpec,
    BrowserStatus,
    BrowserUnavailableError,
)
from surogates.browser.registry import BrowserEntry

logger = logging.getLogger(__name__)

SERVICE_PORT_REST = 10001
SERVICE_PORT_CDP = 9222
SERVICE_PORT_LIVE_VIEW = 443
TARGET_PORT_LIVE_VIEW = 8080


def _image_pull_policy(image: str) -> str:
    if "@" in image:
        return "IfNotPresent"
    image_name = image.rsplit("/", 1)[-1]
    if ":" not in image_name or image_name.endswith(":latest"):
        return "Always"
    return "IfNotPresent"


@dataclass
class _PodEntry:
    browser_id: str
    pod_name: str
    service_name: str
    secret_name: str | None
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
        cluster_domain: str = "cluster.local",
        pod_ready_timeout: int = 60,
        endpoint_probe_timeout: int = 30,
        image: str = "ghcr.io/invergent-ai/surogates-agent-browser:latest",
        storage_settings: Any = None,
        s3fs_image: str = "ghcr.io/invergent-ai/surogates-s3fs:latest",
        s3_endpoint: str = "",
    ) -> None:
        self._namespace = namespace
        self._service_account = service_account
        self._cluster_domain = cluster_domain.strip(".") or "cluster.local"
        self._pod_ready_timeout = pod_ready_timeout
        self._endpoint_probe_timeout = endpoint_probe_timeout
        self._image = image
        self._storage = storage_settings
        self._s3fs_image = s3fs_image
        self._s3_endpoint = s3_endpoint
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
        secret_name = f"browser-s3-{suffix}" if spec.workspace_source_ref else None
        fqdn = f"{service_name}.{self._namespace}.svc.{self._cluster_domain}."
        endpoint = BrowserEndpoint(
            rest_url=f"http://{fqdn}:{SERVICE_PORT_REST}",
            cdp_url=f"ws://{fqdn}:{SERVICE_PORT_CDP}",
            live_view_url=f"ws://{fqdn}:{SERVICE_PORT_LIVE_VIEW}",
        )

        if secret_name is not None:
            await self._create_s3_secret(api, secret_name)

        pod_manifest = self._build_pod_manifest(
            browser_id=browser_id,
            pod_name=pod_name,
            session_id=session_id,
            org_id=org_id,
            user_id=user_id,
            secret_name=secret_name,
            spec=spec,
        )
        try:
            await api.create_namespaced_pod(self._namespace, pod_manifest)
        except ApiException as exc:
            if secret_name is not None:
                await self._delete_secret_safe(api, secret_name)
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
            if secret_name is not None:
                await self._delete_secret_safe(api, secret_name)
            raise BrowserUnavailableError(
                f"Failed to create browser service {service_name}: {exc}",
            ) from exc

        try:
            await self._wait_for_ready(api, pod_name)
        except Exception as exc:
            await self._delete_service_safe(api, service_name)
            await self._delete_pod_safe(api, pod_name)
            if secret_name is not None:
                await self._delete_secret_safe(api, secret_name)
            raise BrowserUnavailableError(
                f"Browser pod {pod_name} did not become ready: {exc}",
                classification="readiness",
            ) from exc

        try:
            await self._wait_for_endpoint(endpoint.rest_url)
        except Exception as exc:
            await self._delete_service_safe(api, service_name)
            await self._delete_pod_safe(api, pod_name)
            if secret_name is not None:
                await self._delete_secret_safe(api, secret_name)
            raise BrowserUnavailableError(
                f"Browser endpoint {endpoint.rest_url} not reachable: {exc}",
                classification="endpoint-propagation",
            ) from exc

        self._pods[browser_id] = _PodEntry(
            browser_id=browser_id,
            pod_name=pod_name,
            service_name=service_name,
            secret_name=secret_name,
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

    async def destroy(self, browser_id: str) -> None:
        """Delete a browser Service and pod if this worker knows about them."""
        entry = self._pods.pop(browser_id, None)
        if entry is None:
            return

        api = await self._get_api()
        await self._delete_service_safe(api, entry.service_name)
        await self._delete_pod_safe(api, entry.pod_name)
        if entry.secret_name is not None:
            await self._delete_secret_safe(api, entry.secret_name)
        logger.info(
            "Destroyed K8s browser %s (pod %s, service %s)",
            browser_id,
            entry.pod_name,
            entry.service_name,
        )

    async def destroy_for_session(self, session_id: str) -> None:
        """Delete browser resources with the session label, even if uncached."""
        api = await self._get_api()
        selector = f"app=surogates-browser,surogates.ai/session-id={session_id}"
        pod_result = await api.list_namespaced_pod(
            self._namespace,
            label_selector=selector,
        )
        service_result = await api.list_namespaced_service(
            self._namespace,
            label_selector=selector,
        )

        services = list(getattr(service_result, "items", []) or [])
        pods = list(getattr(pod_result, "items", []) or [])
        for service in services:
            name = getattr(service.metadata, "name", None)
            if name:
                await self._delete_service_safe(api, name)

        for pod in pods:
            name = getattr(pod.metadata, "name", None)
            labels = getattr(pod.metadata, "labels", None) or {}
            browser_id = labels.get("surogates.ai/browser-id")
            if browser_id:
                self._pods.pop(browser_id, None)
                await self._delete_secret_safe(api, f"browser-s3-{browser_id[:12]}")
            if name:
                await self._delete_pod_safe(api, name)

        if pods or services:
            logger.info(
                "Destroyed K8s browser resources for session %s "
                "(pods=%d, services=%d)",
                session_id,
                len(pods),
                len(services),
            )

    async def find_by_session(
        self,
        session_id: str,
    ) -> tuple[str, BrowserEndpoint] | None:
        """Resolve a browser endpoint by session label."""
        found = await self._find_pod_by_session(session_id)
        if found is None:
            return None
        browser_id, _org_id, _user_id, service_name = found
        if not browser_id or not service_name:
            return None
        return (
            browser_id,
            BrowserEndpoint(
                rest_url=f"http://{service_name}.{self._namespace}.svc:{SERVICE_PORT_REST}",
                cdp_url=f"ws://{service_name}.{self._namespace}.svc:{SERVICE_PORT_CDP}",
                live_view_url=(
                    f"ws://{service_name}.{self._namespace}.svc:{SERVICE_PORT_LIVE_VIEW}"
                ),
            ),
        )

    async def find_entry_by_session(self, session_id: str) -> BrowserEntry | None:
        """Resolve browser metadata by session label.

        This variant preserves org/user labels for API-side tenant checks.
        """
        found = await self._find_pod_by_session(session_id)
        if found is None:
            return None
        browser_id, org_id, user_id, service_name = found
        if not browser_id or not org_id or not user_id or not service_name:
            return None

        return BrowserEntry(
            session_id=session_id,
            org_id=org_id,
            user_id=user_id,
            rest_url=f"http://{service_name}.{self._namespace}.svc:{SERVICE_PORT_REST}",
            cdp_url=f"ws://{service_name}.{self._namespace}.svc:{SERVICE_PORT_CDP}",
            live_view_url=(
                f"ws://{service_name}.{self._namespace}.svc:{SERVICE_PORT_LIVE_VIEW}"
            ),
            provisioned_at=datetime.now(timezone.utc),
        )

    async def _find_pod_by_session(
        self,
        session_id: str,
    ) -> tuple[str | None, str | None, str | None, str | None] | None:
        api = await self._get_api()
        selector = f"app=surogates-browser,surogates.ai/session-id={session_id}"
        result = await api.list_namespaced_pod(
            self._namespace,
            label_selector=selector,
        )
        items = list(getattr(result, "items", []) or [])
        if not items:
            return None
        pod = items[0]
        labels = pod.metadata.labels or {}
        return (
            labels.get("surogates.ai/browser-id"),
            labels.get("surogates.ai/org-id"),
            labels.get("surogates.ai/user-id"),
            pod.metadata.name,
        )

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
        secret_name: str | None = None,
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
        runtime_volume_mounts = [
            client.V1VolumeMount(
                name="browser-runtime",
                mount_path="/var/log/supervisord",
                sub_path="supervisord",
            ),
            client.V1VolumeMount(
                name="browser-runtime",
                mount_path="/etc/envoy/certs",
                sub_path="envoy-certs",
            ),
        ]
        volume_mounts: list[client.V1VolumeMount] = [*runtime_volume_mounts]
        volumes: list[client.V1Volume] = [
            client.V1Volume(
                name="browser-runtime",
                empty_dir=client.V1EmptyDirVolumeSource(),
            ),
        ]
        containers: list[client.V1Container]
        if spec.workspace_source_ref:
            volume_mounts.extend([
                client.V1VolumeMount(
                    name="workspace",
                    mount_path="/workspace",
                    mount_propagation="HostToContainer",
                ),
            ])
            env_vars = [
                client.V1EnvVar(name="WORKSPACE_DIR", value="/workspace"),
                *[env for env in env_vars if env.name not in {"HOME", "WORKSPACE_DIR"}],
            ]
            volumes.append(
                client.V1Volume(
                    name="workspace",
                    empty_dir=client.V1EmptyDirVolumeSource(),
                )
            )
        init_container = client.V1Container(
            name="browser-runtime-init",
            image=spec.image or self._image,
            image_pull_policy=_image_pull_policy(spec.image or self._image),
            command=[
                "/bin/sh",
                "-c",
                (
                    "mkdir -p "
                    "/browser-runtime/supervisord "
                    "/browser-runtime/envoy-certs && "
                    "touch /browser-runtime/supervisord/chromedriver && "
                    "chown -R 1000:1000 /browser-runtime && "
                    "chmod -R 0770 /browser-runtime"
                ),
            ],
            security_context=client.V1SecurityContext(run_as_user=0),
            volume_mounts=[
                client.V1VolumeMount(
                    name="browser-runtime",
                    mount_path="/browser-runtime",
                ),
            ],
        )
        container = client.V1Container(
            name="browser",
            image=spec.image or self._image,
            image_pull_policy=_image_pull_policy(spec.image or self._image),
            ports=[
                client.V1ContainerPort(container_port=SERVICE_PORT_REST, name="rest"),
                client.V1ContainerPort(container_port=SERVICE_PORT_CDP, name="cdp"),
                client.V1ContainerPort(
                    container_port=TARGET_PORT_LIVE_VIEW,
                    name="live-view",
                ),
            ],
            readiness_probe=client.V1Probe(
                http_get=client.V1HTTPGetAction(
                    path="/spec.json",
                    port=SERVICE_PORT_REST,
                ),
                period_seconds=2,
                failure_threshold=30,
            ),
            resources=client.V1ResourceRequirements(
                requests={"cpu": spec.cpu, "memory": spec.memory},
                limits={"cpu": spec.cpu_limit, "memory": spec.memory_limit},
            ),
            env=env_vars,
            volume_mounts=volume_mounts,
        )
        containers = [container]

        if spec.workspace_source_ref:
            containers.append(
                self._build_s3fs_container(
                    source_ref=spec.workspace_source_ref,
                    secret_name=secret_name,
                )
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
                volumes=volumes,
                init_containers=[init_container],
                containers=containers,
            ),
        )

    def _build_s3fs_container(
        self,
        *,
        source_ref: str,
        secret_name: str | None,
    ) -> client.V1Container:
        if not secret_name:
            raise ValueError("workspace_source_ref requires an S3 secret name")
        session_bucket_path = self._session_bucket_path(source_ref)
        s3_endpoint = self._s3_endpoint or ""
        if not s3_endpoint and self._storage:
            s3_endpoint = getattr(self._storage, "endpoint", "")
        return client.V1Container(
            name="s3fs",
            image=self._s3fs_image,
            security_context=client.V1SecurityContext(privileged=True),
            env=[
                client.V1EnvVar(name="S3_BUCKET_PATH", value=session_bucket_path),
                client.V1EnvVar(name="S3_ENDPOINT", value=s3_endpoint),
                client.V1EnvVar(
                    name="S3_REGION",
                    value=self._resolve_s3_region(s3_endpoint),
                ),
            ],
            env_from=[
                client.V1EnvFromSource(
                    secret_ref=client.V1SecretEnvSource(name=secret_name),
                ),
            ],
            volume_mounts=[
                client.V1VolumeMount(
                    name="workspace",
                    mount_path="/workspace",
                    mount_propagation="Bidirectional",
                ),
            ],
        )

    @staticmethod
    def _session_bucket_path(source_ref: str) -> str:
        if not source_ref.startswith("s3://"):
            raise ValueError("workspace_source_ref must use s3://")
        source = source_ref[5:].rstrip("/")
        if "/" in source:
            bucket, path = source.split("/", 1)
            return f"{bucket}:/{path}"
        return source

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

    async def _wait_for_endpoint(self, rest_url: str) -> None:
        # Pod-Ready means the kubelet's local readiness probe passed; it does
        # not guarantee Service EndpointSlice / kube-proxy / NetworkPolicy
        # rules have propagated to the *caller's* node. Probe from here until
        # /spec.json answers, so tool code never burns a 30s ConnectTimeout
        # on the cold path.
        deadline = asyncio.get_event_loop().time() + self._endpoint_probe_timeout
        last_error: Exception | None = None
        probe_timeout = httpx.Timeout(connect=2.0, read=5.0, write=5.0, pool=2.0)
        async with httpx.AsyncClient(
            base_url=rest_url, timeout=probe_timeout
        ) as probe:
            while True:
                try:
                    response = await probe.get("/spec.json")
                    if response.status_code == 200:
                        return
                    last_error = RuntimeError(
                        f"HTTP {response.status_code} from /spec.json",
                    )
                except httpx.HTTPError as exc:
                    last_error = exc
                if asyncio.get_event_loop().time() >= deadline:
                    raise RuntimeError(
                        f"endpoint {rest_url} unreachable within "
                        f"{self._endpoint_probe_timeout}s: {last_error}",
                    )
                await asyncio.sleep(1.0)

    async def _delete_pod_safe(self, api: client.CoreV1Api, pod_name: str) -> None:
        try:
            await api.delete_namespaced_pod(
                pod_name,
                self._namespace,
                grace_period_seconds=0,
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

    async def _create_s3_secret(self, api: client.CoreV1Api, secret_name: str) -> None:
        access_key = ""
        secret_key = ""
        if self._storage:
            access_key = getattr(self._storage, "access_key", "")
            secret_key = getattr(self._storage, "secret_key", "")

        secret = client.V1Secret(
            metadata=client.V1ObjectMeta(
                name=secret_name,
                namespace=self._namespace,
                labels={"app": "surogates-browser"},
            ),
            string_data={
                "AWS_ACCESS_KEY_ID": access_key,
                "AWS_SECRET_ACCESS_KEY": secret_key,
            },
        )
        try:
            await api.create_namespaced_secret(self._namespace, secret)
        except ApiException as exc:
            if exc.status != 409:
                raise

    async def _delete_secret_safe(
        self, api: client.CoreV1Api, secret_name: str
    ) -> None:
        try:
            await api.delete_namespaced_secret(secret_name, self._namespace)
        except ApiException as exc:
            if exc.status != 404:
                logger.warning(
                    "Failed to delete browser secret %s: %s", secret_name, exc
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

    _DEFAULT_REGION = "eu-central-1"

    def _resolve_s3_region(self, s3_endpoint: str) -> str:
        explicit = ""
        if self._storage:
            explicit = getattr(self._storage, "region", "") or ""
        if explicit:
            return explicit

        host = ""
        if s3_endpoint:
            try:
                host = urlparse(s3_endpoint).hostname or ""
            except (ValueError, TypeError):
                host = ""

        if host.endswith(".amazonaws.com"):
            match = re.match(r"^s3[.-]([a-z0-9-]+)\.amazonaws\.com$", host)
            if match:
                return match.group(1)

        return self._DEFAULT_REGION

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
                        target_port=TARGET_PORT_LIVE_VIEW,
                    ),
                ],
            ),
        )
