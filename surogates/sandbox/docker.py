"""Docker sandbox backend -- one container per root session (local dev).

Runs the agent-sandbox image (whose main process is the tool-executor
daemon) as a Docker container and talks to it over HTTP via the shared
:class:`ExecutorHTTPClient`, mirroring the K8s backend's executor contract
through the browser backend's ``docker run`` lifecycle.  Intended for
local development on a trusted single-user host; production multi-tenant
isolation remains the Kubernetes backend's job.
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx

from surogates.sandbox._executor_client import ExecutorHTTPClient
from surogates.sandbox.base import (
    SandboxSpec,
    SandboxStatus,
    SandboxUnavailableError,
)

logger = logging.getLogger(__name__)

# The image's TOOL_EXECUTOR_PORT default; the daemon always binds this
# fixed in-container port and only the published host port varies.
_IN_CONTAINER_PORT = 8071
_WORKSPACE_SENTINEL = "/workspace"
_PORT_CONFLICT_RE = re.compile(
    r"(port is already allocated|Bind for .* failed)", re.IGNORECASE,
)
_MAX_PORT_ATTEMPTS = 25


def _rewrite_host_for_container(url: str) -> str:
    """Rewrite host-local URLs so a bridged container can reach host services."""
    return url.replace("127.0.0.1", "host.docker.internal").replace(
        "localhost", "host.docker.internal",
    )


class _DockerDriver(Protocol):
    async def run(self, args: list[str]) -> tuple[int, bytes, bytes]: ...


class _RealDocker:
    async def run(self, args: list[str]) -> tuple[int, bytes, bytes]:
        proc = await asyncio.create_subprocess_exec(
            "docker", *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        return proc.returncode or 0, stdout, stderr


@dataclass(slots=True)
class _Entry:
    sandbox_id: str
    container_id: str
    host_port: int
    token: str
    spec: SandboxSpec
    status: SandboxStatus = SandboxStatus.RUNNING


class DockerSandbox:
    """Sandbox backend that runs one Docker container per root session."""

    def __init__(
        self,
        *,
        image: str = "ghcr.io/invergent-ai/surogates-agent-sandbox:latest",
        executor_port_base: int = 33000,
        ready_timeout: int = 60,
        network: str = "bridge",
        mcp_proxy_url: str = "",
        storage_settings: Any = None,
        docker: _DockerDriver | None = None,
        httpx_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._image = image
        self._port_base = executor_port_base
        self._ready_timeout = ready_timeout
        self._network = network
        self._mcp_proxy_url = mcp_proxy_url
        self._storage = storage_settings
        self._docker = docker or _RealDocker()
        self._transport = httpx_transport
        self._client = ExecutorHTTPClient()
        self._entries: dict[str, _Entry] = {}
        self._next_offset = 0
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Sandbox protocol
    # ------------------------------------------------------------------

    async def provision(self, spec: SandboxSpec) -> str:
        sandbox_id = uuid.uuid4().hex
        token = secrets.token_urlsafe(32)

        # Reap stale containers left by a previous worker for this root
        # session before claiming a new one.
        if spec.session_id:
            await self.destroy_for_session(spec.session_id)

        workspace = self._mountable_workspace(spec.workspace_path)
        env = self._build_env(spec, sandbox_id, token)
        # Docker is the local-dev backend: the configured image (docker_image)
        # is authoritative, so a developer's locally-built image is used rather
        # than the production ghcr reference that SandboxSpec.image defaults to.
        image = self._image

        container_id = ""
        for _attempt in range(_MAX_PORT_ATTEMPTS):
            async with self._lock:
                offset = self._next_offset
                self._next_offset += 1
            host_port = self._port_base + offset

            args = ["run", "-d", "--rm", "-p", f"{host_port}:{_IN_CONTAINER_PORT}"]
            if self._network:
                args += ["--network", self._network]
            args += [
                "--add-host", "host.docker.internal:host-gateway",
                "--label", "app=surogates-sandbox",
            ]
            if spec.session_id:
                args += ["--label", f"surogates.session_id={spec.session_id}"]
            if workspace is not None:
                args += ["-v", f"{workspace}:/workspace"]
            for key, value in env.items():
                args += ["-e", f"{key}={value}"]
            args.append(image)

            code, stdout, stderr = await self._docker.run(args)
            if code != 0:
                stderr_text = stderr.decode(errors="replace")
                if _PORT_CONFLICT_RE.search(stderr_text):
                    logger.warning(
                        "Sandbox port %d unavailable; trying next offset", host_port,
                    )
                    continue
                raise SandboxUnavailableError(
                    f"docker run failed (exit {code}): {stderr_text}",
                    classification="docker",
                )
            container_id = stdout.decode().strip().splitlines()[0]
            break
        else:
            raise SandboxUnavailableError(
                "docker run failed: no free sandbox ports found",
                classification="docker",
            )

        try:
            await self._wait_ready(host_port)
        except Exception:
            await self._docker.run(["stop", container_id])
            await self._docker.run(["rm", container_id])
            raise

        self._entries[sandbox_id] = _Entry(
            sandbox_id=sandbox_id,
            container_id=container_id,
            host_port=host_port,
            token=token,
            spec=spec,
        )
        logger.info(
            "Provisioned docker sandbox %s (container %s, port %d)",
            sandbox_id, container_id, host_port,
        )
        return sandbox_id

    async def execute(self, sandbox_id: str, name: str, input: str) -> str:
        entry = self._entries.get(sandbox_id)
        if entry is None:
            raise ValueError(f"Unknown sandbox: {sandbox_id}")
        try:
            return await self._client.execute(
                host="127.0.0.1",
                port=entry.host_port,
                token=entry.token,
                name=name,
                args_str=input,
                timeout=entry.spec.timeout,
            )
        except SandboxUnavailableError:
            entry.status = SandboxStatus.FAILED
            raise

    async def status(self, sandbox_id: str) -> SandboxStatus:
        entry = self._entries.get(sandbox_id)
        if entry is None:
            return SandboxStatus.TERMINATED
        code, stdout, _stderr = await self._docker.run(
            ["inspect", "--format", "{{.State.Status}}", entry.container_id],
        )
        if code != 0:
            return SandboxStatus.FAILED
        status = stdout.decode().strip()
        if status == "running":
            return SandboxStatus.RUNNING
        if status in {"created", "restarting"}:
            return SandboxStatus.PENDING
        if status in {"exited", "dead", "removing"}:
            return SandboxStatus.TERMINATED
        return SandboxStatus.FAILED

    async def destroy(self, sandbox_id: str) -> None:
        entry = self._entries.pop(sandbox_id, None)
        if entry is None:
            return
        await self._docker.run(["stop", entry.container_id])
        await self._docker.run(["rm", entry.container_id])
        logger.info("Destroyed docker sandbox %s", sandbox_id)

    async def destroy_for_session(self, session_id: str) -> None:
        code, stdout, stderr = await self._docker.run(
            ["ps", "-aq", "--filter", f"label=surogates.session_id={session_id}"],
        )
        if code != 0:
            logger.warning(
                "Failed to list sandbox containers for session %s: %s",
                session_id, stderr.decode(errors="replace"),
            )
            return
        for container_id in stdout.decode().split():
            # Drop any in-memory entry pointing at this container.
            for sid, entry in list(self._entries.items()):
                if entry.container_id == container_id:
                    self._entries.pop(sid, None)
            await self._docker.run(["stop", container_id])
            await self._docker.run(["rm", container_id])
            logger.info(
                "Destroyed sandbox container %s for session %s",
                container_id, session_id,
            )

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _build_env(
        self, spec: SandboxSpec, sandbox_id: str, token: str,
    ) -> dict[str, str]:
        """Container env: base + spec passthrough + MCP/KB host wiring.

        Mirrors the K8s pod manifest's env block, with host-local URLs
        rewritten so a bridged container can reach host services.
        """
        import os

        env = {
            "TOOL_EXECUTOR_TOKEN": token,
            "WORKSPACE_DIR": "/workspace",
            "TOOL_EXECUTOR_REQUIRE_FUSE": "0",
        }
        reserved = {
            "TOOL_EXECUTOR_TOKEN", "WORKSPACE_DIR",
            "TOOL_EXECUTOR_REQUIRE_FUSE", "TOOL_EXECUTOR_PORT",
        }
        for key, value in spec.env.items():
            if key not in reserved:
                env[key] = value

        # MCP proxy -- mirror the K8s pod manifest.
        if self._mcp_proxy_url:
            env["MCP_PROXY_URL"] = _rewrite_host_for_container(self._mcp_proxy_url)
            mcp_token = self._mint_mcp_token(spec, sandbox_id)
            if mcp_token:
                env["MCP_PROXY_TOKEN"] = mcp_token

        # KB env passthrough from the worker process, URLs rewritten for the
        # bridged container. Mirrors the K8s manifest's KB var loop.
        for kb_var in (
            "SUROGATES_AGENT_ID",
            "SUROGATES_OPS_DB_URL",
            "SUROGATES_KB_HUB_ENDPOINT_URL",
            "SUROGATES_KB_HUB_ACCESS_KEY_ID",
            "SUROGATES_KB_HUB_SECRET_ACCESS_KEY",
        ):
            val = os.environ.get(kb_var, "")
            if val:
                env[kb_var] = (
                    _rewrite_host_for_container(val)
                    if kb_var.endswith("_URL")
                    else val
                )
        return env

    def _mint_mcp_token(self, spec: SandboxSpec, sandbox_id: str) -> str:
        """Mint a sandbox->MCP-proxy token, mirroring the K8s manifest.

        Returns "" on any failure (e.g. non-UUID env in local dev) so a
        misconfigured MCP setup degrades to "MCP tools unavailable" rather
        than failing the whole provision.
        """
        from surogates.tenant.auth.jwt import create_sandbox_token

        zero = "00000000-0000-0000-0000-000000000000"
        try:
            session_uuid = (
                uuid.UUID(spec.session_id) if spec.session_id
                else uuid.UUID(sandbox_id)
            )
            return create_sandbox_token(
                org_id=uuid.UUID(spec.env.get("ORG_ID", zero)),
                user_id=uuid.UUID(spec.env.get("USER_ID", zero)),
                session_id=session_uuid,
                agent_id=spec.env.get("SUROGATES_AGENT_ID") or None,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Could not mint MCP proxy token for docker sandbox: %s", exc,
            )
            return ""

    def _has_s3_creds(self) -> bool:
        s = self._storage
        return bool(
            s
            and getattr(s, "access_key", "")
            and getattr(s, "secret_key", "")
        )

    def _s3_bucket_spec(self, spec: SandboxSpec) -> str | None:
        """Parse the spec's s3:// workspace Resource into geesefs's
        ``bucket:/prefix`` form (mirrors K8sSandbox._build_pod_manifest)."""
        for res in spec.resources:
            if res.source_ref.startswith("s3://"):
                source = res.source_ref[5:].rstrip("/")
                if "/" in source:
                    bucket, path = source.split("/", 1)
                    return f"{bucket}:/{path}"
                return source
        return None

    def _workspace_mode(self, spec: SandboxSpec) -> tuple[str, str | None]:
        """Decide how /workspace is backed for this provision.

        - ``("s3fs", bucket_spec)``  -- geesefs FUSE mount of R2 (needs creds)
        - ``("bind", host_path)``    -- host bind-mount
        - ``("ephemeral", None)``    -- container-internal scratch
        """
        bucket_spec = self._s3_bucket_spec(spec)
        if bucket_spec and self._has_s3_creds():
            return ("s3fs", bucket_spec)
        workspace = self._mountable_workspace(spec.workspace_path)
        if workspace is not None:
            return ("bind", str(workspace))
        return ("ephemeral", None)

    def _mountable_workspace(self, workspace_path: str | None) -> Path | None:
        # "/workspace" is the in-pod FUSE sentinel returned by S3Backend; it is
        # not bindable from the host. Empty/None means no workspace path.
        if not workspace_path or workspace_path == _WORKSPACE_SENTINEL:
            return None
        workspace = Path(workspace_path).resolve()
        try:
            workspace.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning(
                "Skipping sandbox workspace bind mount for %s: %s", workspace, exc,
            )
            return None
        if not workspace.is_dir():
            return None
        return workspace

    async def _wait_ready(self, host_port: int) -> None:
        deadline = asyncio.get_running_loop().time() + self._ready_timeout
        # Track the last observed reason across BOTH paths: a transport
        # exception (daemon not bound yet) and a non-200 response (daemon up
        # but unhealthy, e.g. an image whose /healthz still requires a FUSE
        # mount). Otherwise a persistent 503 would be misreported with a
        # stale connection-error name from the brief startup window.
        last_detail = ""
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{host_port}",
            transport=self._transport,
            timeout=2.0,
        ) as client:
            while asyncio.get_running_loop().time() < deadline:
                try:
                    response = await client.get("/healthz")
                    if response.status_code == 200:
                        return
                    last_detail = f"last status {response.status_code}"
                except Exception as exc:  # noqa: BLE001
                    last_detail = type(exc).__name__
                await asyncio.sleep(0.5)
        raise SandboxUnavailableError(
            f"Sandbox did not become ready within {self._ready_timeout}s "
            f"({last_detail or 'no_response'})",
            classification="readiness",
        )
