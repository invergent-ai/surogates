"""Kubernetes sandbox backend.

Provisions ephemeral K8s pods as sandboxes — one pod per session.
Each pod has:

- A main container running the agent sandbox image (bash, git, etc.)
- An s3fs sidecar that FUSE-mounts the session's S3 bucket as ``/workspace``
- Session-scoped S3 credentials injected via a K8s Secret

The worker communicates with the sandbox pod via the K8s exec API
(``kubernetes_asyncio``).  No HTTP server runs inside the sandbox.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from kubernetes_asyncio import client, config, watch
from kubernetes_asyncio.client import ApiException
from kubernetes_asyncio.stream import WsApiClient

from surogates.sandbox.base import (
    SandboxSpec,
    SandboxStatus,
    SandboxUnavailableError,
)

logger = logging.getLogger(__name__)

# Safety: max lifetime for a sandbox pod (seconds).
_DEFAULT_ACTIVE_DEADLINE = 3600  # 1 hour


@dataclass
class _PodEntry:
    """Internal bookkeeping for a provisioned sandbox pod."""

    sandbox_id: str
    pod_name: str
    secret_name: str
    namespace: str
    spec: SandboxSpec
    status: SandboxStatus = SandboxStatus.PENDING


class K8sSandbox:
    """Kubernetes sandbox backend.

    Creates one K8s pod per sandbox instance.  Implements the
    :class:`~surogates.sandbox.base.Sandbox` protocol.

    Parameters
    ----------
    namespace:
        K8s namespace for sandbox pods.
    service_account:
        ServiceAccount for sandbox pods (should have no K8s API permissions).
    pod_ready_timeout:
        Seconds to wait for a pod to become Ready after creation.
    executor_path:
        Path to the tool-executor binary inside the sandbox image.
    storage_settings:
        Storage configuration (for S3 endpoint/credentials).
    s3fs_image:
        Container image for the s3fs-fuse sidecar.
    """

    def __init__(
        self,
        namespace: str = "surogates",
        service_account: str = "surogates-sandbox",
        pod_ready_timeout: int = 60,
        executor_path: str = "/usr/local/bin/tool-executor",
        storage_settings: Any = None,
        s3fs_image: str = "ghcr.io/invergent-ai/s3fs-fuse:latest",
        s3_endpoint: str = "",
        mcp_proxy_url: str = "",
    ) -> None:
        self._namespace = namespace
        self._service_account = service_account
        self._pod_ready_timeout = pod_ready_timeout
        self._executor_path = executor_path
        self._storage = storage_settings
        self._s3fs_image = s3fs_image
        self._s3_endpoint = s3_endpoint
        self._mcp_proxy_url = mcp_proxy_url
        self._pods: dict[str, _PodEntry] = {}
        self._api: client.CoreV1Api | None = None

    # ------------------------------------------------------------------
    # K8s client
    # ------------------------------------------------------------------

    async def _get_api(self) -> client.CoreV1Api:
        """Return a cached CoreV1Api client.

        Tries in-cluster config first (production), falls back to
        kubeconfig (local dev running outside the cluster).
        """
        if self._api is None:
            try:
                config.load_incluster_config()
            except config.ConfigException:
                await config.load_kube_config()
            self._api = client.CoreV1Api()
        return self._api

    # ------------------------------------------------------------------
    # Sandbox protocol
    # ------------------------------------------------------------------

    async def provision(self, spec: SandboxSpec) -> str:
        """Create a sandbox pod and wait for it to become ready."""
        api = await self._get_api()
        sandbox_id = uuid.uuid4().hex
        pod_name = f"sandbox-{sandbox_id[:12]}"
        secret_name = f"sandbox-s3-{sandbox_id[:12]}"

        # 1. Create K8s Secret with session-scoped S3 credentials.
        await self._create_s3_secret(api, secret_name)

        # 2. Build and create the pod.
        pod_manifest = self._build_pod_manifest(
            sandbox_id, pod_name, secret_name, spec,
        )
        try:
            await api.create_namespaced_pod(self._namespace, pod_manifest)
        except ApiException as exc:
            logger.error("Failed to create sandbox pod %s: %s", pod_name, exc)
            await self._delete_secret_safe(api, secret_name)
            raise SandboxUnavailableError(
                self._classify_create_pod_failure(exc),
            ) from exc

        entry = _PodEntry(
            sandbox_id=sandbox_id,
            pod_name=pod_name,
            secret_name=secret_name,
            namespace=self._namespace,
            spec=spec,
        )
        self._pods[sandbox_id] = entry

        # 3. Wait for the pod to become ready.
        try:
            await self._wait_for_ready(api, pod_name)
            entry.status = SandboxStatus.RUNNING
        except Exception as exc:
            logger.error("Sandbox pod %s failed to become ready", pod_name, exc_info=True)
            await self._destroy_entry(api, entry)
            raise SandboxUnavailableError(
                f"Sandbox pod {pod_name} failed to become ready: {exc}",
            ) from exc

        logger.info("Provisioned K8s sandbox %s (pod %s)", sandbox_id, pod_name)
        return sandbox_id

    async def execute(self, sandbox_id: str, name: str, input: str) -> str:
        """Execute a command in the sandbox pod via K8s exec API."""
        import aiohttp

        entry = self._get_entry(sandbox_id)
        api = await self._get_api()

        command = [self._executor_path, name, input]

        try:
            resp = await asyncio.wait_for(
                self._exec_in_pod(api, entry.pod_name, command),
                timeout=entry.spec.timeout + 5,  # buffer over tool timeout
            )
            return resp
        except asyncio.TimeoutError:
            logger.warning("Sandbox exec timed out in pod %s", entry.pod_name)
            return self._result_json(
                exit_code=-1,
                stdout="",
                stderr="Execution timed out",
                truncated=False,
                timed_out=True,
            )
        except (
            aiohttp.WSServerHandshakeError,
            aiohttp.ClientConnectionError,
            ApiException,
        ) as exc:
            # WS handshake / connection failure means the exec endpoint
            # itself is unreachable -- typically pod missing, RBAC
            # misconfiguration, or the cluster is down.  Every subsequent
            # sandbox tool will fail identically; raise so the harness
            # can surface a single "sandbox unavailable" result.
            logger.error("Sandbox exec infra failure in pod %s: %s", entry.pod_name, exc)
            entry.status = SandboxStatus.FAILED
            raise SandboxUnavailableError(
                self._classify_exec_failure(entry.pod_name, exc),
            ) from exc
        except Exception as exc:
            logger.error("Sandbox exec failed in pod %s: %s", entry.pod_name, exc)
            entry.status = SandboxStatus.FAILED
            return self._result_json(
                exit_code=-1,
                stdout="",
                stderr=f"Sandbox execution error: {exc}",
                truncated=False,
                timed_out=False,
            )

    async def destroy(self, sandbox_id: str) -> None:
        """Delete the sandbox pod and its S3 credential secret."""
        entry = self._pods.pop(sandbox_id, None)
        if entry is None:
            logger.warning("Attempted to destroy unknown sandbox %s", sandbox_id)
            return

        api = await self._get_api()
        await self._destroy_entry(api, entry)
        logger.info("Destroyed K8s sandbox %s (pod %s)", sandbox_id, entry.pod_name)

    async def status(self, sandbox_id: str) -> SandboxStatus:
        """Check the current status of the sandbox pod.

        Uses ``read_namespaced_pod`` (resource ``pods``) rather than
        ``read_namespaced_pod_status`` (resource ``pods/status``) so the
        worker only needs ``get`` on ``pods`` -- one fewer RBAC entry to
        keep in sync.

        On any non-404 API error (transient network failure, momentary
        RBAC misconfiguration) the cached status is returned instead of
        ``FAILED``.  Returning ``FAILED`` on a status-read error would
        cause :class:`SandboxPool` to destroy the (still healthy) pod
        and reprovision -- a tight loop that wastes K8s churn and
        produces nothing.  A 404 is the only signal that the pod is
        truly gone.
        """
        entry = self._pods.get(sandbox_id)
        if entry is None:
            return SandboxStatus.TERMINATED

        api = await self._get_api()
        try:
            pod = await api.read_namespaced_pod(entry.pod_name, self._namespace)
            new_status = self._map_pod_status(pod)
            entry.status = new_status
            return new_status
        except ApiException as exc:
            if exc.status == 404:
                self._pods.pop(sandbox_id, None)
                return SandboxStatus.TERMINATED
            logger.warning(
                "Status check for pod %s failed (HTTP %s); trusting "
                "cached status %s", entry.pod_name, exc.status, entry.status,
            )
            return entry.status

    # ------------------------------------------------------------------
    # Pod manifest builder
    # ------------------------------------------------------------------

    def _build_pod_manifest(
        self,
        sandbox_id: str,
        pod_name: str,
        secret_name: str,
        spec: SandboxSpec,
    ) -> client.V1Pod:
        """Build the K8s pod manifest for a sandbox."""
        # Parse resources from spec for s3fs mount.
        session_bucket = ""
        for res in spec.resources:
            if res.source_ref.startswith("s3://"):
                session_bucket = res.source_ref[5:]
                break

        # Use the in-cluster S3 endpoint (reachable from inside the pod),
        # falling back to the storage config endpoint.
        s3_endpoint = self._s3_endpoint or ""
        if not s3_endpoint and self._storage:
            s3_endpoint = getattr(self._storage, "endpoint", "")

        # Environment variables for the main container.
        env_vars = [
            client.V1EnvVar(name="WORKSPACE_DIR", value="/workspace"),
        ]
        if self._mcp_proxy_url:
            env_vars.append(client.V1EnvVar(
                name="MCP_PROXY_URL", value=self._mcp_proxy_url,
            ))
            # Mint a sandbox token for MCP proxy authentication.
            from surogates.tenant.auth.jwt import create_sandbox_token
            sandbox_token = create_sandbox_token(
                org_id=uuid.UUID(spec.env.get("ORG_ID", "00000000-0000-0000-0000-000000000000")),
                user_id=uuid.UUID(spec.env.get("USER_ID", "00000000-0000-0000-0000-000000000000")),
                session_id=uuid.UUID(sandbox_id),
            )
            env_vars.append(client.V1EnvVar(
                name="MCP_PROXY_TOKEN", value=sandbox_token,
            ))
        for k, v in spec.env.items():
            env_vars.append(client.V1EnvVar(name=k, value=v))

        # Main sandbox container.
        sandbox_container = client.V1Container(
            name="sandbox",
            image=spec.image,
            command=["sleep", "infinity"],
            resources=client.V1ResourceRequirements(
                requests={"cpu": spec.cpu, "memory": spec.memory},
                limits={
                    "cpu": spec.cpu_limit,
                    "memory": spec.memory_limit,
                },
            ),
            env=env_vars,
            volume_mounts=[
                client.V1VolumeMount(
                    name="workspace",
                    mount_path="/workspace",
                    mount_propagation="HostToContainer",
                ),
            ],
        )

        # s3fs sidecar container — uses the entrypoint.sh from the image.
        s3fs_env = [
            client.V1EnvVar(name="S3_BUCKET", value=session_bucket),
            client.V1EnvVar(name="S3_ENDPOINT", value=s3_endpoint),
        ]

        s3fs_container = client.V1Container(
            name="s3fs",
            image=self._s3fs_image,
            security_context=client.V1SecurityContext(privileged=True),
            env=s3fs_env,
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

        return client.V1Pod(
            metadata=client.V1ObjectMeta(
                name=pod_name,
                namespace=self._namespace,
                labels={
                    "app": "surogates-sandbox",
                    "surogates.ai/sandbox-id": sandbox_id,
                },
                annotations={
                    "surogates.ai/created-at": datetime.now(timezone.utc).isoformat(),
                },
            ),
            spec=client.V1PodSpec(
                service_account_name=self._service_account,
                active_deadline_seconds=_DEFAULT_ACTIVE_DEADLINE,
                restart_policy="Never",
                volumes=[
                    client.V1Volume(
                        name="workspace",
                        empty_dir=client.V1EmptyDirVolumeSource(),
                    ),
                ],
                containers=[sandbox_container, s3fs_container],
            ),
        )

    # ------------------------------------------------------------------
    # K8s exec
    # ------------------------------------------------------------------

    async def _exec_in_pod(
        self, api: client.CoreV1Api, pod_name: str, command: list[str],
    ) -> str:
        """Execute a command in the sandbox container and return stdout.

        Uses ``WsApiClient`` from ``kubernetes-asyncio`` to get a proper
        websocket-based exec stream with channel multiplexing.
        """
        from kubernetes_asyncio.stream import WsApiClient
        from kubernetes_asyncio.stream.ws_client import STDOUT_CHANNEL, STDERR_CHANNEL, ERROR_CHANNEL

        # WsApiClient must be used instead of the regular ApiClient
        # to get websocket exec with channel separation.
        async with WsApiClient() as ws_api:
            ws_core = client.CoreV1Api(api_client=ws_api)
            resp = await ws_core.connect_get_namespaced_pod_exec(
                name=pod_name,
                namespace=self._namespace,
                container="sandbox",
                command=command,
                stderr=True,
                stdin=False,
                stdout=True,
                tty=False,
            )

        # resp is a WsResponse — read the full content.
        # WsApiClient with _preload_content=True (default) merges
        # stdout and stderr into a single string.
        if isinstance(resp, str):
            raw = resp
        elif isinstance(resp, bytes):
            raw = resp.decode("utf-8", errors="replace")
        elif hasattr(resp, "data"):
            # WsResponse or similar object with a .data attribute.
            data = resp.data
            raw = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else str(data)
        else:
            raw = str(resp)

        logger.debug("Sandbox exec raw output (%d chars): %s", len(raw), raw[:500])

        # The tool-executor writes a JSON result as the LAST line of
        # output.  Stderr (e.g. git progress) may precede it.
        # Find the last valid JSON object in the output.
        last_json = None
        for line in reversed(raw.strip().splitlines()):
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                try:
                    json.loads(line)
                    last_json = line
                    break
                except json.JSONDecodeError:
                    # May be Python repr with single quotes — try converting.
                    try:
                        import ast
                        obj = ast.literal_eval(line)
                        if isinstance(obj, dict):
                            last_json = json.dumps(obj)
                            break
                    except (ValueError, SyntaxError):
                        continue

        if last_json:
            return last_json

        # Fallback: no valid JSON found — wrap raw output.
        return self._result_json(
            exit_code=0,
            stdout=raw,
            stderr="",
            truncated=False,
            timed_out=False,
        )

    # ------------------------------------------------------------------
    # S3 credential secret
    # ------------------------------------------------------------------

    async def _create_s3_secret(self, api: client.CoreV1Api, secret_name: str) -> None:
        """Create a K8s Secret with S3 credentials for the sandbox."""
        access_key = ""
        secret_key = ""
        if self._storage:
            access_key = getattr(self._storage, "access_key", "")
            secret_key = getattr(self._storage, "secret_key", "")

        secret = client.V1Secret(
            metadata=client.V1ObjectMeta(
                name=secret_name,
                namespace=self._namespace,
                labels={"app": "surogates-sandbox"},
            ),
            string_data={
                "AWS_ACCESS_KEY_ID": access_key,
                "AWS_SECRET_ACCESS_KEY": secret_key,
            },
        )
        try:
            await api.create_namespaced_secret(self._namespace, secret)
        except ApiException as exc:
            if exc.status != 409:  # Already exists — OK
                raise

    async def _delete_secret_safe(self, api: client.CoreV1Api, secret_name: str) -> None:
        """Delete a secret, ignoring 404."""
        try:
            await api.delete_namespaced_secret(secret_name, self._namespace)
        except ApiException as exc:
            if exc.status != 404:
                logger.warning("Failed to delete secret %s: %s", secret_name, exc)

    # ------------------------------------------------------------------
    # Pod lifecycle helpers
    # ------------------------------------------------------------------

    async def _wait_for_ready(self, api: client.CoreV1Api, pod_name: str) -> None:
        """Watch the pod until it's Ready or the timeout expires."""
        w = watch.Watch()
        try:
            async with asyncio.timeout(self._pod_ready_timeout):
                async for event in w.stream(
                    api.list_namespaced_pod,
                    namespace=self._namespace,
                    field_selector=f"metadata.name={pod_name}",
                    timeout_seconds=self._pod_ready_timeout,
                ):
                    pod = event["object"]
                    if self._is_pod_ready(pod):
                        return
                    phase = pod.status.phase if pod.status else "Unknown"
                    if phase in ("Failed", "Succeeded"):
                        raise RuntimeError(
                            f"Sandbox pod {pod_name} entered {phase} phase"
                        )
        except TimeoutError:
            raise RuntimeError(
                f"Sandbox pod {pod_name} did not become ready "
                f"within {self._pod_ready_timeout}s"
            )
        finally:
            w.stop()

    async def _destroy_entry(self, api: client.CoreV1Api, entry: _PodEntry) -> None:
        """Delete a pod and its secret, handling errors gracefully."""
        try:
            await api.delete_namespaced_pod(
                entry.pod_name, entry.namespace,
                grace_period_seconds=5,
            )
        except ApiException as exc:
            if exc.status != 404:
                logger.warning("Failed to delete pod %s: %s", entry.pod_name, exc)

        await self._delete_secret_safe(api, entry.secret_name)

    # ------------------------------------------------------------------
    # Status mapping
    # ------------------------------------------------------------------

    @staticmethod
    def _is_pod_ready(pod: client.V1Pod) -> bool:
        """Return True if the pod has a Ready condition."""
        if not pod.status or not pod.status.conditions:
            return False
        return any(
            c.type == "Ready" and c.status == "True"
            for c in pod.status.conditions
        )

    @staticmethod
    def _map_pod_status(pod: client.V1Pod) -> SandboxStatus:
        """Map K8s pod phase + conditions to SandboxStatus."""
        if not pod.status:
            return SandboxStatus.PENDING

        phase = pod.status.phase
        if phase == "Running" and K8sSandbox._is_pod_ready(pod):
            return SandboxStatus.RUNNING
        if phase == "Pending":
            return SandboxStatus.PENDING
        if phase in ("Failed", "Unknown"):
            return SandboxStatus.FAILED
        if phase == "Succeeded":
            return SandboxStatus.TERMINATED

        return SandboxStatus.PENDING

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _get_entry(self, sandbox_id: str) -> _PodEntry:
        """Look up a sandbox entry, raising ValueError if not found."""
        try:
            return self._pods[sandbox_id]
        except KeyError:
            raise ValueError(f"Unknown sandbox: {sandbox_id}") from None

    @staticmethod
    def _classify_create_pod_failure(exc: ApiException) -> str:
        """Map a pod-create ApiException into a human-readable reason.

        The body is a K8s Status JSON; pull ``message`` and prefix with the
        most common diagnoses so the LLM-facing text reads as a triage
        rather than a stack trace.
        """
        try:
            body = json.loads(exc.body) if exc.body else {}
            message = body.get("message", str(exc))
        except (json.JSONDecodeError, TypeError):
            message = str(exc)

        if exc.status == 403:
            return f"Sandbox pod creation forbidden by Kubernetes RBAC: {message}"
        if exc.status == 404:
            return f"Sandbox namespace or referenced resource missing: {message}"
        if exc.status == 409:
            return f"Sandbox pod name conflict: {message}"
        return f"Sandbox pod creation failed (HTTP {exc.status}): {message}"

    @staticmethod
    def _classify_exec_failure(pod_name: str, exc: BaseException) -> str:
        """Map an exec-time WS / API failure into a human-readable reason."""
        status = getattr(exc, "status", None)
        if status == 401:
            return f"Sandbox exec unauthorized for pod {pod_name} (worker token rejected)."
        if status == 403:
            return f"Sandbox exec forbidden for pod {pod_name} (worker SA missing pods/exec permission)."
        if status == 404:
            return f"Sandbox pod {pod_name} not found (likely terminated or never created)."
        if isinstance(exc, ApiException):
            return f"Sandbox exec failed for pod {pod_name} (HTTP {exc.status}): {exc.reason}"
        return f"Sandbox exec connection failed for pod {pod_name}: {exc}"

    @staticmethod
    def _result_json(
        *,
        exit_code: int,
        stdout: str,
        stderr: str,
        truncated: bool,
        timed_out: bool,
    ) -> str:
        """Build the standard sandbox result JSON."""
        return json.dumps({
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
            "truncated": truncated,
            "timed_out": timed_out,
        })
