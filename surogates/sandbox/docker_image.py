"""The Docker backend's sandboxes in a session's own image (session config ``sandbox_image``).

The image is the task's (a repository with its toolchain); the tool executor is
not in it.  The executor bundle image (``images/executor-bundle``) is mounted
read-only at ``/opt/surogates`` (``--mount type=image``, Docker 28 or later)
and its tini and Python run the executor daemon in place of the image's
entrypoint, as the image's own user, in the session's folder of the image,
with the image's environment for the commands.

There the container IS the session's workspace, with nothing in storage
behind it, so:

- it is one container per session, whichever worker asks for it: named after
  the session, a worker that finds it takes it over (its port from ``docker
  inspect``, its token derived from the deployment's signing secret and the
  session) instead of starting its own; one that stopped (its executor killed,
  the daemon restarted, a Stop) is started again, its files as they were;
- nothing of a worker's own ends it: not a turn's end, a Stop or pause (they
  stop it), a worker's shutdown or another worker's view of it.  It is removed
  only by :meth:`ImageSessions.reap_images`, on any worker: once its session
  and the agents below it are at rest and the container itself says it went
  unused for ``image_idle_seconds`` (its executor retires it, ``/retire``,
  atomically with its calls; or since it stopped), or when it can never start;
- its label is ``app=surogates-image-sandbox``, not the agent-sandbox's: no
  sweep of a session's sandbox containers ever takes it.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

from surogates.sandbox.base import SandboxSpec, SandboxUnavailableError
from surogates.sandbox.session_image import (
    BUNDLE_MOUNT,
    BUNDLE_PYTHON,
    BUNDLE_TINI,
    valid_reference,
    valid_workdir,
)

logger = logging.getLogger(__name__)

#: The first Docker Engine API with image mounts (``--mount type=image``, Docker 28.0), for the CLI and the daemon.
IMAGE_MOUNT_API = (1, 48)
#: An environment variable's name, as an image declares one.
_ENV_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
#: Lines of the container's log quoted when its executor never became ready.
_LOG_TAIL_LINES = 20
#: Docker's names of the architectures its version and an image's inspect give.
_ARCHES = {"x86_64": "amd64", "aarch64": "arm64"}
#: What the executor reads to know it runs in a session's own image (``surogates.tools.workspace_io.local``).
IMAGE_SESSION_ENV = ("SUROGATES_IMAGE_SESSION", "SUROGATES_EXECUTOR_HOME", "SUROGATES_IMAGE_ENV_KEYS",
                     "SUROGATES_IMAGE_ENV_SHADOWED", "SUROGATES_IMAGE_ENV_UNSET")
#: The label of a session image's container (``app``): never the agent-sandbox's, so no session sweep takes it.
IMAGE_APP = "surogates-image-sandbox"
#: Names the executor and the harness's tools read for their own control (the daemon's port, workspace and history,
#: the harness's own switches): an image that declares one gets it held for the daemon ("" or the harness's value),
#: and its own value back in the commands' environment.
_CONTROL_PREFIXES = ("SUROGATES_", "TOOL_EXECUTOR_", "TERMINAL_", "HISTORY_", "MCP_PROXY_", "SANDBOX_", "S3_",
                     "GEESEFS_")
_CONTROL_NAMES = frozenset({"PROJECT_DIR", "WORKSPACE_DIR", "USER_ID", "ORG_ID"})
#: Capabilities a task never needs, of Docker's default set: raw packets on a shared bridge, device nodes, file caps.
_DROPPED_CAPS = ("NET_RAW", "MKNOD", "SETFCAP")
#: How long a failed pull answers the sessions that waited on it, without a pull of their own.
_FAILED_PULL_HOLD_S = 60.0
#: A log that says the bundle's programs could not run in the image (no glibc, another architecture).
#: A start that can never succeed (the image or the bundle gone, the binary unrunnable): the container is removed.
_NEVER_STARTS_RE = re.compile(r"(no such image|exec format error|executable file not found|no such file or directory)",
                              re.IGNORECASE)
#: Docker's refusal of a second container under a name in use.
_NAME_TAKEN_RE = re.compile(r"Conflict\. The container name .* is already in use", re.IGNORECASE)
#: How long a worker waits for the session's container another worker is starting.
_TAKE_OVER_S = 30.0
_CANNOT_RUN_RE = re.compile(r"/opt/surogates/bin/(python3|tini)\S*.*(no such file|not found|exec format error)",
                            re.IGNORECASE)


_IN_PORT = 8071


def _stopped_for(finished: str, seconds: float) -> bool:
    """Whether a container that stopped at *finished* (Docker's RFC 3339 time) has been stopped *seconds*."""
    from datetime import datetime, timezone

    text = finished.strip().removesuffix("Z")
    head, _, fraction = text.partition(".")
    try:
        when = datetime.fromisoformat(head + (f".{fraction[:6]}" if fraction else "")).replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    if when.year < 2000:  # never ran: "0001-01-01T00:00:00Z"
        return True
    return (datetime.now(timezone.utc) - when).total_seconds() >= seconds


@dataclass(frozen=True, slots=True)
class ImageRun:
    """How a session's own image runs: its folder, the environment it declares, and what ``docker run`` adds."""

    image: str
    workdir: str
    env: tuple[tuple[str, str], ...]
    run_args: tuple[str, ...]
    command: tuple[str, ...]


def _secret() -> bytes:
    from surogates.tenant.auth.jwt import signing_secret

    try:
        return signing_secret().encode()
    except RuntimeError:
        raise SandboxUnavailableError(
            "A session's own image needs the runtime's signing secret (SUROGATES_JWT_SECRET)", classification="config",
        ) from None


def session_token(session_id: str) -> str:
    """The executor token of a session's own-image container: derived from the deployment's signing secret and the
    session, so every worker of the deployment can call it without reading it back from the container."""
    import hashlib
    import hmac

    digest = hmac.new(_secret(), f"surogates-image-sandbox\0{session_id}".encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def deployment_id() -> str:
    """This deployment's mark on its session-image containers (from its signing secret, which its workers share):
    a reaper takes only its own deployment's, whatever network they are on."""
    import hashlib

    return hashlib.sha256(b"surogates-deployment\0" + _secret()).hexdigest()[:16]


def container_name(spec: SandboxSpec, sandbox_id: str) -> str:
    """The one container of a session's own image: named after the session, so no worker starts a second."""
    return f"surogates-sbx-{spec.session_id or sandbox_id}"


def is_control(name: str) -> bool:
    return name in _CONTROL_NAMES or name.startswith(_CONTROL_PREFIXES)


def image_env(env: dict[str, str], declared: tuple[tuple[str, str], ...], workdir: str) -> dict[str, str]:
    """The container environment of a session's own image, from the backend's own *env*.

    The image's declared names reach the commands (``SUROGATES_IMAGE_ENV_KEYS``);
    those the harness sets, or reads for its own control, are held for the
    daemon and their image values handed to the commands
    (``SUROGATES_IMAGE_ENV_SHADOWED``).
    """
    env = dict(env)
    env["WORKSPACE_DIR"] = workdir
    env["TOOL_EXECUTOR_PORT"] = "8071"
    env["TOOL_EXECUTOR_REQUIRE_FUSE"] = "0"
    keys, shadowed, unset = [], {}, []
    for name, value in declared:
        if name in IMAGE_SESSION_ENV or name in env or is_control(name):
            shadowed[name] = value
            if name not in env and name not in IMAGE_SESSION_ENV:
                # The image's value leaves the daemon's environment before it reads it (surogates.image_entry).
                unset.append(name)
        else:
            keys.append(name)
    env["SUROGATES_IMAGE_ENV_UNSET"] = ",".join(unset)
    env["SUROGATES_IMAGE_SESSION"] = "1"
    env["SUROGATES_EXECUTOR_HOME"] = BUNDLE_MOUNT
    env["SUROGATES_IMAGE_ENV_KEYS"] = ",".join(keys)
    env["SUROGATES_IMAGE_ENV_SHADOWED"] = base64.b64encode(json.dumps(shadowed).encode()).decode()
    return env


class ImageSessions:
    """The Docker backend's part for sessions in their own image (mixed into ``DockerSandbox``)."""

    _docker: Any
    _entries: dict
    _lock: asyncio.Lock

    def _init_image_sessions(self, *, bundle_image: str, pull: str, pull_timeout: float) -> None:
        self._bundle_image = bundle_image
        self._image_pull = pull
        self._image_pull_timeout = pull_timeout
        self._pulls: dict[str, asyncio.Lock] = {}
        self._failed_pulls: dict[str, tuple[float, str]] = {}
        #: The CLI's and the daemon's API versions and the daemon's architecture, once Docker said them.
        self._versions: tuple[tuple[int, int], tuple[int, int], str] | None = None

    # -- what the host has ------------------------------------------------------------------------------------------

    async def _versions_of_docker(self) -> tuple[tuple[int, int], tuple[int, int], str]:
        if self._versions is not None:
            return self._versions
        code, stdout, stderr = await self._docker.run(
            ["version", "--format", "{{.Client.APIVersion}} {{.Server.APIVersion}} {{.Server.Arch}}"],
        )
        fields = stdout.decode(errors="replace").split() if code == 0 else []
        try:
            client, server = (tuple(int(p) for p in f.split(".")[:2]) for f in fields[:2])
            arch = fields[2]
        except (IndexError, ValueError):
            raise SandboxUnavailableError(
                f"docker version did not say its API versions: {stderr.decode(errors='replace').strip()}",
                classification="docker",
            ) from None
        self._versions = (client, server, _ARCHES.get(arch, arch))
        return self._versions

    async def _inspect_image(self, ref: str) -> dict | None:
        code, stdout, _ = await self._docker.run(["image", "inspect", ref])
        if code != 0:
            return None
        try:
            found = json.loads(stdout.decode(errors="replace") or "[]")
        except ValueError:
            return None
        return found[0] if isinstance(found, list) and found and isinstance(found[0], dict) else None

    async def _ensure_image(self, ref: str, what: str) -> dict:
        """*ref*'s ``docker image inspect``, pulled first where it is missing and pulls are on.  One pull of an
        image at a time; the sessions that waited on a pull that failed get its error, not a pull each."""
        found = await self._inspect_image(ref)
        if found is not None:
            return found
        if self._image_pull != "missing":
            raise SandboxUnavailableError(
                f"The {what} {ref} is not on this host, and this deployment pulls none (sandbox.docker_image_pull)",
                classification="config",
            )
        async with self._pulls.setdefault(ref, asyncio.Lock()):
            found = await self._inspect_image(ref)  # another session's pull may have brought it meanwhile
            if found is not None:
                return found
            failed = self._failed_pulls.get(ref)
            if failed is not None and time.monotonic() - failed[0] < _FAILED_PULL_HOLD_S:
                raise SandboxUnavailableError(failed[1], classification="docker")
            logger.info("Pulling the %s %s", what, ref)
            try:
                code, _stdout, stderr = await asyncio.wait_for(
                    self._docker.run(["pull", "--quiet", ref]), timeout=self._image_pull_timeout,
                )
                said = "" if code == 0 else f"docker pull {ref} failed (exit {code}): {stderr.decode(errors='replace').strip()}"
            except TimeoutError:
                said = f"Pulling the {what} {ref} took over {self._image_pull_timeout:g}s"
            if said:
                self._failed_pulls[ref] = (time.monotonic(), said)
                raise SandboxUnavailableError(said, classification="docker")
            self._failed_pulls.pop(ref, None)
            found = await self._inspect_image(ref)
        if found is None:
            raise SandboxUnavailableError(f"The {what} {ref} is not on this host after its pull", classification="docker")
        return found

    # -- how the image runs ------------------------------------------------------------------------------------------

    async def _image_run(self, spec: SandboxSpec) -> ImageRun:
        image = spec.session_image
        if not self._bundle_image:
            raise SandboxUnavailableError(
                "A session's own image needs the executor bundle (sandbox.executor_bundle_image is not set)",
                classification="config",
            )
        for ref in (image, self._bundle_image):
            if not valid_reference(ref):
                # session_image_of checked the session's; this is the last door before docker's argument list.
                raise SandboxUnavailableError(f"Not an image reference: {ref}", classification="config")
        if spec.image_workdir and not valid_workdir(spec.image_workdir):
            raise SandboxUnavailableError(f"Not a workspace folder: {spec.image_workdir}", classification="config")
        client, server, arch = await self._versions_of_docker()
        if min(client, server) < IMAGE_MOUNT_API:
            raise SandboxUnavailableError(
                "A session's own image needs Docker 28 or later, client and daemon (image mounts); this host's "
                f"client API is {client[0]}.{client[1]}, its daemon's {server[0]}.{server[1]}",
                classification="config",
            )
        await self._ensure_image(self._bundle_image, "executor bundle")
        found = await self._ensure_image(image, "session image")
        config = found.get("Config") or {}
        if (found.get("Os") or "linux") != "linux" or (found.get("Architecture") or arch) != arch:
            raise SandboxUnavailableError(
                f"The image {image} is for {found.get('Os')}/{found.get('Architecture')}; this host runs linux/{arch}",
                classification="config",
            )
        workdir = spec.image_workdir or str(config.get("WorkingDir") or "")
        if not valid_workdir(workdir):
            raise SandboxUnavailableError(
                f"The image {image} names no working folder the session can work in ({workdir or 'none'}): "
                "name one with sandbox_workdir",
                classification="config",
            )
        declared: dict[str, str] = {}
        for item in config.get("Env") or []:
            name, _, value = str(item).partition("=")
            if _ENV_KEY_RE.fullmatch(name):
                declared[name] = value
        run_args = (
            "--name", "",  # filled per provision: container_name()
            "--label", "surogates.sandbox_image=1",
            "--label", f"surogates.executor_bundle={self._bundle_image}",
            "--mount", f"type=image,source={self._bundle_image},target={BUNDLE_MOUNT},"
                       f"image-subpath={BUNDLE_MOUNT.lstrip('/')}",
            # The image's own user runs the tools; nothing in it gains privileges (setuid files), sends raw packets
            # on the sandbox network, makes devices, or runs the image's own health check.
            "--security-opt", "no-new-privileges",
            *(arg for cap in _DROPPED_CAPS for arg in ("--cap-drop", cap)),
            "--no-healthcheck",
            "--workdir", workdir,
            "--entrypoint", BUNDLE_TINI,
        )
        # -I: the image's PYTHONPATH / PYTHONHOME, and a module in the workspace, never reach the daemon;
        # surogates.image_entry clears the image's names for the executor's controls before it starts.
        command = ("--", BUNDLE_PYTHON, "-I", "-m", "surogates.image_entry")
        return ImageRun(image=image, workdir=workdir, env=tuple(declared.items()), run_args=run_args, command=command)

    # -- one container per session ----------------------------------------------------------------------------------

    async def _session_containers(self, name: str) -> list[tuple[str, str]]:
        """(id, state) of the container named *name*, if there is one."""
        code, stdout, _ = await self._docker.run(
            ["ps", "-a", "--filter", f"name=^{name}$", "--format", "{{.ID}} {{.State}}"],
        )
        if code != 0:
            return []
        out = []
        for line in stdout.decode(errors="replace").splitlines():
            cid, _, state = line.partition(" ")
            if cid:
                out.append((cid, state.strip()))
        return out

    async def _adopt(self, container_id: str) -> tuple[int, str] | None:
        """The host port of a RUNNING container of the session (Docker picks it at each start) and its executor's
        token (derived from the session, ``session_token``), or ``None``."""
        code, stdout, _ = await self._docker.run(
            ["inspect", "--format",
             '{{json .NetworkSettings.Ports}}\n{{index .Config.Labels "surogates.session_id"}}', container_id],
        )
        if code != 0:
            return None
        try:
            # One JSON document a line: JSON never holds a raw newline.
            ports_json, session = stdout.decode(errors="replace").strip().split("\n", 1)
            bindings = (json.loads(ports_json) or {}).get(f"{_IN_PORT}/tcp") or []
            port = int(next(b["HostPort"] for b in bindings if b.get("HostIp") in ("127.0.0.1", "", None)))
        except (ValueError, KeyError, TypeError, StopIteration):
            return None
        return port, session_token(session.strip())

    async def _remove(self, container_id: str) -> None:
        # -v: the anonymous volumes of the image's VOLUME lines go with it.
        await self._docker.run(["rm", "-f", "-v", container_id])

    async def _provision_image(self, spec: SandboxSpec, sandbox_id: str, token: str, limits: list[str]) -> str:
        from surogates.sandbox.docker import _IN_CONTAINER_PORT, _PUBLISH_HOST

        if spec.ssh_key_material:
            raise SandboxUnavailableError("SSH access is not offered in a session's own image", classification="config")
        if not spec.session_id:
            raise SandboxUnavailableError("A session's own image needs its session", classification="config")
        run = await self._image_run(spec)
        deployment = deployment_id()
        name = container_name(spec, sandbox_id)
        for cid, state in await self._session_containers(name):
            if state in ("running", "created", "restarting"):
                # The session's, running or being started by another worker this moment: taken over, never removed.
                return await self._take_over(spec, sandbox_id, name)
            # Stopped (its executor killed, the daemon restarted, a Stop): started again, its files as they were.
            code, _out, err = await self._docker.run(["start", cid])
            if code == 0:
                logger.info("Started the stopped container %s of session %s again", cid, spec.session_id)
                return await self._take_over(spec, sandbox_id, name)
            said = err.decode(errors="replace").strip()
            if not _NEVER_STARTS_RE.search(said):
                # Its files are the session's work: kept, and the call fails; the reaper removes it once at rest.
                raise SandboxUnavailableError(
                    f"The session's stopped container {cid} did not start again: {said[:300]}", classification="docker",
                )
            logger.warning("The container %s of session %s can never start again (%s): removed, a new one starts",
                           cid, spec.session_id, said[:200])
            await self._remove(cid)

        # The executor's token is derived from the session, so any worker of the deployment can call it (take-over)
        # without reading it back from the container.
        token = session_token(spec.session_id)
        env = image_env(self._build_env(spec, sandbox_id, token, "ephemeral", None), run.env, run.workdir)
        run_args = list(run.run_args)
        run_args[run_args.index("--name") + 1] = name
        # Docker picks the host port, at every start: a fixed one could be taken while the container is stopped.
        args = ["run", "-d", "-p", f"{_PUBLISH_HOST}::{_IN_CONTAINER_PORT}"]
        if self._network:
            args += ["--network", self._network]
        args += ["--add-host", "host.docker.internal:host-gateway", "--label", f"app={IMAGE_APP}",
                 "--label", f"surogates.deployment={deployment}", "--label", f"surogates.session_id={spec.session_id}"]
        if self._worker:
            args += ["--label", f"surogates.worker={self._worker}"]
        args += limits + run_args
        for key, value in env.items():
            args += ["-e", f"{key}={value}"]
        args += [run.image, *run.command]
        code, stdout, stderr = await self._docker.run(args)
        if code != 0:
            said = stderr.decode(errors="replace")
            if _NAME_TAKEN_RE.search(said):
                # Another worker started the session's container just now: it is the session's, taken over.
                return await self._take_over(spec, sandbox_id, name)
            # A run that failed at its start leaves its container created under the name, which was free: ours.
            await self._remove(name)
            raise SandboxUnavailableError(f"docker run failed (exit {code}): {said}", classification="docker")
        container_id = stdout.decode().strip().splitlines()[0]
        if stderr.strip():
            said = stderr.decode(errors="replace").strip()
            logger.log(logging.WARNING if "WARNING" in said else logging.INFO,
                       "docker run for sandbox %s: %s", sandbox_id, said)

        try:
            found = await self._adopt(container_id)
            if found is None:
                raise SandboxUnavailableError("Docker gave the new container no port", classification="docker")
            host_port = found[0]
            # An executor that cannot start ends its container at once: told then, not at the ready timeout.
            await self._wait_ready(host_port, container_id)
        except BaseException as exc:
            said = ""
            if isinstance(exc, Exception):
                said = await self._log_tail(container_id)
            # Never left running, a cancelled provision included: it never held any work.
            await asyncio.shield(self._remove(container_id))
            if said:
                hint = (" (the image has no glibc 2.28 or later, such as Alpine, or is of another architecture)"
                        if _CANNOT_RUN_RE.search(said) else "")
                raise SandboxUnavailableError(
                    f"{exc}. The executor in {run.image} said: {said}{hint}", classification="readiness",
                ) from exc
            raise

        self._register(spec, sandbox_id, container_id, host_port, token, created=True)
        logger.info("Provisioned docker sandbox %s in the image %s (container %s, port %d)",
                    sandbox_id, run.image, container_id, host_port)
        return sandbox_id

    def _register(self, spec: SandboxSpec, sandbox_id: str, cid: str, host_port: int, token: str, *, created: bool) -> None:
        from surogates.sandbox.docker import _Entry

        self._entries[sandbox_id] = _Entry(sandbox_id, cid, host_port, token, spec, image=True, created=created)

    async def _take_over(self, spec: SandboxSpec, sandbox_id: str, name: str) -> str:
        """The session's container, running or starting: taken over once its executor answers (``_TAKE_OVER_S`` at
        most for it to run).  One that runs and never answers is stopped, its files kept, and the call fails."""
        deadline = time.monotonic() + _TAKE_OVER_S
        while time.monotonic() < deadline:
            for cid, state in await self._session_containers(name):
                if state == "running" and (found := await self._adopt(cid)) is not None:
                    host_port, token = found
                    try:
                        await self._wait_ready(host_port, cid)
                    except Exception as exc:
                        await asyncio.shield(self._docker.run(["stop", cid]))
                        raise SandboxUnavailableError(
                            f"The session's container {cid} does not answer ({exc}): stopped, its files kept; the "
                            "next call starts it again", classification="readiness",
                        ) from exc
                    self._register(spec, sandbox_id, cid, host_port, token, created=False)
                    logger.info("Took over the container %s of session %s as sandbox %s (port %d)",
                                cid, spec.session_id, sandbox_id, host_port)
                    return sandbox_id
            await asyncio.sleep(0.5)
        raise SandboxUnavailableError(
            f"The session's container {name} never came to run", classification="docker",
        )

    def forget(self, sandbox_id: str) -> None:
        """Drop the entry of a sandbox whose container no longer runs, without a Docker call (the pool's cleanup)."""
        self._entries.pop(sandbox_id, None)

    def created(self, sandbox_id: str) -> bool:
        """Whether *sandbox_id* is a container this provision made new from the image (not one taken over or
        started again): a session that worked in its container before has none of that work there."""
        entry = self._entries.get(sandbox_id)
        return bool(entry is not None and getattr(entry, "image", False) and getattr(entry, "created", False))

    async def stop_image_sandbox(self, entry: Any) -> None:
        """A Stop, a pause, a dead executor's replacement: the container stops, its files kept for the session's
        next call; the reaper removes it once the session has been at rest long enough."""
        await self._docker.run(["stop", entry.container_id])
        logger.info("Stopped the container %s of session %s (kept)", entry.container_id, entry.spec.session_id)

    async def stop_image_of_session(self, session_id: str) -> None:
        """A Stop or ``/clear`` of a session in its own image on a worker that holds no entry for its container: the
        container stops all the same (its runaway commands with it), its files kept."""
        await self._docker.run(["stop", container_name(SandboxSpec(session_id=session_id), session_id)])

    async def _executor(self, cid: str, method: str, path: str, **kw: Any) -> dict | None:
        """A call to a container's executor (``/activity``, ``/retire``); ``None`` when unsure."""
        found = await self._adopt(cid)
        if found is None:
            return None
        host_port, token = found
        import httpx

        try:
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{host_port}", transport=self._transport,
                                         timeout=5.0) as client:
                r = await client.request(method, path, headers={"Authorization": f"Bearer {token}"}, **kw)
            answer = r.json() if r.status_code == 200 else None
        except Exception:  # noqa: BLE001 - unsure: the caller keeps the container
            return None
        return answer if isinstance(answer, dict) else None

    async def reap_images(self, at_rest: Any, *, idle_seconds: float) -> list[tuple[str, str]]:
        """Remove this deployment's session-image containers whose session (and the agents below it) is at rest
        and that went unused for *idle_seconds* by their own clock: the executor retires a running one only if no
        call runs and none is waited on and it went unused that long (``/retire``, atomic with its calls), and a
        stopped one counts from its stop.  Unsure: kept.  (session id, container id) of each one removed."""
        if not self._bundle_image:
            return []  # no session image can run here
        code, stdout, _ = await self._docker.run(
            ["ps", "-a", "--filter", f"label=app={IMAGE_APP}", "--filter", f"label=surogates.deployment={deployment_id()}",
             "--format", "{{.ID}} {{.State}}"],
        )
        if code != 0:
            return []
        reaped: list[tuple[str, str]] = []
        for line in stdout.decode(errors="replace").splitlines():
            cid, _, state = line.partition(" ")
            if not cid:
                continue
            try:
                code, out, _ = await self._docker.run(
                    ["inspect", "--format", '{{index .Config.Labels "surogates.session_id"}} {{.State.FinishedAt}}', cid],
                )
                if code != 0:
                    continue
                session, _, finished = out.decode(errors="replace").strip().partition(" ")
                if not session or not await at_rest(session):
                    continue
                if state.strip() == "running":
                    retired = await self._executor(cid, "POST", "/retire", json={"min_idle_seconds": idle_seconds})
                    if not retired or not retired.get("retired"):
                        continue
                elif not _stopped_for(finished, idle_seconds):
                    continue
                await self._remove(cid)
                for sid, entry in list(self._entries.items()):
                    if entry.container_id == cid:
                        self._entries.pop(sid, None)
                logger.info("Reaped the container %s of session %s at rest", cid, session)
                reaped.append((session, cid))
            except Exception:  # noqa: BLE001 - one container's trouble never ends the round
                logger.warning("Could not reap the session-image container %s", cid, exc_info=True)
        return reaped

    async def _running(self, container_id: str) -> bool:
        """Whether *container_id* still runs (unsure: it does; gone: it does not)."""
        code, stdout, stderr = await self._docker.run(["inspect", "--format", "{{.State.Running}}", container_id])
        if code != 0:
            return b"No such" not in stderr
        return stdout.decode(errors="replace").strip() != "false"

    async def _log_tail(self, container_id: str) -> str:
        """The last lines a container wrote, on one line, or "" when Docker has none."""
        code, stdout, stderr = await self._docker.run(["logs", "--tail", str(_LOG_TAIL_LINES), container_id])
        if code != 0:
            return ""
        said = (stdout + b"\n" + stderr).decode(errors="replace")
        return " | ".join(line.strip() for line in said.splitlines() if line.strip())[-2000:]
