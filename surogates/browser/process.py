"""Local-development browser backend using kernel-images via docker."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx

from surogates.browser.base import (
    BrowserEndpoint,
    BrowserSpec,
    BrowserStatus,
    BrowserUnavailableError,
)

logger = logging.getLogger(__name__)

_PORT_CONFLICT_RE = re.compile(
    r"(port is already allocated|Bind for .* failed)",
    re.IGNORECASE,
)
_MAX_PORT_ATTEMPTS = 25


class _DockerDriver(Protocol):
    async def run(self, args: list[str]) -> tuple[int, bytes, bytes]: ...


class _RealDocker:
    async def run(self, args: list[str]) -> tuple[int, bytes, bytes]:
        proc = await asyncio.create_subprocess_exec(
            "docker",
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        return proc.returncode or 0, stdout, stderr


@dataclass(slots=True)
class _Entry:
    container_id: str
    endpoint: BrowserEndpoint
    rest_port: int
    cdp_port: int
    live_view_port: int


class ProcessBrowserBackend:
    """Runs one kernel-images Docker container per browser session."""

    def __init__(
        self,
        *,
        image: str,
        rest_port_base: int,
        cdp_port_base: int,
        live_view_port_base: int,
        docker: _DockerDriver | None = None,
        httpx_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._image = image
        self._rest_port_base = rest_port_base
        self._cdp_port_base = cdp_port_base
        self._live_view_port_base = live_view_port_base
        self._docker = docker or _RealDocker()
        self._transport = httpx_transport
        self._entries: dict[str, _Entry] = {}
        self._next_offset = 0
        self._lock = asyncio.Lock()

    async def provision(
        self,
        spec: BrowserSpec,
        *,
        session_id: str = "",
        org_id: str = "",
        user_id: str = "",
    ) -> tuple[str, BrowserEndpoint]:
        _ = (org_id, user_id)
        workspace = self._mountable_workspace(spec.workspace_path)
        reserved_env = {"HOME", "WORKSPACE_DIR"} if workspace is not None else set()
        image = spec.image or self._image

        for _attempt in range(_MAX_PORT_ATTEMPTS):
            async with self._lock:
                offset = self._next_offset
                self._next_offset += 1

            rest_port = self._rest_port_base + offset
            cdp_port = self._cdp_port_base + offset
            live_view_port = self._live_view_port_base + offset

            args = [
                "run",
                "-d",
                "--rm",
                "-p",
                f"{rest_port}:10001",
                "-p",
                f"{cdp_port}:9222",
                "-p",
                f"{live_view_port}:8080",
                "--shm-size",
                "2g",
                "--label",
                "app=surogates-browser",
                "--label",
                f"surogates.session_id={session_id}",
            ]
            if workspace is not None:
                args.extend(["-v", f"{workspace}:/workspace"])
                args.extend(["-e", "WORKSPACE_DIR=/workspace"])
                args.extend(["-e", "HOME=/workspace"])
            for key, value in spec.env.items():
                if key in reserved_env:
                    continue
                args.extend(["-e", f"{key}={value}"])
            args.append(image)

            code, stdout, stderr = await self._docker.run(args)
            if code != 0:
                stderr_text = stderr.decode(errors="replace")
                if _PORT_CONFLICT_RE.search(stderr_text):
                    logger.warning(
                        "Browser port offset %d unavailable; trying next offset",
                        offset,
                    )
                    continue
                raise BrowserUnavailableError(
                    f"docker run failed (exit {code}): {stderr_text}",
                    classification="docker",
                )
            break
        else:
            raise BrowserUnavailableError(
                "docker run failed: no free browser ports found",
                classification="docker",
            )

        container_id = stdout.decode().strip().splitlines()[0]
        endpoint = BrowserEndpoint(
            rest_url=f"http://127.0.0.1:{rest_port}",
            cdp_url=f"ws://127.0.0.1:{cdp_port}",
            live_view_url=f"ws://127.0.0.1:{live_view_port}",
        )

        try:
            await self._wait_ready(endpoint, spec.pod_ready_timeout)
        except Exception:
            await self._docker.run(["stop", container_id])
            await self._docker.run(["rm", container_id])
            raise

        self._entries[container_id] = _Entry(
            container_id=container_id,
            endpoint=endpoint,
            rest_port=rest_port,
            cdp_port=cdp_port,
            live_view_port=live_view_port,
        )
        logger.info(
            "Provisioned browser container %s on REST port %s", container_id, rest_port
        )
        return container_id, endpoint

    def _mountable_workspace(self, workspace_path: str | None) -> Path | None:
        if not workspace_path:
            return None
        workspace = Path(workspace_path).resolve()
        try:
            workspace.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning(
                "Skipping browser workspace bind mount for %s: %s",
                workspace,
                exc,
            )
            return None
        if not workspace.is_dir():
            logger.warning(
                "Skipping browser workspace bind mount for %s: not a directory",
                workspace,
            )
            return None
        return workspace

    async def status(self, browser_id: str) -> BrowserStatus:
        if browser_id not in self._entries:
            return BrowserStatus.TERMINATED
        code, stdout, _stderr = await self._docker.run(
            ["inspect", "--format", "{{.State.Status}}", browser_id]
        )
        if code != 0:
            return BrowserStatus.FAILED
        status = stdout.decode().strip()
        if status == "running":
            return BrowserStatus.RUNNING
        if status in {"created", "restarting"}:
            return BrowserStatus.PENDING
        if status in {"exited", "dead", "removing"}:
            return BrowserStatus.TERMINATED
        return BrowserStatus.FAILED

    async def destroy(self, browser_id: str) -> None:
        if browser_id not in self._entries:
            return
        await self._docker.run(["stop", browser_id])
        await self._docker.run(["rm", browser_id])
        del self._entries[browser_id]
        logger.info("Destroyed browser container %s", browser_id)

    async def destroy_for_session(self, session_id: str) -> None:
        code, stdout, stderr = await self._docker.run(
            [
                "ps",
                "-aq",
                "--filter",
                f"label=surogates.session_id={session_id}",
            ]
        )
        if code != 0:
            logger.warning(
                "Failed to list browser containers for session %s: %s",
                session_id,
                stderr.decode(errors="replace"),
            )
            return

        for container_id in stdout.decode().split():
            if container_id in self._entries:
                await self.destroy(container_id)
                continue
            await self._docker.run(["stop", container_id])
            await self._docker.run(["rm", container_id])
            logger.info(
                "Destroyed uncached browser container %s for session %s",
                container_id,
                session_id,
            )

    async def _wait_ready(self, endpoint: BrowserEndpoint, timeout: int) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        last_error: Exception | None = None
        async with httpx.AsyncClient(
            base_url=endpoint.rest_url,
            transport=self._transport,
            timeout=2.0,
        ) as client:
            while asyncio.get_running_loop().time() < deadline:
                try:
                    response = await client.get("/spec.json")
                    if response.status_code == 200:
                        return
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
                await asyncio.sleep(0.5)
        detail = type(last_error).__name__ if last_error is not None else "no_response"
        raise BrowserUnavailableError(
            f"Browser did not become ready within {timeout}s ({detail})",
            classification="readiness",
        )
