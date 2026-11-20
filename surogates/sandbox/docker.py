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
import os
import re
import secrets
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
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
# The host address the daemon's port is published on: the worker reaches it at 127.0.0.1 (``execute``).
_PUBLISH_HOST = "127.0.0.1"
_WORKSPACE_SENTINEL = "/workspace"
# A port another process took: older Docker says it is "already allocated", newer that binding it failed as the
# address is "already in use".
_PORT_CONFLICT_RE = re.compile(
    r"(port is already allocated|Bind for .* failed|failed to bind host port .*address already in use)", re.IGNORECASE,
)
_MAX_PORT_ATTEMPTS = 25
#: The most processes and threads a sandbox container may hold at once (``--pids-limit``), unless configured.
DEFAULT_PIDS_LIMIT = 4096
# Docker refuses a memory limit under 6 MiB, and --cpus under 0.01; it takes --cpus in nanoCPUs.
_MIN_MEMORY_BYTES = 6 * 2**20
_MIN_CPUS = Decimal("0.01")
_CPU_STEP = Decimal("0.001")

# Kubernetes quantity suffixes, the form SandboxSpec's limits are written in ("8Gi", "512Mi", "500m").
_BINARY_SUFFIXES = {"Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40, "Pi": 2**50, "Ei": 2**60}
_DECIMAL_SUFFIXES = {"k": 10**3, "M": 10**6, "G": 10**9, "T": 10**12, "P": 10**15, "E": 10**18}


def _quantity(value: str, what: str, example: str) -> Decimal | None:
    """The number in the Kubernetes quantity *value*, suffix applied; ``None`` for no limit ("" or any zero)."""
    text = str(value).strip()
    if not text:
        return None
    number, factor = text, Decimal(1)
    for suffix, scale in (*_BINARY_SUFFIXES.items(), *_DECIMAL_SUFFIXES.items(), ("m", Decimal("0.001"))):
        if text.endswith(suffix):
            number, factor = text[: -len(suffix)], Decimal(scale)
            break
    try:
        amount = Decimal(number) * factor
    except InvalidOperation:
        amount = Decimal(-1)
    if not amount.is_finite() or amount < 0:
        raise SandboxUnavailableError(
            f"The sandbox {what} {value!r} is not a quantity such as {example}", classification="config",
        )
    return amount if amount else None


@dataclass(frozen=True, slots=True)
class DockerHost:
    """What the Docker daemon can hold a container to (``docker info``); *answered* when the daemon said."""

    cpus: int | None = None
    cpu_quota: bool = True
    memory_limit: bool = True
    swap_limit: bool = True
    pids_limit: bool = True
    answered: bool = True


def limit_args(spec: SandboxSpec, *, pids_limit: int, host: DockerHost | None = None) -> list[str]:
    """``docker run`` flags that hold a container to *spec*'s limits.

    ``memory_limit`` caps memory, swap included (``--memory`` = ``--memory-swap``:
    no swap past the limit), so a runaway command is killed by the kernel inside
    the container instead of filling the host.  ``cpu_limit`` becomes ``--cpus``
    (at most the daemon's CPUs, which Docker refuses past; none where the daemon
    has no CPU quota, which Docker refuses too), *pids_limit* ``--pids-limit``.
    A limit of "" or zero (``pids_limit`` 0) sets none.
    """
    host = host or DockerHost()
    args: list[str] = []
    memory = _quantity(spec.memory_limit, "memory limit", "8Gi or 512Mi")
    if memory is not None:
        if memory < _MIN_MEMORY_BYTES:
            # "500m" is half a byte: Docker would read the 0 it rounds to as no limit at all.
            raise SandboxUnavailableError(
                f"The sandbox memory limit {spec.memory_limit!r} is under the 6Mi Docker allows (500Mi, not 500m?)",
                classification="config",
            )
        memory_bytes = str(int(memory))
        args += ["--memory", memory_bytes, "--memory-swap", memory_bytes]
    cpus = _quantity(spec.cpu_limit, "CPU limit", "2 or 500m")
    if cpus is not None:
        if host.cpu_quota:
            if host.cpus:
                cpus = min(cpus, Decimal(host.cpus))
            cpus = max(cpus.quantize(_CPU_STEP), _MIN_CPUS)
            args += ["--cpus", format(cpus.normalize(), "f")]
    if pids_limit > 0:
        args += ["--pids-limit", str(pids_limit)]
    return args


async def docker_host(docker: _DockerDriver) -> DockerHost:
    """The daemon's CPUs and the limits it can apply; unknown (all assumed, the worker's CPUs) when it does not say."""
    code, stdout, stderr = await docker.run(
        ["info", "--format", "{{.NCPU}} {{.CPUCfsQuota}} {{.MemoryLimit}} {{.SwapLimit}} {{.PidsLimit}}"],
    )
    fields = stdout.decode(errors="replace").split() if code == 0 else []
    if len(fields) != 5 or not fields[0].isdigit():
        logger.warning("docker info did not say what limits the daemon applies (%s); assuming all",
                       stderr.decode(errors="replace").strip() or "no answer")
        return DockerHost(cpus=os.cpu_count(), answered=False)
    host = DockerHost(int(fields[0]), *(field == "true" for field in fields[1:]))
    for applies, what in (
        (host.cpu_quota, "CPU"), (host.memory_limit, "memory"), (host.swap_limit, "swap"), (host.pids_limit, "process"),
    ):
        if not applies:
            logger.warning("The Docker daemon cannot apply a %s limit: sandbox containers run without it", what)
    return host


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
        pids_limit: int = DEFAULT_PIDS_LIMIT,
        docker: _DockerDriver | None = None,
        httpx_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._image = image
        self._port_base = executor_port_base
        self._ready_timeout = ready_timeout
        self._network = network
        self._pids_limit = pids_limit
        self._host: DockerHost | None = None
        self._mcp_proxy_url = mcp_proxy_url
        self._storage = storage_settings
        self._docker = docker or _RealDocker()
        self._transport = httpx_transport
        self._client = ExecutorHTTPClient()
        self._entries: dict[str, _Entry] = {}
        self._next_offset = 0
        self._lock = asyncio.Lock()
        #: This worker, on its containers' ``surogates.worker`` label, and who says whether another worker lives
        #: (``claim_as``).  Unset: one worker, whose sweeps of a session take all its containers.
        self._worker: str | None = None
        self._worker_alive: Any = None

    @classmethod
    def from_settings(cls, sandbox: Any, *, mcp_proxy_url: str = "", storage_settings: Any = None) -> DockerSandbox:
        """The backend a worker runs, from its :class:`~surogates.config.SandboxSettings`."""
        return cls(
            image=sandbox.docker_image,
            executor_port_base=sandbox.docker_executor_port_base,
            ready_timeout=sandbox.docker_ready_timeout,
            network=sandbox.docker_network,
            mcp_proxy_url=mcp_proxy_url,
            storage_settings=storage_settings,
            pids_limit=sandbox.docker_pids_limit,
        )

    # ------------------------------------------------------------------
    # Sandbox protocol
    # ------------------------------------------------------------------

    def claim_as(self, worker: str, alive: Any) -> None:
        """Label this worker's containers *worker*; a sweep of a session's containers (at a provision, or a turn's
        end) then spares those of another worker *alive* (an async callable of a worker's label) says lives.

        With several workers, a session's turns, and its sub-agents', run on any of them, each in that worker's
        own container for the session: a sweep by one must never take another's container in mid-task.
        """
        self._worker, self._worker_alive = worker, alive

    async def provision(self, spec: SandboxSpec) -> str:
        if "PROJECT_DIR" in spec.env:
            raise SandboxUnavailableError(
                "A project thread's copy of the project's files needs the Kubernetes sandbox",
                classification="docker",
            )
        sandbox_id = uuid.uuid4().hex
        token = secrets.token_urlsafe(32)
        # Before anything starts: a limit that is no quantity fails the provision, and no container runs unbounded.
        # Asked until the daemon answers: one that is still starting must not pin a guess for the worker's life.
        host = self._host or await docker_host(self._docker)
        if host.answered:
            self._host = host
        limits = limit_args(spec, pids_limit=self._pids_limit, host=host)

        # Reap stale containers left by a previous worker for this root
        # session before claiming a new one: a live worker's are its own.
        if spec.session_id:
            await self.destroy_for_session(spec.session_id)

        mode, detail = self._workspace_mode(spec)
        env = self._build_env(spec, sandbox_id, token, mode, detail)

        # SSH remote-access (dev-only, non-production isolation): start a local
        # ssh-agent on the host and bind-mount its socket into the container.
        ssh_mount_args: list[str] = []
        if spec.ssh_key_material:
            from surogates.sandbox.ssh_agent_local import start_local_ssh_agent

            sock = await start_local_ssh_agent(sandbox_id, spec.ssh_key_material)
            ssh_mount_args = ["-v", f"{os.path.dirname(sock)}:/ssh-agent"]
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

            # Published on the loopback only, where the worker calls it: on every interface, the host's
            # network, and any container through the gateway, could reach the daemon (its token aside).
            args = ["run", "-d", "--rm", "-p", f"{_PUBLISH_HOST}:{host_port}:{_IN_CONTAINER_PORT}"]
            if self._network:
                args += ["--network", self._network]
            args += [
                "--add-host", "host.docker.internal:host-gateway",
                "--label", "app=surogates-sandbox",
            ]
            if spec.session_id:
                args += ["--label", f"surogates.session_id={spec.session_id}"]
            if self._worker:
                args += ["--label", f"surogates.worker={self._worker}"]
            args += limits
            if mode == "s3fs":
                # geesefs needs FUSE inside the container.
                args += [
                    "--cap-add", "SYS_ADMIN",
                    "--device", "/dev/fuse",
                    "--security-opt", "apparmor:unconfined",
                ]
            elif mode == "bind":
                args += ["-v", f"{detail}:/workspace"]
            args += ssh_mount_args
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
            if stderr.strip():
                # A limit the host cannot apply is a warning, and the container runs without it; an image pull's
                # progress is not.
                said = stderr.decode(errors="replace").strip()
                logger.log(logging.WARNING if "WARNING" in said else logging.INFO, "docker run for sandbox %s: %s",
                           sandbox_id, said)
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

    async def execute(self, sandbox_id: str, name: str, input: str, *, timeout: float | None = None) -> str:
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
                timeout=timeout or entry.spec.timeout,
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
        from surogates.sandbox.ssh_agent_local import stop_local_ssh_agent

        await stop_local_ssh_agent(sandbox_id)
        logger.info("Destroyed docker sandbox %s", sandbox_id)

    async def destroy_for_session(self, session_id: str) -> None:
        # Both filters, not just the session: browser containers carry the
        # same surogates.session_id label, and a sweep keyed on the session
        # alone stops the session's live browser at every completion.
        code, stdout, stderr = await self._docker.run(
            [
                "ps",
                "-aq",
                "--filter",
                "label=app=surogates-sandbox",
                "--filter",
                f"label=surogates.session_id={session_id}",
            ],
        )
        if code != 0:
            logger.warning(
                "Failed to list sandbox containers for session %s: %s",
                session_id, stderr.decode(errors="replace"),
            )
            return
        for container_id in stdout.decode().split():
            if not await self._sweepable(container_id):
                # Another worker's, that lives: a turn of the session, or of one of its sub-agents, runs in it.
                continue
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

    async def _sweepable(self, container_id: str) -> bool:
        """Whether a sweep of its session may take *container_id*: this worker's, one of no worker's, or one of
        a worker that is gone.  Unsure, it is spared."""
        if self._worker is None:
            return True
        code, stdout, _ = await self._docker.run(
            ["inspect", "--format", '{{index .Config.Labels "surogates.worker"}}', container_id],
        )
        if code != 0:
            return True  # gone already
        owner = stdout.decode().strip()
        if not owner or owner == "<no value>" or owner == self._worker:
            return True
        try:
            return not await self._worker_alive(owner)
        except Exception:
            logger.warning("Could not tell whether worker %s lives; its container %s is spared", owner, container_id)
            return False

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _build_env(
        self,
        spec: SandboxSpec,
        sandbox_id: str,
        token: str,
        mode: str,
        bucket_spec: str | None,
    ) -> dict[str, str]:
        """Container env: base + per-mode workspace wiring + MCP/KB.

        Mirrors the K8s pod manifest's env block, with host-local URLs
        rewritten so a bridged container can reach host services.
        """
        import os

        env = {
            "TOOL_EXECUTOR_TOKEN": token,
            "WORKSPACE_DIR": "/workspace",
        }
        if mode == "s3fs":
            # /workspace becomes a real FUSE mount, so leave the daemon's
            # FUSE readiness check at its default (require_fuse=1) -- the
            # entrypoint mounts geesefs from these vars before serving.
            s = self._storage
            env["SANDBOX_S3FS_INLINE"] = "1"
            env["S3_BUCKET_PATH"] = bucket_spec or ""
            env["S3_ENDPOINT"] = _rewrite_host_for_container(getattr(s, "endpoint", ""))
            env["S3_REGION"] = getattr(s, "region", "") or "auto"
            env["AWS_ACCESS_KEY_ID"] = getattr(s, "access_key", "")
            env["AWS_SECRET_ACCESS_KEY"] = getattr(s, "secret_key", "")
        else:
            # bind / ephemeral: /workspace is not FUSE, so disable the check.
            env["TOOL_EXECUTOR_REQUIRE_FUSE"] = "0"
        reserved = {
            "TOOL_EXECUTOR_TOKEN", "WORKSPACE_DIR", "TOOL_EXECUTOR_REQUIRE_FUSE",
            "TOOL_EXECUTOR_PORT", "SANDBOX_S3FS_INLINE", "S3_BUCKET_PATH",
            "S3_ENDPOINT", "S3_REGION", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
            # The daemon's own switches: never from a tenant's spec or a skill's passthrough.
            "TOOL_EXECUTOR_ALLOW_LOCAL", "FORWARDED_ALLOW_IPS",
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

        # The worker's own environment stays in the worker. The KB tools
        # that read the ops database and the Hub are HARNESS-located
        # (``surogates.tools.router.TOOL_LOCATIONS``) and run in the worker
        # process, which takes its ops connection from ``settings.ops_db``
        # -- nothing in the container ever read these. Copying them here
        # would hand an untrusted container the ops database URL and the
        # Hub key pair; a sandbox reaches platform data through the MCP
        # proxy, with the scoped token minted above.
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
                is_service_account=spec.env.get("SUROGATES_IS_SERVICE_ACCOUNT") == "1",
                simulated=spec.simulated,
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
