"""Kubernetes sandbox backend.

Provisions ephemeral K8s pods as sandboxes — one pod per session.
Each pod has:

- A main container running the agent sandbox image (bash, git, etc.)
- An s3fs sidecar that FUSE-mounts the session's S3 bucket as ``/workspace``
- Session-scoped S3 credentials injected via a K8s Secret

The worker communicates with the sandbox pod over HTTP: the sandbox
container's main process is the tool-executor daemon
(``surogates.sandbox.executor_server``), reached on the pod IP with a
per-sandbox bearer token minted at provision time.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from kubernetes_asyncio import client, config, watch
from kubernetes_asyncio.client import ApiException

from surogates.sandbox._executor_client import ExecutorHTTPClient
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
    pod_ip: str = ""
    token: str = ""
    status: SandboxStatus = SandboxStatus.PENDING
    # SSH remote-access resources reaped alongside the pod.
    ssh_secret_name: str | None = None
    ssh_netpol_name: str | None = None


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
    executor_port:
        Port the tool-executor daemon listens on inside the sandbox pod.
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
        executor_port: int = 8071,
        storage_settings: Any = None,
        s3fs_image: str = "ghcr.io/invergent-ai/s3fs-fuse:latest",
        s3_endpoint: str = "",
        mcp_proxy_url: str = "",
        ssh_agent_image: str = "ghcr.io/invergent-ai/surogates-ssh-agent:latest",
        ssh_egress_enforced: bool = False,
        ssh_egress_baseline_cidrs: list[str] | None = None,
    ) -> None:
        self._namespace = namespace
        self._service_account = service_account
        self._pod_ready_timeout = pod_ready_timeout
        self._executor_port = executor_port
        self._storage = storage_settings
        self._s3fs_image = s3fs_image
        self._s3_endpoint = s3_endpoint
        self._mcp_proxy_url = mcp_proxy_url
        # Isolated ssh-agent sidecar image + egress enforcement for the SSH
        # remote-access feature (see ``_build_ssh_sidecar`` / NetworkPolicy).
        self._ssh_agent_image = ssh_agent_image
        self._ssh_egress_enforced = ssh_egress_enforced
        self._ssh_egress_baseline_cidrs = list(ssh_egress_baseline_cidrs or ())
        self._pods: dict[str, _PodEntry] = {}
        self._api: client.CoreV1Api | None = None
        self._net_api: client.NetworkingV1Api | None = None
        self._client = ExecutorHTTPClient()

    # ------------------------------------------------------------------
    # K8s client
    # ------------------------------------------------------------------

    async def _get_api(self) -> client.CoreV1Api:
        """Return a cached CoreV1Api client.

        Tries in-cluster config first (production), falls back to
        kubeconfig (local dev running outside the cluster).  If neither
        path yields a usable config, raises ``SandboxUnavailableError``
        so the harness reports the failure as an infra issue rather
        than leaking a raw ``ConfigException`` to the LLM.
        """
        if self._api is None:
            try:
                config.load_incluster_config()
            except config.ConfigException:
                try:
                    await config.load_kube_config()
                except Exception as exc:
                    raise SandboxUnavailableError(
                        f"Kubernetes sandbox unavailable — could not load "
                        f"kubeconfig: {exc}. Check that your local cluster "
                        f"is running and ~/.kube/config is valid.",
                    ) from exc
            self._api = client.CoreV1Api()
        return self._api

    async def _get_net_api(self) -> client.NetworkingV1Api:
        """Return a cached NetworkingV1Api client (SSH egress policies).

        Reuses the same in-cluster/kubeconfig loading as ``_get_api``.
        """
        if self._net_api is None:
            await self._get_api()
            self._net_api = client.NetworkingV1Api()
        return self._net_api

    # ------------------------------------------------------------------
    # Sandbox protocol
    # ------------------------------------------------------------------

    async def provision(self, spec: SandboxSpec) -> str:
        """Create a sandbox pod and wait for it to become ready."""
        api = await self._get_api()
        sandbox_id = uuid.uuid4().hex
        pod_name = f"sandbox-{sandbox_id[:12]}"
        secret_name = f"sandbox-s3-{sandbox_id[:12]}"

        # SSH remote-access requires enforced egress; refuse rather than
        # silently degrade to config-only host scoping.
        self._require_ssh_egress_enforced(spec)

        # 1. Create K8s Secret with session-scoped S3 credentials.
        await self._create_s3_secret(api, secret_name)

        # 1b. Create the in-memory SSH key Secret (sidecar-only) when needed.
        ssh_secret_name: str | None = None
        if spec.ssh_key_material:
            ssh_secret_name = f"sandbox-ssh-{sandbox_id[:12]}"
            await self._create_ssh_secret(api, ssh_secret_name, spec.ssh_key_material)

        # 2. Build and create the pod.
        executor_token = secrets.token_urlsafe(32)
        pod_manifest = self._build_pod_manifest(
            sandbox_id, pod_name, secret_name, spec,
            executor_token=executor_token,
            ssh_secret_name=ssh_secret_name,
        )
        try:
            await api.create_namespaced_pod(self._namespace, pod_manifest)
        except ApiException as exc:
            logger.error("Failed to create sandbox pod %s: %s", pod_name, exc)
            await self._delete_secret_safe(api, secret_name)
            if ssh_secret_name:
                await self._delete_secret_safe(api, ssh_secret_name)
            raise SandboxUnavailableError(
                self._classify_create_pod_failure(exc),
            ) from exc

        entry = _PodEntry(
            sandbox_id=sandbox_id,
            pod_name=pod_name,
            secret_name=secret_name,
            namespace=self._namespace,
            spec=spec,
            token=executor_token,
            ssh_secret_name=ssh_secret_name,
        )
        self._pods[sandbox_id] = entry

        # 3. Wait for the pod to become ready (the readinessProbe gates
        # on the executor daemon being up AND /workspace being FUSE-
        # mounted), then capture the pod IP the daemon is reached on.
        try:
            await self._wait_for_ready(api, pod_name)
            pod = await api.read_namespaced_pod(pod_name, self._namespace)
            entry.pod_ip = (pod.status.pod_ip if pod.status else "") or ""
            if not entry.pod_ip:
                raise RuntimeError(f"Pod {pod_name} has no IP after becoming ready")
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
        """Execute a tool in the sandbox pod via the executor daemon.

        Delegates the HTTP transport to the shared client.  On a fatal
        condition (token rejected, daemon unreachable) the client raises
        ``SandboxUnavailableError``; we mark the entry FAILED so the next
        ``SandboxPool.ensure`` reprovisions, then re-raise.
        """
        entry = self._get_entry(sandbox_id)
        try:
            return await self._client.execute(
                host=entry.pod_ip,
                port=self._executor_port,
                token=entry.token,
                name=name,
                args_str=input,
                timeout=entry.spec.timeout,
            )
        except SandboxUnavailableError:
            entry.status = SandboxStatus.FAILED
            raise

    async def aclose(self) -> None:
        """Release the shared HTTP client session (worker shutdown)."""
        await self._client.aclose()

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
        *,
        executor_token: str,
        ssh_secret_name: str | None = None,
    ) -> client.V1Pod:
        """Build the K8s pod manifest for a sandbox."""
        # Parse resources from spec for s3fs mount.  s3fs accepts
        # "bucket:/prefix" to mount a path inside the bucket.
        session_bucket_path = ""
        for res in spec.resources:
            if res.source_ref.startswith("s3://"):
                source = res.source_ref[5:].rstrip("/")
                if "/" in source:
                    bucket, path = source.split("/", 1)
                    session_bucket_path = f"{bucket}:/{path}"
                else:
                    session_bucket_path = source
                break

        # Use the in-cluster S3 endpoint (reachable from inside the pod),
        # falling back to the storage config endpoint.
        s3_endpoint = self._s3_endpoint or ""
        if not s3_endpoint and self._storage:
            s3_endpoint = getattr(self._storage, "endpoint", "")

        # Environment variables for the main container.
        env_vars = [
            client.V1EnvVar(name="WORKSPACE_DIR", value="/workspace"),
            client.V1EnvVar(
                name="TOOL_EXECUTOR_PORT", value=str(self._executor_port),
            ),
            client.V1EnvVar(
                name="TOOL_EXECUTOR_TOKEN", value=executor_token,
            ),
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
                # Binds the sandbox token to its agent so the proxy can
                # reject a spoofed ``?agent_id=``.  Absent → unbound (the
                # proxy trusts the query param, as before).
                agent_id=spec.env.get("SUROGATES_AGENT_ID") or None,
                is_service_account=spec.env.get("SUROGATES_IS_SERVICE_ACCOUNT") == "1",
            )
            env_vars.append(client.V1EnvVar(
                name="MCP_PROXY_TOKEN", value=sandbox_token,
            ))
        for k, v in spec.env.items():
            env_vars.append(client.V1EnvVar(name=k, value=v))

        # Propagate KB-related env vars from the worker so that tool
        # handlers running inside the sandbox can reach the ops DB and
        # Hub.  Only added when non-empty so non-KB deployments are
        # unaffected.
        for kb_var in (
            "SUROGATES_AGENT_ID",
            "SUROGATES_OPS_DB_URL",
            "SUROGATES_KB_HUB_ENDPOINT_URL",
            "SUROGATES_KB_HUB_ACCESS_KEY_ID",
            "SUROGATES_KB_HUB_SECRET_ACCESS_KEY",
        ):
            val = os.environ.get(kb_var, "")
            if val:
                env_vars.append(client.V1EnvVar(name=kb_var, value=val))

        # Main sandbox container.
        sandbox_container = client.V1Container(
            name="sandbox",
            image=spec.image,
            # The daemon is the container's main process; its death
            # terminates the container (restartPolicy=Never -> pod Failed
            # -> pool status check reprovisions).
            command=[
                "tini", "--", "python", "-m", "surogates.sandbox.executor_server",
            ],
            readiness_probe=client.V1Probe(
                http_get=client.V1HTTPGetAction(
                    path="/healthz", port=self._executor_port,
                ),
                # Fast checks for quick provisioning; the high failure
                # threshold tolerates transient kubelet/CNI blips — the
                # fork-per-request daemon never starves /healthz, so 15
                # consecutive failures means genuinely broken (and the
                # pool's reprovision is then desired self-healing).
                period_seconds=1,
                timeout_seconds=2,
                failure_threshold=15,
            ),
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
        # ``S3_REGION`` is what s3fs passes as ``-o endpoint=`` for SigV4
        # signing.  When the bucket lives in real AWS, the region must
        # match the endpoint host, otherwise AWS rejects the pre-mount
        # ``s3fs_check_service`` call with HTTP 400 and s3fs exits cleanly,
        # leaving the pod NotReady forever.
        s3_region = self._resolve_s3_region(s3_endpoint)
        s3fs_env = [
            client.V1EnvVar(name="S3_BUCKET_PATH", value=session_bucket_path),
            client.V1EnvVar(name="S3_ENDPOINT", value=s3_endpoint),
            client.V1EnvVar(name="S3_REGION", value=s3_region),
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
                client.V1VolumeMount(
                    name="geesefs-cache",
                    mount_path="/var/cache/geesefs",
                ),
            ],
        )

        containers = [sandbox_container, s3fs_container]
        volumes = [
            client.V1Volume(
                name="workspace",
                empty_dir=client.V1EmptyDirVolumeSource(),
            ),
            # On-disk cache for the geesefs sidecar. Sized to bound
            # node ephemeral-storage pressure; if a session needs
            # more, the cache evicts LRU rather than failing writes.
            client.V1Volume(
                name="geesefs-cache",
                empty_dir=client.V1EmptyDirVolumeSource(
                    size_limit="2Gi",
                ),
            ),
        ]
        automount_sa_token = None

        # Isolated ssh-agent sidecar: holds the private key(s) in a separate
        # container the untrusted sandbox cannot read or ptrace.  The main
        # container gets ONLY the auth socket (a shared in-memory volume); the
        # key Secret is mounted solely into the sidecar.
        if spec.ssh_key_material and ssh_secret_name:
            sandbox_container.volume_mounts.append(
                client.V1VolumeMount(
                    name="ssh-agent-sock", mount_path="/ssh-agent",
                ),
            )
            volumes.extend([
                client.V1Volume(
                    name="ssh-agent-sock",
                    empty_dir=client.V1EmptyDirVolumeSource(medium="Memory"),
                ),
                client.V1Volume(
                    name="ssh-keys",
                    secret=client.V1SecretVolumeSource(
                        secret_name=ssh_secret_name, default_mode=0o400,
                    ),
                ),
                client.V1Volume(
                    name="ssh-tmp",
                    empty_dir=client.V1EmptyDirVolumeSource(medium="Memory"),
                ),
            ])
            containers.append(self._build_ssh_sidecar(spec, ssh_secret_name))
            # The sidecar (and the sandbox) need no K8s API access; withholding
            # the SA token shrinks the blast radius of the untrusted container.
            automount_sa_token = False

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
                automount_service_account_token=automount_sa_token,
                volumes=volumes,
                containers=containers,
            ),
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

    # ------------------------------------------------------------------
    # SSH remote-access: isolated ssh-agent sidecar
    # ------------------------------------------------------------------

    async def _create_ssh_secret(
        self, api: client.CoreV1Api, secret_name: str,
        ssh_key_material: dict[str, dict[str, str]],
    ) -> None:
        """Create the in-memory Secret holding the SSH private key(s).

        Mounted solely into the ssh-agent sidecar (never the main container).
        K8s Secret volumes are tmpfs-backed, and the Secret is reaped with the
        pod so no key material outlives the session.
        """
        data: dict[str, str] = {}
        for name, mat in ssh_key_material.items():
            data[f"id_{name}"] = mat.get("private_key", "")
            data[f"pass_{name}"] = mat.get("passphrase", "")
        secret = client.V1Secret(
            metadata=client.V1ObjectMeta(
                name=secret_name,
                namespace=self._namespace,
                labels={"app": "surogates-sandbox"},
            ),
            string_data=data,
        )
        try:
            await api.create_namespaced_secret(self._namespace, secret)
        except ApiException as exc:
            if exc.status != 409:
                raise

    def _build_ssh_sidecar(
        self, spec: SandboxSpec, ssh_secret_name: str,
    ) -> client.V1Container:
        """Build the isolated ssh-agent sidecar container.

        Runs as a distinct non-root uid with a read-only root filesystem and no
        Linux capabilities.  Loads each key into the agent (passphrase fed via
        ``SSH_ASKPASS`` non-interactively) then idles holding the socket.  The
        untrusted sandbox container shares only the auth socket.
        """
        key_names = sorted(spec.ssh_key_material)
        add_lines = "\n".join(
            (
                f'if [ -s /ssh-keys/pass_{n} ]; then '
                f'cat /ssh-keys/pass_{n} > /tmp/askpass_in; '
                f'SSH_ASKPASS=/tmp/askpass SSH_ASKPASS_REQUIRE=force DISPLAY=:0 '
                f'ssh-add /ssh-keys/id_{n} < /dev/null; '
                f'else ssh-add /ssh-keys/id_{n} < /dev/null; fi'
            )
            for n in key_names
        )
        script = (
            "set -eu\n"
            "umask 077\n"
            "cat > /tmp/askpass <<'EOF'\n#!/bin/sh\ncat /tmp/askpass_in\nEOF\n"
            "chmod 0700 /tmp/askpass\n"
            'eval "$(ssh-agent -a /ssh-agent/auth.sock)"\n'
            f"{add_lines}\n"
            "rm -f /tmp/askpass_in\n"
            "while kill -0 \"$SSH_AGENT_PID\" 2>/dev/null; do sleep 5; done\n"
        )
        return client.V1Container(
            name="ssh-agent",
            image=self._ssh_agent_image,
            command=["/bin/sh", "-c", script],
            security_context=client.V1SecurityContext(
                run_as_non_root=True,
                run_as_user=65532,
                read_only_root_filesystem=True,
                allow_privilege_escalation=False,
                capabilities=client.V1Capabilities(drop=["ALL"]),
            ),
            resources=client.V1ResourceRequirements(
                requests={"cpu": "10m", "memory": "32Mi"},
                limits={"cpu": "100m", "memory": "64Mi"},
            ),
            volume_mounts=[
                client.V1VolumeMount(
                    name="ssh-agent-sock", mount_path="/ssh-agent",
                ),
                client.V1VolumeMount(
                    name="ssh-keys", mount_path="/ssh-keys", read_only=True,
                ),
                client.V1VolumeMount(name="ssh-tmp", mount_path="/tmp"),
            ],
        )

    def _require_ssh_egress_enforced(self, spec: SandboxSpec) -> None:
        """Fail closed when SSH is requested but egress cannot be enforced.

        Host scoping is enforced by NetworkPolicy egress, not by the in-sandbox
        ssh config.  If the cluster is not configured to enforce it, refuse the
        SSH session rather than silently degrade to config-only controls.
        """
        if spec.ssh_key_material and not self._ssh_egress_enforced:
            raise SandboxUnavailableError(
                "SSH targets require enforced egress (ssh_egress_enforced); "
                "refusing to provision an SSH-enabled sandbox.",
            )

    async def _delete_network_policy_safe(
        self, api: client.CoreV1Api, name: str,
    ) -> None:
        """Delete the SSH NetworkPolicy, ignoring 404."""
        try:
            net_api = await self._get_net_api()
            await net_api.delete_namespaced_network_policy(name, self._namespace)
        except ApiException as exc:
            if exc.status != 404:
                logger.warning("Failed to delete network policy %s: %s", name, exc)

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
                grace_period_seconds=0,
            )
        except ApiException as exc:
            if exc.status != 404:
                logger.warning("Failed to delete pod %s: %s", entry.pod_name, exc)

        await self._delete_secret_safe(api, entry.secret_name)
        if entry.ssh_secret_name:
            await self._delete_secret_safe(api, entry.ssh_secret_name)
        if entry.ssh_netpol_name:
            await self._delete_network_policy_safe(api, entry.ssh_netpol_name)

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

    # Platform default region.  Used when the endpoint URL doesn't encode
    # one and ``storage.region`` is unset.  Garage/MinIO ignore this label
    # and AWS uses it for SigV4 signing, so the platform's home region is
    # the safe choice.
    _DEFAULT_REGION = "eu-central-1"

    def _resolve_s3_region(self, s3_endpoint: str) -> str:
        """Pick the SigV4 region label for s3fs's ``-o endpoint=`` flag.

        Precedence:
        1. Explicit ``storage.region`` setting — always wins.
        2. Parse from a regional AWS S3 hostname (``s3.<region>.amazonaws.com``
           or the legacy ``s3-<region>.amazonaws.com``).
        3. Fall back to :attr:`_DEFAULT_REGION`.
        """
        explicit = ""
        if self._storage:
            explicit = getattr(self._storage, "region", "") or ""
        if explicit:
            return explicit

        host = ""
        if s3_endpoint:
            try:
                from urllib.parse import urlparse
                host = urlparse(s3_endpoint).hostname or ""
            except (ValueError, TypeError):
                host = ""

        if host.endswith(".amazonaws.com"):
            import re
            m = re.match(r"^s3[.-]([a-z0-9-]+)\.amazonaws\.com$", host)
            if m:
                return m.group(1)

        return self._DEFAULT_REGION

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
