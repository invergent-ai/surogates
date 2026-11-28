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
import secrets
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
from surogates.image_entry import is_control  # noqa: E402  (the one list, also what the entry clears)
#: Capabilities a task never needs, of Docker's default set: raw packets on a shared bridge, device nodes, file caps.
_DROPPED_CAPS = ("NET_RAW", "MKNOD", "SETFCAP")
#: How long a failed pull answers the sessions that waited on it, without a pull of their own.
_FAILED_PULL_HOLD_S = 60.0
#: Docker's refusal of a second container under a name in use.
_NAME_TAKEN_RE = re.compile(r"Conflict\. The container name .* is already in use", re.IGNORECASE)
#: Rounds at rest a running container's executor may leave unanswered before the reaper stops it.
_UNANSWERED_ROUNDS = 5
#: How long a worker waits for the session's container another worker is starting.
_TAKE_OVER_S = 30.0
# How long a worker whose call met a retired executor waits for the reaper to remove its container.
_RETIRED_WAIT_S = 20.0
#: A log that says the bundle's programs could not run in the image (no glibc, another architecture).
_CANNOT_RUN_RE = re.compile(r"/opt/surogates/bin/(python3|tini)\S*.*(no such file|not found|exec format error)|"
                            r"Error relocating /opt/surogates/", re.IGNORECASE)
#: What the main (glibc) bundle's start says in a musl image: no glibc loader, or gcompat's half of one.
_MUSL_RE = re.compile(r"exec /opt/surogates/bin/python3 failed: No such file or directory|"
                      r"Error relocating /opt/surogates/.*symbol not found", re.IGNORECASE)




class _NeedsMusl(SandboxUnavailableError):
    """The main bundle could not start in the image: its C library is musl."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason, classification="readiness")


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
    #: What ``docker run`` runs: the inspected image's id (its name when Docker gave none).
    image_id: str = ""


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


def deployment_id(configured: str) -> str:
    """This deployment's mark on its session-image containers (``sandbox.image_deployment``): a reaper takes only
    its own deployment's, whatever network they are on.  Required: two stacks on one Docker host must never take
    each other's."""
    if not configured or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}", configured):
        raise SandboxUnavailableError(
            "A session's own image needs this deployment's mark (sandbox.image_deployment: letters, digits, . _ -)",
            classification="config",
        )
    return configured


def _is_kill(input: str) -> bool:
    try:
        args = json.loads(input or "{}")
    except ValueError:
        return False
    return isinstance(args, dict) and args.get("action") == "kill"


def container_name(spec: SandboxSpec, sandbox_id: str) -> str:
    """The one container of a session's own image: named after the session, so no worker starts a second."""
    return f"surogates-sbx-{spec.session_id or sandbox_id}"


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

    def _init_image_sessions(self, *, bundle_image: str, pull: str, pull_timeout: float, deployment: str = "",
                             bundle_image_musl: str = "") -> None:
        self._bundle_image = bundle_image
        self._bundle_image_musl = bundle_image_musl
        #: Images (by id) the main bundle could not start in: they get the musl bundle from then on.
        self._musl_images: set[str] = set()
        self._deployment = deployment
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
        for ref in (image, self._bundle_image, *([self._bundle_image_musl] if self._bundle_image_musl else [])):
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
            "--label", f"surogates.session_image={image}",
            # The image's own user runs the tools; nothing in it gains privileges (setuid files), sends raw packets
            # on the sandbox network, makes devices, or runs the image's own health check.
            "--security-opt", "no-new-privileges",
            *(arg for cap in _DROPPED_CAPS for arg in ("--cap-drop", cap)),
            "--no-healthcheck",
            # The entrypoint is ours: a Stop sends it SIGTERM whatever STOPSIGNAL the image names.
            "--stop-signal", "SIGTERM",
            # The image inspected above runs (by its id: its environment is the one cleared for the daemon), never
            # one a tag points at by then.
            "--pull", "never",
            "--workdir", workdir,
            "--entrypoint", BUNDLE_TINI,
        )
        # -I: the image's PYTHONPATH / PYTHONHOME, and a module in the workspace, never reach the daemon;
        # surogates.image_entry clears the image's names for the executor's controls before it starts.
        command = ("--", BUNDLE_PYTHON, "-I", "-m", "surogates.image_entry")
        return ImageRun(image=image, workdir=workdir, env=tuple(declared.items()), run_args=run_args, command=command,
                        image_id=str(found.get("Id") or image))

    # -- one container per session ----------------------------------------------------------------------------------

    async def _session_containers(self, name: str) -> list[tuple[str, str]]:
        """(id, state) of the container named *name*, if there is one.  Docker unsure: the call fails (never a guess
        that there is none, which could start a second container or remove the session's)."""
        code, stdout, stderr = await self._docker.run(
            ["ps", "-a", "--filter", f"name=^{name}$", "--format", "{{.ID}} {{.State}}"],
        )
        if code != 0:
            raise SandboxUnavailableError(
                f"docker ps failed: {stderr.decode(errors='replace').strip()[:300]}", classification="docker",
            )
        out = []
        for line in stdout.decode(errors="replace").splitlines():
            cid, _, state = line.partition(" ")
            if cid:
                out.append((cid, state.strip()))
        return out

    async def _port_of(self, container_id: str, name: str = "") -> tuple[bool, int | None] | None:
        """(running, its host port) of *container_id* (Docker picks the port at each start); ``None``: gone, or, with
        *name*, no longer under the session's name (set aside: never worked in again).  Docker unsure (an inspect
        failing for another reason): :class:`SandboxUnavailableError`.  In "range" mode the port is the fixed one the
        container asked for (``HostConfig.PortBindings``): a network that publishes nothing records no other."""
        fixed = getattr(self, "_port_range", None) is not None
        ports = "{{json .HostConfig.PortBindings}}\n{{json .NetworkSettings.Ports}}" if fixed else "{{json .NetworkSettings.Ports}}"
        code, stdout, stderr = await self._docker.run(
            ["inspect", "--format", "{{.Name}}\n{{.State.Running}}\n" + ports, container_id],
        )
        if code != 0:
            if b"No such" in stderr:
                return None
            raise SandboxUnavailableError(  # Docker unsure: never read as "gone"
                f"docker inspect failed: {stderr.decode(errors='replace').strip()[:300]}", classification="docker",
            )
        try:
            # One JSON document a line: JSON never holds a raw newline.
            named, running, ports_json = stdout.decode(errors="replace").strip().split("\n", 2)
            if name and named.strip().lstrip("/") != name:
                return None
            if fixed:
                # The port the container asked for; one made before "range" mode asked for none (Docker picked it):
                # read where Docker published it.
                asked_json, _, ports_json = ports_json.partition("\n")
                asked = (json.loads(asked_json) or {}).get("8071/tcp") or []
                port = next((int(b["HostPort"]) for b in asked
                             if b.get("HostIp") in ("127.0.0.1", "", None) and b.get("HostPort")), None)
                if port is not None:
                    return running.strip() == "true", port
            bindings = (json.loads(ports_json) or {}).get("8071/tcp") or []
            port = next((int(b["HostPort"]) for b in bindings if b.get("HostIp") in ("127.0.0.1", "", None)), None)
        except (ValueError, KeyError, TypeError):
            return None
        return running.strip() == "true", port

    async def _remove(self, container_id: str, *, force: bool = True) -> bool:
        """Remove *container_id* with its anonymous volumes; without *force* only a stopped one.  Whether it went."""
        code, _out, _err = await self._docker.run(["rm", *(["-f"] if force else []), "-v", container_id])
        return code == 0

    async def _set_aside(self, cid: str, session: str) -> None:
        """A container of the session that will not start or not answer its token: renamed out of the way, its
        files kept (the reaper removes it once its session has been at rest long enough), so a new one can start."""
        await self._docker.run(["stop", cid])
        await self._docker.run(["rename", cid, f"surogates-sbx-{session}-aside-{int(time.time())}-{secrets.token_hex(3)}"])
        logger.warning("Set the container %s of session %s aside: a new one starts from the image", cid, session)

    async def _answers(self, host_port: int, token: str) -> int | None:
        """The status the executor on *host_port* answers an authenticated ``/activity`` with (``None``: nothing)."""
        import httpx

        try:
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{host_port}", transport=self._transport, trust_env=False,
                                         timeout=5.0) as client:
                r = await client.get("/activity", headers={"Authorization": f"Bearer {token}"})
            return r.status_code
        except Exception:  # noqa: BLE001
            return None

    async def _provision_image(self, spec: SandboxSpec, sandbox_id: str, token: str, limits: list[str]) -> str:
        if spec.ssh_key_material:
            raise SandboxUnavailableError("SSH access is not offered in a session's own image", classification="config")
        if not spec.session_id:
            raise SandboxUnavailableError("A session's own image needs its session", classification="config")
        run = await self._image_run(spec)
        deployment = deployment_id(self._deployment)
        token = session_token(spec.session_id)
        name = container_name(spec, sandbox_id)
        for cid, state in await self._session_containers(name):
            if state in ("running", "created", "restarting"):
                # The session's, running or being started by another worker this moment: taken over, never removed.
                taken = await self._take_over(spec, sandbox_id, name)
                if taken is not None:
                    return taken
                break  # set aside: a new one starts below
            # Stopped (its executor killed, the daemon restarted, a Stop): started again, its files as they were.
            code, _out, err = await self._docker.run(["start", cid])
            if code == 0:
                logger.info("Started the stopped container %s of session %s again", cid, spec.session_id)
                taken = await self._take_over(spec, sandbox_id, name, started=cid)
                if taken is not None:
                    return taken
                break
            # It will not start: its files are kept aside (the reaper's at rest), and a new one starts.
            logger.warning("The container %s of session %s did not start again: %s", cid, spec.session_id,
                           err.decode(errors="replace").strip()[:300])
            await self._set_aside(cid, spec.session_id)

        bundles = [self._bundle_for(run)]
        arch = self._versions[2] if self._versions else ""
        if self._bundle_image_musl and bundles[0] != self._bundle_image_musl and arch in ("amd64", "x86_64"):
            bundles.append(self._bundle_image_musl)  # the musl bundle is amd64 only
        why = ""
        for i, bundle in enumerate(bundles):
            musl = bool(self._bundle_image_musl) and bundle == self._bundle_image_musl
            if musl:
                # Asked before every use (as the main bundle is in _image_run): a run never pulls an image mount.
                try:
                    await self._ensure_image(bundle, "executor bundle (musl)")
                except SandboxUnavailableError as exc:
                    raise SandboxUnavailableError(
                        f"{exc.reason}{f' (wanted for {run.image}: {why})' if why else ''}",
                        classification=exc.classification,
                    ) from exc
            try:
                taken = await self._run_new(spec, sandbox_id, name, token, limits, run, deployment, bundle,
                                            main_tried=i > 0)
            except _NeedsMusl as exc:
                if i + 1 == len(bundles):
                    raise SandboxUnavailableError(exc.reason, classification="readiness") from exc
                logger.info("The image %s looks like a musl image: the musl executor bundle is tried", run.image)
                why = "the main executor bundle did not start in it"
                continue
            if musl:
                # Its executor answered: a musl image (Alpine), the musl bundle for it from now on.
                self._musl_images.add(run.image_id or run.image)
            return taken
        raise SandboxUnavailableError("no executor bundle", classification="config")  # unreachable

    def _bundle_for(self, run: ImageRun) -> str:
        if self._bundle_image_musl and (run.image_id or run.image) in self._musl_images:
            return self._bundle_image_musl
        return self._bundle_image

    async def _run_new(self, spec: SandboxSpec, sandbox_id: str, name: str, token: str, limits: list[str],
                       run: ImageRun, deployment: str, bundle: str, *, main_tried: bool = False) -> str:
        """A new container of the session from *run*, with *bundle* mounted; its sandbox id once its executor
        answers.  ``_NeedsMusl`` when the main bundle cannot start in it (a musl image)."""
        env = image_env(self._build_env(spec, sandbox_id, token, "ephemeral", None), run.env, run.workdir)
        run_args = list(run.run_args)
        run_args[run_args.index("--name") + 1] = name
        run_args += ["--label", f"surogates.executor_bundle={bundle}",
                     "--mount", f"type=image,source={bundle},target={BUNDLE_MOUNT},"
                                f"image-subpath={BUNDLE_MOUNT.lstrip('/')}"]
        # Docker picks the host port, at every start: a fixed one could be taken while the container is stopped.  In
        # "range" mode a fixed one of this worker's range, held by the container until it is removed (stopped, it
        # still asks for it, and no other container is given it meanwhile).
        fixed = await self._take_port() if getattr(self, "_port_range", None) is not None else None
        if fixed is None:
            return await self._run_new_on(spec, sandbox_id, name, token, limits, run, deployment, bundle, env, run_args,
                                          fixed, main_tried=main_tried)
        try:
            taken = await self._run_new_on(spec, sandbox_id, name, token, limits, run, deployment, bundle, env,
                                           run_args, fixed, main_tried=main_tried)
        except BaseException:
            # Its container may be removed, set aside (still holding the port) or, cut off mid-run, not listed yet.
            self._port_started(fixed, failed=True)
            raise
        self._port_started(fixed)
        return taken

    async def _run_new_on(self, spec: SandboxSpec, sandbox_id: str, name: str, token: str, limits: list[str],
                          run: ImageRun, deployment: str, bundle: str, env: dict, run_args: list[str],
                          fixed: int | None, *, main_tried: bool = False) -> str:
        from surogates.sandbox.docker import _IN_CONTAINER_PORT, _PUBLISH_HOST

        args = ["run", "-d", "-p", f"{_PUBLISH_HOST}:{fixed if fixed is not None else ''}:{_IN_CONTAINER_PORT}"]
        if self._network:
            args += ["--network", self._network]
        args += ["--add-host", "host.docker.internal:host-gateway", "--label", f"app={IMAGE_APP}",
                 "--label", f"surogates.deployment={deployment}", "--label", f"surogates.session_id={spec.session_id}",
                 "--label", f"surogates.sandbox_id={sandbox_id}"]
        if self._worker:
            args += ["--label", f"surogates.worker={self._worker}"]
        args += limits + run_args
        for key, value in env.items():
            args += ["-e", f"{key}={value}"]
        args += [run.image_id or run.image, *run.command]
        for attempt in (1, 2):
            try:
                code, stdout, stderr = await self._docker.run(args)
            except BaseException:
                # Cut off mid-run (a provision cancelled): a container it may have left under the session's name is
                # this run's, never started for any work.
                await asyncio.shield(self._remove_ours(name, sandbox_id))
                raise
            said = stderr.decode(errors="replace")
            if code == 0 or not _NAME_TAKEN_RE.search(said):
                break
            # Another worker started the session's container just now: it is the session's, taken over.
            taken = await self._take_over(spec, sandbox_id, name)
            if taken is not None:
                return taken
            if attempt == 2:
                raise SandboxUnavailableError(f"The session's container {name} could not be taken over",
                                              classification="docker")
            # Not listed yet (Docker holds the name before the container shows), or gone again: once more.
            await asyncio.sleep(0.5)
        if code != 0:
            # A run that failed at its start may leave its container created: removed, only if it is this run's.
            await self._remove_ours(name, sandbox_id)
            raise SandboxUnavailableError(f"docker run of {run.image} failed (exit {code}): {said}",
                                          classification="docker")
        container_id = stdout.decode().strip().splitlines()[0]
        if stderr.strip():
            said = stderr.decode(errors="replace").strip()
            logger.log(logging.WARNING if "WARNING" in said else logging.INFO,
                       "docker run for sandbox %s: %s", sandbox_id, said)

        try:
            seen = await self._port_of(container_id)
            if not seen or not seen[0] or seen[1] is None:
                raise SandboxUnavailableError(
                    "The sandbox's container stopped before its executor became ready", classification="readiness",
                )
            host_port = seen[1]
            # An executor that cannot start ends its container at once: told then, not at the ready timeout.
            await self._wait_ready(host_port, container_id)
        except BaseException as exc:
            said = ""
            if isinstance(exc, Exception):
                said = await self._log_tail(container_id)
            # Never left running, a cancelled provision included.  One that ended never served a call: removed.
            # One that runs may have been taken over meanwhile by another worker and worked in: set aside, its files
            # kept for the reaper.
            await asyncio.shield(self._discard_new(container_id, spec.session_id))
            if said:
                on_musl_bundle = bool(self._bundle_image_musl) and bundle == self._bundle_image_musl
                musl = not on_musl_bundle and bool(_MUSL_RE.search(said))
                hint = ""
                if _CANNOT_RUN_RE.search(said):
                    arch = self._versions[2] if self._versions else ""
                    if on_musl_bundle and main_tried:
                        hint = (" (neither executor bundle starts in it: it has no glibc 2.28 or later and no musl"
                                " 1.2.2 or later (Alpine 3.13), or is of another architecture)")
                    elif on_musl_bundle:
                        hint = " (the musl executor bundle does not start in it)"
                    elif musl and arch not in ("x86_64", "amd64"):
                        hint = f" (a musl image, such as Alpine: no musl executor bundle for {arch})"
                    elif musl and not self._bundle_image_musl:
                        hint = " (a musl image, such as Alpine: set sandbox.executor_bundle_image_musl)"
                    else:
                        hint = " (the image has no glibc 2.28 or later, or is of another architecture)"
                reason = f"{exc}. The executor in {run.image} said: {said}{hint}"
                if musl:
                    raise _NeedsMusl(reason) from exc
                raise SandboxUnavailableError(reason, classification="readiness") from exc
            raise

        self._register(spec, sandbox_id, container_id, host_port, token, created=True)
        logger.info("Provisioned docker sandbox %s in the image %s (container %s, port %d)",
                    sandbox_id, run.image, container_id, host_port)
        return sandbox_id

    async def _discard_new(self, cid: str, session: str) -> None:
        """A new container whose provision failed: removed only if Docker says it is not running (it never served
        a call); running, or unsure, set aside (another worker may have taken it over and worked in it)."""
        if not await self._still_runs(cid):
            await self._remove(cid)
        else:
            await self._set_aside(cid, session)

    async def _remove_ours(self, name: str, sandbox_id: str) -> None:
        code, stdout, _ = await self._docker.run(
            ["ps", "-aq", "--filter", f"name=^{name}$", "--filter", f"label=surogates.sandbox_id={sandbox_id}"],
        )
        for cid in (stdout.decode(errors="replace").split() if code == 0 else []):
            await self._remove(cid)

    def _register(self, spec: SandboxSpec, sandbox_id: str, cid: str, host_port: int, token: str, *, created: bool) -> None:
        from surogates.sandbox.docker import _Entry

        self._entries[sandbox_id] = _Entry(sandbox_id, cid, host_port, token, spec, image=True, created=created)

    async def _take_over(self, spec: SandboxSpec, sandbox_id: str, name: str, *, started: str = "") -> str | None:
        """The session's container, running or starting: taken over once its executor answers its token
        (``_TAKE_OVER_S`` at most for it to run).  ``None``: start a new one, the session's container being set aside
        (its files kept) when it answers but not to this deployment's token (the signing secret rotated), or when
        this provision started it again (*started*, its id) and its executor never came up (a crash at its start, not
        a Stop meanwhile), or when it is retired again after a take-back; or there being none (gone, or not listed
        yet), or one never started removed at the deadline.  One that someone else started and whose executor never answers is stopped, its files
        kept, and the call fails."""
        deadline = time.monotonic() + _TAKE_OVER_S
        token = session_token(spec.session_id)
        revived = looked_again = False
        while time.monotonic() < deadline:
            listed = await self._session_containers(name)
            if not listed:
                return None  # gone meanwhile (a late reaper, another worker's failed run): a new one starts
            for cid, state in listed:
                if state in ("exited", "dead") and cid == started:
                    if await self._crashed(cid):
                        # Ended at once after this provision started it: it will not start (its files kept aside).
                        await asyncio.shield(self._set_aside(cid, spec.session_id))
                        return None
                    raise SandboxUnavailableError(  # a Stop stopped it meanwhile: it stays the session's
                        "The session's container was stopped meanwhile (a Stop): its files kept; the next call starts "
                        "it again", classification="readiness",
                    )
                if state == "exited" and revived and not started:
                    # Taken back from retired but stopped meanwhile: started once more, as this provision's.
                    if (await self._docker.run(["start", cid]))[0] != 0:
                        await asyncio.shield(self._set_aside(cid, spec.session_id))
                        return None
                    started = cid
                    continue
                if state != "running":
                    continue
                stamp = await self._started_at(cid)  # before the port: a start after it shows as a change
                seen = await self._port_of(cid)
                if not seen or not seen[0] or seen[1] is None:
                    continue
                host_port = seen[1]
                try:
                    await self._wait_ready(host_port, cid)
                except Exception as exc:
                    retired = isinstance(exc, SandboxUnavailableError) and exc.classification == "retired"
                    if retired and not revived:
                        # Retired for the reaper: removed in a moment, or kept (it failed); kept, it is taken back.
                        revived = True
                        if await self._back_from_retired(cid, name, spec.session_id):
                            deadline = time.monotonic() + _TAKE_OVER_S
                            break
                        return None
                    now = await self._started_at(cid)
                    if not retired and stamp and now and now != stamp and not looked_again:
                        # Started again by someone else while this waited: looked at again, at its new port.
                        looked_again = True
                        deadline = time.monotonic() + _TAKE_OVER_S
                        break
                    if retired or (started == cid and await self._crashed(cid)):
                        await asyncio.shield(self._set_aside(cid, spec.session_id))
                        return None
                    await asyncio.shield(self._docker.run(["stop", cid]))
                    raise SandboxUnavailableError(
                        f"The session's container {cid} does not answer ({exc}): stopped, its files kept; the "
                        "next call starts it again", classification="readiness",
                    ) from exc
                answered = await self._answers(host_port, token)
                if answered == 401:
                    await self._set_aside(cid, spec.session_id)
                    return None
                self._warn_other_bundle(cid)
                self._register(spec, sandbox_id, cid, host_port, token, created=False)
                logger.info("Took over the container %s of session %s as sandbox %s (port %d)",
                            cid, spec.session_id, sandbox_id, host_port)
                return sandbox_id
            await asyncio.sleep(0.5)
        # Still not running: one that never started (a run cut off between its create and its start) holds no work.
        for cid, state in await self._session_containers(name):
            if state == "created":
                code, out, _ = await self._docker.run(["inspect", "--format", "{{.State.StartedAt}}", cid])
                if code == 0 and out.decode(errors="replace").strip().startswith("0001-") and await self._remove(
                        cid, force=False):
                    return None
        raise SandboxUnavailableError(
            f"The session's container {name} never came to run", classification="docker",
        )

    def _warn_other_bundle(self, cid: str) -> None:
        """A container made by a worker with another bundle keeps that bundle's executor for its life: logged."""
        self._bundle_checks = getattr(self, "_bundle_checks", set())
        if cid in self._bundle_checks:
            return
        if len(self._bundle_checks) > 1000:
            self._bundle_checks.clear()
        self._bundle_checks.add(cid)

        async def check() -> None:
            code, out, _ = await self._docker.run(
                ["inspect", "--format", '{{index .Config.Labels "surogates.executor_bundle"}}', cid])
            made_with = out.decode(errors="replace").strip()
            mine = {self._bundle_image, *([self._bundle_image_musl] if self._bundle_image_musl else [])}
            if code == 0 and made_with and made_with not in mine:
                logger.warning("The container %s runs the executor of %s, not this worker's %s: a new bundle "
                               "applies to new containers only", cid, made_with, " / ".join(sorted(mine)))
        task = asyncio.ensure_future(check())
        self._checks_running = getattr(self, "_checks_running", set())
        self._checks_running.add(task)
        task.add_done_callback(self._checks_running.discard)

    async def execute_in_image(self, entry: Any, name: str, input: str, timeout: float | None) -> str:
        """A call into the session's container, wherever it is now: started again if it stopped (a Stop elsewhere,
        its executor killed), at the port Docker gave it at its last start."""
        named = container_name(entry.spec, entry.sandbox_id)
        seen = await self._port_of(entry.container_id, named)
        if seen is None:
            if name == "process" and _is_kill(input):
                return json.dumps({"status": "already_exited", "note": "The command's container is gone: it ended "
                                   "with it."})
            raise SandboxUnavailableError(f"The session's container {entry.container_id} is gone")
        running, port = seen
        if not running:
            if name.startswith("_"):
                # The worker's own look (a drain): a stopped container stays stopped.
                raise SandboxUnavailableError("The session's container is stopped")
            if name == "process" and _is_kill(input):
                # A kill (the watch's, or the model's): stopping the container ended every command in it.
                return json.dumps({"status": "already_exited", "note": "The session's container is stopped: its "
                                   "commands ended with it."})
            port = await self._start_again(entry)
        if port is None:
            raise SandboxUnavailableError("The session's container has no port")
        entry.host_port = port
        try:
            return await self._client.execute(
                host="127.0.0.1", port=port, token=entry.token, name=name, args_str=input,
                timeout=timeout or entry.spec.timeout,
            )
        except SandboxUnavailableError as exc:
            if exc.classification != "retired" or name.startswith("_"):
                raise
        # Retired for the reaper (the session was at rest): it removes the container in a moment.  The call never
        # ran.  Removed: the next call makes a new one.  Kept (the reaper failed, or a turn came first): its
        # executor comes back, its files as they were, and the call runs.
        if not await self._back_from_retired(entry.container_id, named, entry.spec.session_id):
            raise SandboxUnavailableError("The session's container was removed at rest: the next call starts a new "
                                          "one from its image")
        port = await self._start_again(entry)
        entry.host_port = port
        return await self._client.execute(
            host="127.0.0.1", port=port, token=entry.token, name=name, args_str=input,
            timeout=timeout or entry.spec.timeout,
        )

    async def _back_from_retired(self, cid: str, name: str, session: str) -> bool:
        """After a 410: wait for the reaper to remove *cid* (False: gone, or no longer under the session's *name*);
        still there after ``_RETIRED_WAIT_S``, the reaper kept it, and its executor is taken back (``/unretire``:
        no restart, its port and processes as they are; idempotent, so any number of waiting calls on any worker may
        ask).  True: there (a stopped one is left to the caller's start)."""
        deadline = time.monotonic() + _RETIRED_WAIT_S
        while time.monotonic() < deadline:
            if await self._port_of(cid, name) is None:
                return False
            await asyncio.sleep(0.5)
        seen = await self._port_of(cid, name)
        if seen is None:
            return False
        running, port = seen
        if running and port is not None:
            import httpx

            try:
                async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", transport=self._transport, trust_env=False,
                                             timeout=5.0) as client:
                    r = await client.post("/unretire", headers={"Authorization": f"Bearer {session_token(session)}"})
                if r.status_code != 200:
                    logger.warning("The retired container %s was not taken back (%s)", cid, r.status_code)
            except Exception:  # noqa: BLE001 - the caller's wait for its executor tells
                logger.warning("Could not take the retired container %s back", cid, exc_info=True)
        return True

    async def _started_at(self, cid: str) -> str:
        """When *cid* last started (Docker's own stamp; "" unknown): a change tells a start by someone else."""
        code, out, _ = await self._docker.run(["inspect", "--format", "{{.State.StartedAt}}", cid])
        return out.decode(errors="replace").strip() if code == 0 else ""

    async def _health(self, host_port: int) -> int | None:
        import httpx

        try:
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{host_port}", transport=self._transport, trust_env=False,
                                         timeout=5.0) as client:
                return (await client.get("/healthz")).status_code
        except Exception:  # noqa: BLE001
            return None

    async def _crashed(self, cid: str) -> bool:
        """Whether *cid* ended by itself (an executor that could not start) or runs without answering, not stopped
        by a Stop or a pause meanwhile (exit 0, 137, 143: its files stay the session's)."""
        code, out, _ = await self._docker.run(["inspect", "--format", "{{.State.Status}} {{.State.ExitCode}}", cid])
        if code != 0:
            return False
        status, _, exit_code = out.decode(errors="replace").strip().partition(" ")
        if status == "running":
            # Started again meanwhile (another call, another worker) at a port of its own: answering, no crash.
            try:
                seen = await self._port_of(cid)
            except SandboxUnavailableError:
                return False
            return not (seen and seen[1] is not None and await self._health(seen[1]) in (200, 410))
        return exit_code.strip() not in ("0", "137", "143")

    async def _start_again(self, entry: Any) -> int:
        """Start the session's stopped container again (one running already: as it is) and wait for its executor;
        set it aside if it will not come back (its files kept), so the next call makes a new one."""
        code, _out, err = await self._docker.run(["start", entry.container_id])
        if code != 0:
            await self._set_aside(entry.container_id, entry.spec.session_id)
            raise SandboxUnavailableError(
                f"The session's container did not start again: {err.decode(errors='replace').strip()[:300]}",
            )
        stamp = await self._started_at(entry.container_id)
        for look in (1, 2):
            seen = await self._port_of(entry.container_id)
            port = seen[1] if seen and seen[0] else None
            try:
                if port is None:
                    raise SandboxUnavailableError("The session's container did not come back")
                await self._wait_ready(port, entry.container_id)
                return port
            except Exception:
                now = await self._started_at(entry.container_id)
                if look == 1 and stamp and now and now != stamp:
                    stamp = now  # started again by someone else meanwhile: its new port, once more
                    continue
                # One that crashed at its start is set aside (its files kept), so the next call makes a new one;
                # one stopped meanwhile (a Stop) stays the session's.
                if await self._crashed(entry.container_id):
                    await asyncio.shield(self._set_aside(entry.container_id, entry.spec.session_id))
                raise
        raise AssertionError("unreachable")  # look 2 returns or raises

    def created(self, sandbox_id: str) -> bool:
        """Whether *sandbox_id* is a container this provision made new from the image (not one taken over or
        started again): a session that worked in its container before has none of that work there."""
        entry = self._entries.get(sandbox_id)
        return bool(entry is not None and getattr(entry, "image", False) and getattr(entry, "created", False))

    async def gone(self, sandbox_id: str) -> bool:
        """Whether *sandbox_id*'s container no longer runs: removed, or stopped (Docker sure of it; a failing
        ``docker inspect`` is no answer)."""
        entry = self._entries.get(sandbox_id)
        if entry is None:
            return True
        code, out, err = await self._docker.run(
            ["inspect", "--format", "{{.Name}} {{.State.Running}}", entry.container_id])
        if code != 0:
            return b"No such" in err
        named, _, running = out.decode(errors="replace").strip().partition(" ")
        return running == "false" or named.lstrip("/") != container_name(entry.spec, sandbox_id)

    def forget(self, sandbox_id: str) -> None:
        """Drop the entry of a sandbox whose container no longer runs, without a Docker call (the pool's cleanup)."""
        self._entries.pop(sandbox_id, None)

    async def stop_image_sandbox(self, entry: Any) -> None:
        """A Stop or a pause: the container stops, its files kept for the session's next call; the reaper removes
        it once the session has been at rest long enough."""
        await self._docker.run(["stop", entry.container_id])
        logger.info("Stopped the container %s of session %s (kept)", entry.container_id, entry.spec.session_id)

    async def stop_image_of_session(self, session_id: str) -> None:
        """A Stop or ``/clear`` of a session in its own image on a worker that holds no entry for its container: the
        container stops all the same (its runaway commands with it), its files kept."""
        await self._docker.run(["stop", container_name(SandboxSpec(session_id=session_id), session_id)])

    async def _retire(self, cid: str, session: str, idle_seconds: float) -> dict | None:
        """``POST /retire`` to a running container's executor; ``None`` when it did not answer, ``{}`` when Docker
        could not tell where it is (kept, not counted as unanswered)."""
        try:
            seen = await self._port_of(cid)
        except SandboxUnavailableError:
            return {}
        if not seen or not seen[0] or seen[1] is None:
            return None
        import httpx

        try:
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{seen[1]}", transport=self._transport, trust_env=False,
                                         timeout=5.0) as client:
                r = await client.post("/retire", headers={"Authorization": f"Bearer {session_token(session)}"},
                                      json={"min_idle_seconds": idle_seconds})
            answer = r.json() if r.status_code == 200 else None
        except Exception:  # noqa: BLE001 - unsure: kept
            return None
        return answer if isinstance(answer, dict) else None

    async def reap_images(self, at_rest: Any, *, idle_seconds: float) -> list[tuple[str, str]]:
        """Remove this deployment's session-image containers whose session (and the agents below it) is at rest
        and that went unused for *idle_seconds* by their own clock: the executor retires a running one only if no
        call runs, nothing it waits on runs, and it went unused that long (``/retire``, atomic with its calls);
        a stopped one counts from its stop.  A running one whose executor never answers is stopped after a few
        rounds at rest (then the stopped clock applies).  Unsure: kept.  (session id, container id) of each one
        removed."""
        if not self._bundle_image or not self._deployment:
            return []  # no session image can run here
        code, stdout, _ = await self._docker.run(
            ["ps", "-a", "--filter", f"label=app={IMAGE_APP}",
             "--filter", f"label=surogates.deployment={deployment_id(self._deployment)}",
             "--format", "{{.ID}} {{.State}}"],
        )
        if code != 0:
            return []
        unanswered = self.__dict__.setdefault("_unanswered", {})
        listed = {line.partition(" ")[0] for line in stdout.decode(errors="replace").splitlines()}
        for gone in [cid for cid in unanswered if cid not in listed]:
            unanswered.pop(gone, None)
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
                    unanswered.pop(cid, None)
                    continue
                if state.strip() == "running":
                    retired = await self._retire(cid, session, idle_seconds)
                    if retired == {}:
                        continue  # Docker unsure: kept, asked again next round
                    if retired is None:
                        unanswered[cid] = unanswered.get(cid, 0) + 1
                        if unanswered[cid] >= _UNANSWERED_ROUNDS:
                            # Hung, at rest: stopped, its files kept; the stopped clock removes it.
                            await self._docker.run(["stop", cid])
                            unanswered.pop(cid, None)
                        continue
                    unanswered.pop(cid, None)
                    if not retired.get("retired"):
                        continue
                    # Asked once more right before the stop: a worker that took it back meanwhile (``/unretire``,
                    # after waiting for this removal far longer than it takes) has un-retired it: kept.
                    again = await self._retire(cid, session, idle_seconds)
                    if not again or not again.get("retired"):
                        continue
                    # Stopped, then removed without -f: one started meanwhile is not taken.
                    gone = await asyncio.shield(self._stop_and_remove(cid))
                elif _stopped_for(finished, idle_seconds):
                    # Without -f: one started again meanwhile by a worker is not taken.
                    gone = await self._remove(cid, force=False)
                else:
                    continue
                if not gone:
                    continue
                for sid, entry in list(self._entries.items()):
                    if entry.container_id == cid:
                        self._entries.pop(sid, None)
                logger.info("Reaped the container %s of session %s at rest", cid, session)
                reaped.append((session, cid))
            except Exception:  # noqa: BLE001 - one container's trouble never ends the round
                logger.warning("Could not reap the session-image container %s", cid, exc_info=True)
        return reaped

    async def _stop_and_remove(self, cid: str) -> bool:
        await self._docker.run(["stop", "-t", "2", cid])
        return await self._remove(cid, force=False)

    async def _still_runs(self, container_id: str) -> bool:
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
