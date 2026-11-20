"""A session's own sandbox image (session config ``sandbox_image``): the task's image, the executor bundle mounted in it.

What is held: only images on the operator's list run, and the list is empty unless set (the harness as before);
a sub-agent works in its root's image; the Docker backend starts the bundle's executor in the image with the image's
own user, folder and environment; a session that names no image gets exactly the container it got before.
"""

from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import httpx
import pytest

from surogates.sandbox.base import SandboxSpec, SandboxUnavailableError
from surogates.sandbox.docker import DockerSandbox
from surogates.sandbox.session_image import (
    SessionImage,
    SessionImageRefused,
    refusal,
    session_image_of,
    valid_reference,
    valid_workdir,
)

TASK_IMAGE = "registry.example/tasks/repo-fix:2026-11-20"
BUNDLE = "registry.example/surogates-executor-bundle:test"
ALLOWED = SimpleNamespace(allowed_images=["registry.example/tasks/*"], require_image_digest=False)


# ── which images a session may name ─────────────────────────────────────────


def test_no_image_named_is_the_backends_own():
    assert session_image_of({}, ALLOWED) is None
    assert session_image_of({"sandbox_image": ""}, ALLOWED) is None
    assert session_image_of(None, ALLOWED) is None


def test_an_empty_list_runs_no_session_image():
    with pytest.raises(SessionImageRefused, match="allowed_images is empty"):
        session_image_of({"sandbox_image": TASK_IMAGE}, SimpleNamespace(allowed_images=[]))


def test_an_image_on_the_list_runs_with_its_folder():
    got = session_image_of({"sandbox_image": TASK_IMAGE, "sandbox_workdir": "/testbed/repo"}, ALLOWED)
    assert got == SessionImage(image=TASK_IMAGE, workdir="/testbed/repo")
    assert session_image_of({"sandbox_image": TASK_IMAGE}, ALLOWED) == SessionImage(image=TASK_IMAGE, workdir="")


def test_an_image_off_the_list_is_refused():
    with pytest.raises(SessionImageRefused, match="not one this deployment runs"):
        session_image_of({"sandbox_image": "docker.io/library/ubuntu:24.04"}, ALLOWED)


@pytest.mark.parametrize("image", [
    "--privileged", "-v/:/host", "ubuntu:24.04 --privileged", "ubuntu:24.04\n", "UPPER/case:1", "repo@sha256:abc",
    "a" * 300, 12, "registry.example/tasks/x:tag,source=/", "",
])
def test_anything_but_a_reference_is_refused(image):
    config = {"sandbox_image": image}
    if image == "":
        assert session_image_of(config, ALLOWED) is None
        return
    with pytest.raises(SessionImageRefused):
        session_image_of(config, SimpleNamespace(allowed_images=["*"]))


@pytest.mark.parametrize("image", [
    "ubuntu", "ubuntu:24.04", "docker.io/library/python:3.13-slim-bookworm", "localhost:5000/a/b:c",
    "ghcr.io/younesbelkada/scale800:life4__textdistance-7pdye0v2", "jefzda/sweap-images:ansible.ansible-ansible__x-v3",
    "alexgshaw/regex-chess:20251031", "x/y@sha256:" + "0" * 64, "x/y:1@sha256:" + "a" * 64,
])
def test_references_as_docker_writes_them_are_valid(image):
    assert valid_reference(image)


@pytest.mark.parametrize("workdir", ["/", "app", "/app/", "/app/../etc", "/app/./x", "/a b", "/a,b", "/a:b", "//app"])
def test_a_folder_that_is_no_plain_absolute_path_is_refused(workdir):
    assert not valid_workdir(workdir)
    with pytest.raises(SessionImageRefused, match="absolute folder"):
        session_image_of({"sandbox_image": TASK_IMAGE, "sandbox_workdir": workdir}, ALLOWED)


def test_a_folder_without_an_image_is_refused():
    assert "needs sandbox_image" in refusal({"sandbox_workdir": "/app"}, ALLOWED)


def test_a_digest_can_be_required():
    pinned = SimpleNamespace(allowed_images=["registry.example/tasks/*"], require_image_digest=True)
    assert "pinned by digest" in refusal({"sandbox_image": TASK_IMAGE}, pinned)
    assert refusal({"sandbox_image": "registry.example/tasks/x@sha256:" + "1" * 64}, pinned) is None


def test_the_default_settings_run_no_session_image():
    from surogates.config import SandboxSettings

    settings = SandboxSettings()
    assert settings.allowed_images == []
    assert refusal({"sandbox_image": TASK_IMAGE}, settings) is not None
    assert refusal({}, settings) is None


@pytest.mark.parametrize("image", ["3f2a9c", "sha256:" + "a" * 64, "a" * 64, "deadbeef01"])
def test_a_name_docker_could_read_as_an_image_id_is_refused(image):
    with pytest.raises(SessionImageRefused, match="not an image reference"):
        session_image_of({"sandbox_image": image}, SimpleNamespace(allowed_images=["*"]))


def test_the_list_matches_an_image_as_written_or_in_full():
    full = SimpleNamespace(allowed_images=["docker.io/library/python:*", "docker.io/alexgshaw/*"])
    assert refusal({"sandbox_image": "python:3.13-slim"}, full) is None
    assert refusal({"sandbox_image": "alexgshaw/regex-chess:20251031"}, full) is None
    assert refusal({"sandbox_image": "ghcr.io/alexgshaw/regex-chess:1"}, full) is not None
    short = SimpleNamespace(allowed_images=["alexgshaw/*"])
    assert refusal({"sandbox_image": "evil.example/alexgshaw/x:1"}, short) is not None


# ── the worker's spec, and a sub-agent's ────────────────────────────────────


def _session(config: dict) -> Any:
    return SimpleNamespace(id=uuid4(), agent_id="agent-1", config={"workspace_path": "/data/ws", **config})


def _settings(patterns: list[str]) -> Any:
    return SimpleNamespace(sandbox=SimpleNamespace(allowed_images=patterns, require_image_digest=False))


async def test_the_spec_takes_the_sessions_image_and_folder():
    from surogates.harness import tool_exec

    session = _session({"sandbox_image": TASK_IMAGE, "sandbox_workdir": "/app"})
    with patch("surogates.config.load_settings", return_value=_settings(["registry.example/tasks/*"])):
        spec = await tool_exec._build_session_sandbox_spec(session, SimpleNamespace(), "root-1")
    assert (spec.session_image, spec.image_workdir) == (TASK_IMAGE, "/app")


async def test_the_worker_holds_the_image_to_its_own_list():
    from surogates.harness import tool_exec

    session = _session({"sandbox_image": TASK_IMAGE})
    with patch("surogates.config.load_settings", return_value=_settings([])):
        with pytest.raises(SandboxUnavailableError, match="allowed_images") as raised:
            await tool_exec._build_session_sandbox_spec(session, SimpleNamespace(), "root-1")
    assert raised.value.classification == "config"


async def test_a_session_without_an_image_gets_the_spec_it_got_before():
    from surogates.harness import tool_exec

    with patch("surogates.config.load_settings", return_value=_settings([])):
        spec = await tool_exec._build_session_sandbox_spec(_session({}), SimpleNamespace(), "root-1")
    assert (spec.session_image, spec.image_workdir) == ("", "")


async def test_a_session_that_names_no_image_never_reads_the_settings_for_it():
    from surogates.harness import tool_exec

    with patch("surogates.config.load_settings", side_effect=AssertionError("read")):
        spec = await tool_exec._build_session_sandbox_spec(_session({}), SimpleNamespace(), "root-1")
    assert spec.session_image == ""


async def test_a_sub_agent_works_in_its_roots_image_and_cannot_name_another():
    from surogates.session.provisioning import create_child_session

    parent = MagicMock()
    parent.id, parent.agent_id, parent.model, parent.channel = uuid4(), "agent-1", "m", "api"
    parent.org_id, parent.user_id, parent.service_account_id = uuid4(), uuid4(), None
    parent.config = {"storage_bucket": "b", "storage_key_prefix": "", "workspace_path": "/ws", "supports_vision": False,
                     "sandbox_image": TASK_IMAGE, "sandbox_workdir": "/app"}
    store = AsyncMock()
    store.create_session = AsyncMock(return_value=MagicMock())
    await create_child_session(store=store, parent=parent, channel="delegation",
                               config={"sandbox_image": "evil/other:1", "sandbox_workdir": "/"})
    config = store.create_session.call_args.kwargs["config"]
    assert (config["sandbox_image"], config["sandbox_workdir"]) == (TASK_IMAGE, "/app")

    parent.config = {k: v for k, v in parent.config.items() if not k.startswith("sandbox_")}
    await create_child_session(store=store, parent=parent, channel="delegation", config={"sandbox_image": "evil/other:1"})
    config = store.create_session.call_args.kwargs["config"]
    assert "sandbox_image" not in config and "sandbox_workdir" not in config


async def test_the_process_backend_refuses_a_session_image():
    from surogates.sandbox.process import ProcessSandbox

    with pytest.raises(SandboxUnavailableError, match="Docker sandbox backend"):
        await ProcessSandbox().provision(SandboxSpec(session_id="r", session_image=TASK_IMAGE))


# ── the Docker backend ──────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _signing_secret(monkeypatch):
    """The deployment's signing secret: a session image's executor token and deployment mark derive from it."""
    monkeypatch.setenv("SUROGATES_JWT_SECRET", "test-signing-secret-of-this-deployment")


class ImageDocker:
    """A Docker CLI that knows images, pulls, named containers (Docker-picked ports) and logs, besides runs."""

    def __init__(self, *, images: dict[str, dict] | None = None, api: str = "1.51", client_api: str | None = None,
                 arch: str = "x86_64", pull_ok: bool = True) -> None:
        self.calls: list[list[str]] = []
        self.images = dict(images or {})
        self.api, self.client_api, self.arch, self.pull_ok = api, client_api or api, arch, pull_ok
        self.pull_started = asyncio.Event()
        self.pull_gate: asyncio.Event | None = None
        self.logs = b""
        self.exited = False
        #: name -> [id, state, session]; a running one has port 33000 + its index
        self.named: dict[str, list] = {}
        self.run_answers: list[tuple[int, bytes, bytes]] = []
        self.start_error = b""

    def _by_id(self, cid: str):
        return next(((name, c) for name, c in self.named.items() if c[0] == cid), (None, None))

    async def run(self, args: list[str]) -> tuple[int, bytes, bytes]:
        self.calls.append(args)
        joined = " ".join(args)
        if args[:1] == ["version"]:
            return 0, f"{self.client_api} {self.api} {self.arch}\n".encode(), b""
        if args[:1] == ["info"]:
            return 0, b"8 true true true true\n", b""
        if args[:2] == ["image", "inspect"]:
            found = self.images.get(args[2])
            return (0, json.dumps([found]).encode(), b"") if found is not None else (1, b"[]", b"No such image")
        if args[:1] == ["pull"]:
            self.pull_started.set()
            if self.pull_gate is not None:
                await self.pull_gate.wait()
            if not self.pull_ok:
                return 1, b"", b"denied"
            self.images[args[-1]] = _image()
            return 0, b"", b""
        if args[:2] == ["run", "-d"]:
            if self.run_answers:
                return self.run_answers.pop(0)
            if "--name" not in args:  # a default sandbox
                return 0, b"cid-1\n", b""
            name = args[args.index("--name") + 1]
            session = next(a.split("=", 1)[1] for a in args if a.startswith("surogates.session_id="))
            cid = f"cid-{len(self.named) + 1}"
            self.named[name] = [cid, "running", session]
            return 0, cid.encode() + b"\n", b""
        if args[:1] == ["start"]:
            name, c = self._by_id(args[-1])
            if self.start_error:
                return 1, b"", self.start_error
            if c is not None:
                c[1] = "running"
            return 0, b"", b""
        if args[:1] == ["stop"]:
            name, c = self._by_id(args[-1]) if not args[-1].startswith("surogates-sbx-") else (args[-1], self.named.get(args[-1]))
            if c is not None:
                c[1] = "exited"
            return 0, b"", b""
        if args[:1] == ["rm"]:
            name, c = self._by_id(args[-1])
            if c is None and args[-1] in self.named:
                name = args[-1]
            if name is not None:
                self.named.pop(name, None)
            return 0, b"", b""
        if args[:1] == ["logs"]:
            return 0, self.logs, b""
        if args[:1] == ["inspect"] and "{{.State.Running}}" in args:
            return 0, b"false\n" if self.exited else b"true\n", b""
        if args[:1] == ["inspect"] and "NetworkSettings.Ports" in joined:
            name, c = self._by_id(args[-1])
            if c is None:
                return 1, b"", b"No such object"
            idx = list(self.named).index(name)
            ports = {"8071/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(33000 + idx)}]} if c[1] == "running" else {}
            return 0, f"{json.dumps(ports)}\n{c[2]}\n".encode(), b""
        if args[:2] == ["ps", "-a"] and any(a.startswith("name=") for a in args):
            name = next(a for a in args if a.startswith("name=")).removeprefix("name=^").removesuffix("$")
            c = self.named.get(name)
            return 0, (f"{c[0]} {c[1]}\n".encode() if c else b""), b""
        if args[:2] == ["ps", "-aq"]:
            return 0, b"", b""
        return 0, b"", b""

    def run_args(self) -> list[str]:
        return next(c for c in self.calls if c[:2] == ["run", "-d"])


def _image(env: list[str] | None = None, workdir: str = "/app", arch: str = "amd64") -> dict:
    return {"Os": "linux", "Architecture": arch,
            "Config": {"Env": env if env is not None else ["PATH=/usr/local/bin:/usr/bin", "PYTHONPATH=/app/lib:/app"],
                       "WorkingDir": workdir, "User": ""}}


def _ready() -> httpx.AsyncBaseTransport:
    class Ready(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            return httpx.Response(200, text="ok")
    return Ready()


def _never_ready() -> httpx.AsyncBaseTransport:
    class Down(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            raise httpx.ConnectError("refused")
    return Down()


def _docker_backend(docker, transport=None, **kw) -> DockerSandbox:
    kw.setdefault("executor_bundle_image", BUNDLE)
    return DockerSandbox(image="sbx-test:1", executor_port_base=33000, ready_timeout=1, network="sbx-net",
                         docker=docker, httpx_transport=transport or _ready(), **kw)


def _flag(args: list[str], flag: str) -> list[str]:
    return [args[i + 1] for i, a in enumerate(args[:-1]) if a == flag]


def _env(args: list[str]) -> dict[str, str]:
    return dict(item.split("=", 1) for item in _flag(args, "-e"))


def _shadowed(env: dict[str, str]) -> dict[str, str]:
    import base64

    return json.loads(base64.b64decode(env["SUROGATES_IMAGE_ENV_SHADOWED"]))


async def test_a_session_image_runs_the_bundles_executor_in_the_image():
    from surogates.sandbox.docker_image import deployment_id, session_token

    docker = ImageDocker(images={TASK_IMAGE: _image(), BUNDLE: _image()})
    backend = _docker_backend(docker)
    sid = await backend.provision(SandboxSpec(session_id="root-1", session_image=TASK_IMAGE))
    args = docker.run_args()

    image_at = args.index(TASK_IMAGE)
    assert args[image_at + 1:] == ["--", "/opt/surogates/bin/python3", "-I", "-m", "surogates.image_entry"]
    assert _flag(args, "--entrypoint") == ["/opt/surogates/bin/tini"]
    assert _flag(args, "--name") == ["surogates-sbx-root-1"]
    # Docker picks the host port at each start, on the loopback only.
    assert _flag(args, "-p") == ["127.0.0.1::8071"]
    assert _flag(args, "--mount") == [
        f"type=image,source={BUNDLE},target=/opt/surogates,image-subpath=opt/surogates"]
    assert _flag(args, "--workdir") == ["/app"]
    assert "--rm" not in args and "--user" not in args
    assert _flag(args, "--security-opt") == ["no-new-privileges"]
    assert _flag(args, "--cap-drop") == ["NET_RAW", "MKNOD", "SETFCAP"]
    assert "--no-healthcheck" in args
    labels = _flag(args, "--label")
    assert "surogates.sandbox_image=1" in labels and "surogates.session_id=root-1" in labels
    # Never the agent-sandbox's label: no sweep of the session's sandbox containers takes it.
    assert "app=surogates-image-sandbox" in labels and "app=surogates-sandbox" not in labels
    assert f"surogates.executor_bundle={BUNDLE}" in labels and f"surogates.deployment={deployment_id()}" in labels
    # The limits apply as to every sandbox, and no storage is mounted in the image.
    assert _flag(args, "--memory") == [str(8 * 2**30)] and _flag(args, "--pids-limit") == ["4096"]
    assert "-v" not in args and "--device" not in args and "--cap-add" not in args
    env = _env(args)
    assert env["TOOL_EXECUTOR_TOKEN"] == session_token("root-1") == backend._entries[sid].token
    assert env["WORKSPACE_DIR"] == "/app" and env["TOOL_EXECUTOR_REQUIRE_FUSE"] == "0"
    assert env["TOOL_EXECUTOR_PORT"] == "8071"
    assert env["SUROGATES_IMAGE_SESSION"] == "1" and env["SUROGATES_EXECUTOR_HOME"] == "/opt/surogates"
    assert env["SUROGATES_IMAGE_ENV_KEYS"] == "PATH,PYTHONPATH" and _shadowed(env) == {}
    # The image's own values stay the image's: nothing of them is set here.
    assert "PATH" not in env and "PYTHONPATH" not in env and "HOME" not in env
    assert backend._entries[sid].host_port == 33000 and backend.created(sid)


def test_the_token_and_the_mark_are_the_deployments(monkeypatch):
    from surogates.sandbox.docker_image import deployment_id, session_token

    a_token, a_mark = session_token("s"), deployment_id()
    assert session_token("s") == a_token and session_token("t") != a_token and len(a_token) >= 43
    monkeypatch.setenv("SUROGATES_JWT_SECRET", "another-deployment")
    assert session_token("s") != a_token and deployment_id() != a_mark
    monkeypatch.delenv("SUROGATES_JWT_SECRET")
    with pytest.raises(SandboxUnavailableError, match="SUROGATES_JWT_SECRET"):
        session_token("s")


async def test_the_sessions_folder_wins_over_the_images():
    docker = ImageDocker(images={TASK_IMAGE: _image(workdir="/"), BUNDLE: _image()})
    await _docker_backend(docker).provision(
        SandboxSpec(session_id="r", session_image=TASK_IMAGE, image_workdir="/testbed/repo"))
    assert _flag(docker.run_args(), "--workdir") == ["/testbed/repo"]
    assert _env(docker.run_args())["WORKSPACE_DIR"] == "/testbed/repo"


async def test_an_image_without_a_working_folder_needs_one_named():
    docker = ImageDocker(images={TASK_IMAGE: _image(workdir=""), BUNDLE: _image()})
    with pytest.raises(SandboxUnavailableError, match="sandbox_workdir"):
        await _docker_backend(docker).provision(SandboxSpec(session_id="r", session_image=TASK_IMAGE))
    assert not any(c[:2] == ["run", "-d"] for c in docker.calls)


async def test_the_images_own_names_for_the_harnesss_controls_are_held_for_the_daemon_and_given_to_the_commands():
    """An image that declares PROJECT_DIR would make the daemon a thread's pod; one with TOOL_EXECUTOR_PORT would
    move it off its port; the harness's own names (MCP_PROXY_URL here) stay the harness's in the daemon."""
    env = ["PATH=/x", "TOOL_EXECUTOR_TOKEN=planted", "WORKSPACE_DIR=/elsewhere", "MCP_PROXY_URL=x", "GOPATH=/go",
           "SUROGATES_IMAGE_ENV_KEYS=AWS_SECRET_ACCESS_KEY", "PROJECT_DIR=/app", "HISTORY_MAIN=1",
           "TOOL_EXECUTOR_PORT=9999", "bad-name=1", "PATH=/again"]
    docker = ImageDocker(images={TASK_IMAGE: _image(env=env), BUNDLE: _image()})
    backend = _docker_backend(docker, mcp_proxy_url="http://127.0.0.1:9")
    await backend.provision(SandboxSpec(session_id=str(uuid4()), session_image=TASK_IMAGE))
    sent = _env(docker.run_args())
    assert sent["SUROGATES_IMAGE_ENV_KEYS"] == "PATH,GOPATH"
    assert sent["WORKSPACE_DIR"] == "/app" and sent["TOOL_EXECUTOR_PORT"] == "8071"
    # The image's own control names leave the daemon's environment before it reads it (surogates.image_entry).
    assert "PROJECT_DIR" not in sent and "HISTORY_MAIN" not in sent
    assert sent["SUROGATES_IMAGE_ENV_UNSET"] == "PROJECT_DIR,HISTORY_MAIN"
    assert sent["MCP_PROXY_URL"] == "http://host.docker.internal:9"
    assert sent["TOOL_EXECUTOR_TOKEN"] != "planted"
    assert _shadowed(sent) == {
        "TOOL_EXECUTOR_TOKEN": "planted", "WORKSPACE_DIR": "/elsewhere", "MCP_PROXY_URL": "x",
        "SUROGATES_IMAGE_ENV_KEYS": "AWS_SECRET_ACCESS_KEY", "PROJECT_DIR": "/app", "HISTORY_MAIN": "1",
        "TOOL_EXECUTOR_PORT": "9999"}


async def test_a_running_container_of_the_session_is_taken_over_not_doubled():
    """With several workers a session's turns and its agents run on any of them: in its own image all work in its
    one container, whose files are the session's workspace."""
    from surogates.sandbox.docker_image import session_token

    docker = ImageDocker(images={TASK_IMAGE: _image(), BUNDLE: _image()})
    docker.named["surogates-sbx-root-1"] = ["cid-other", "running", "root-1"]
    backend = _docker_backend(docker)
    sid = await backend.provision(SandboxSpec(session_id="root-1", session_image=TASK_IMAGE))
    assert not any(c[:2] == ["run", "-d"] for c in docker.calls)
    entry = backend._entries[sid]
    assert (entry.container_id, entry.host_port, entry.token, entry.image) == ("cid-other", 33000,
                                                                               session_token("root-1"), True)
    assert not backend.created(sid)


async def test_a_stopped_container_of_the_session_is_started_again_with_its_files():
    """Its executor killed by a command, the Docker daemon restarted, a Stop: the container's files are the work."""
    docker = ImageDocker(images={TASK_IMAGE: _image(), BUNDLE: _image()})
    docker.named["surogates-sbx-root-1"] = ["cid-stopped", "exited", "root-1"]
    backend = _docker_backend(docker)
    sid = await backend.provision(SandboxSpec(session_id="root-1", session_image=TASK_IMAGE))
    assert ["start", "cid-stopped"] in docker.calls
    assert backend._entries[sid].container_id == "cid-stopped" and not backend.created(sid)
    assert not any(c[:1] == ["rm"] for c in docker.calls) and not any(c[:2] == ["run", "-d"] for c in docker.calls)


async def test_a_container_that_does_not_start_for_a_passing_reason_is_kept():
    """A port, a busy daemon: the files are the session's work: the call fails, the container stays."""
    docker = ImageDocker(images={TASK_IMAGE: _image(), BUNDLE: _image()})
    docker.named["surogates-sbx-root-1"] = ["cid-stopped", "exited", "root-1"]
    docker.start_error = b"Error response from daemon: driver failed programming external connectivity"
    with pytest.raises(SandboxUnavailableError, match="did not start again"):
        await _docker_backend(docker).provision(SandboxSpec(session_id="root-1", session_image=TASK_IMAGE))
    assert not any(c[:1] == ["rm"] for c in docker.calls) and not any(c[:2] == ["run", "-d"] for c in docker.calls)


async def test_a_container_that_can_never_start_is_removed_with_its_volumes_and_a_new_one_starts():
    docker = ImageDocker(images={TASK_IMAGE: _image(), BUNDLE: _image()})
    docker.named["surogates-sbx-root-1"] = ["cid-dead", "exited", "root-1"]
    docker.start_error = b"Error response from daemon: No such image: registry.example/tasks/repo-fix:2026-11-20"
    backend = _docker_backend(docker)
    sid = await backend.provision(SandboxSpec(session_id="root-1", session_image=TASK_IMAGE))
    assert ["rm", "-f", "-v", "cid-dead"] in docker.calls
    assert docker.run_args() and backend.created(sid)


async def test_destroying_an_image_sandbox_stops_it_and_keeps_its_files():
    docker = ImageDocker(images={TASK_IMAGE: _image(), BUNDLE: _image()})
    backend = _docker_backend(docker)
    sid = await backend.provision(SandboxSpec(session_id="root-1", session_image=TASK_IMAGE))
    await backend.destroy(sid)
    assert ["stop", "cid-1"] in docker.calls and not any(c[:1] == ["rm"] for c in docker.calls)
    # A Stop on a worker that held no entry for it stops it all the same.
    docker.named["surogates-sbx-root-1"][1] = "running"
    await backend.stop_image_of_session("root-1")
    assert docker.named["surogates-sbx-root-1"][1] == "exited"


async def test_ssh_is_not_offered_in_a_session_image():
    docker = ImageDocker(images={TASK_IMAGE: _image(), BUNDLE: _image()})
    spec = SandboxSpec(session_id="r", session_image=TASK_IMAGE, ssh_key_material={"k": {"private_key": "x"}})
    with pytest.raises(SandboxUnavailableError, match="SSH"):
        await _docker_backend(docker).provision(spec)


async def test_a_container_of_the_session_being_created_is_waited_for_never_removed(monkeypatch):
    from surogates.sandbox import docker_image

    monkeypatch.setattr(docker_image, "_TAKE_OVER_S", 0.3)
    docker = ImageDocker(images={TASK_IMAGE: _image(), BUNDLE: _image()})
    docker.named["surogates-sbx-root-1"] = ["cid-other", "created", "root-1"]
    with pytest.raises(SandboxUnavailableError, match="never came to run"):
        await _docker_backend(docker).provision(SandboxSpec(session_id="root-1", session_image=TASK_IMAGE))
    assert not any(c[:1] in (["rm"], ["stop"]) for c in docker.calls)


async def test_a_running_container_whose_executor_never_answers_is_stopped_not_removed():
    docker = ImageDocker(images={TASK_IMAGE: _image(), BUNDLE: _image()})
    docker.named["surogates-sbx-root-1"] = ["cid-stuck", "running", "root-1"]
    with pytest.raises(SandboxUnavailableError, match="does not answer"):
        await _docker_backend(docker, _never_ready()).provision(SandboxSpec(session_id="root-1", session_image=TASK_IMAGE))
    assert ["stop", "cid-stuck"] in docker.calls and not any(c[:1] == ["rm"] for c in docker.calls)


async def test_a_container_another_worker_is_starting_is_taken_over(monkeypatch):
    from surogates.sandbox import docker_image

    monkeypatch.setattr(docker_image, "_TAKE_OVER_S", 2.0)
    docker = ImageDocker(images={TASK_IMAGE: _image(), BUNDLE: _image()})
    conflict = b'docker: Error response from daemon: Conflict. The container name "/surogates-sbx-root-1" is already in use'
    docker.run_answers = [(125, b"", conflict)]
    real_run = docker.run

    async def run(args):
        out = await real_run(args)
        if args[:2] == ["run", "-d"]:  # the other worker's container comes up meanwhile
            docker.named["surogates-sbx-root-1"] = ["cid-other", "running", "root-1"]
        return out
    docker.run = run
    backend = _docker_backend(docker)
    sid = await backend.provision(SandboxSpec(session_id="root-1", session_image=TASK_IMAGE))
    assert backend._entries[sid].container_id == "cid-other"
    # The other worker's container is never removed by this one.
    assert not any(c[:1] == ["rm"] for c in docker.calls)


async def test_a_run_that_fails_at_its_start_leaves_no_created_container():
    docker = ImageDocker(images={TASK_IMAGE: _image(), BUNDLE: _image()})
    docker.run_answers = [(125, b"", b"docker: Error response from daemon: invalid mount config")]
    with pytest.raises(SandboxUnavailableError, match="invalid mount"):
        await _docker_backend(docker).provision(SandboxSpec(session_id="root-1", session_image=TASK_IMAGE))
    assert ["rm", "-f", "-v", "surogates-sbx-root-1"] in docker.calls


async def test_a_missing_image_is_refused_where_pulls_are_off():
    docker = ImageDocker(images={BUNDLE: _image()})
    with pytest.raises(SandboxUnavailableError, match="pulls none"):
        await _docker_backend(docker, image_pull="never").provision(SandboxSpec(session_id="r", session_image=TASK_IMAGE))
    assert not any(c[:1] == ["pull"] for c in docker.calls)


async def test_a_missing_image_is_pulled_once_for_all_who_ask():
    docker = ImageDocker(images={BUNDLE: _image()})
    docker.pull_gate = asyncio.Event()
    backend = _docker_backend(docker)
    first = asyncio.ensure_future(backend.provision(SandboxSpec(session_id="a", session_image=TASK_IMAGE)))
    second = asyncio.ensure_future(backend.provision(SandboxSpec(session_id="b", session_image=TASK_IMAGE)))
    await docker.pull_started.wait()
    await asyncio.sleep(0.05)
    docker.pull_gate.set()
    await asyncio.gather(first, second)
    assert [c for c in docker.calls if c[:1] == ["pull"]] == [["pull", "--quiet", TASK_IMAGE]]


async def test_a_failed_pull_answers_those_who_waited_on_it_without_a_pull_each():
    docker = ImageDocker(images={BUNDLE: _image()}, pull_ok=False)
    docker.pull_gate = asyncio.Event()
    backend = _docker_backend(docker)
    first = asyncio.ensure_future(backend.provision(SandboxSpec(session_id="a", session_image=TASK_IMAGE)))
    second = asyncio.ensure_future(backend.provision(SandboxSpec(session_id="b", session_image=TASK_IMAGE)))
    await docker.pull_started.wait()
    await asyncio.sleep(0.05)
    docker.pull_gate.set()
    results = await asyncio.gather(first, second, return_exceptions=True)
    assert all(isinstance(r, SandboxUnavailableError) and "docker pull" in str(r) for r in results)
    assert len([c for c in docker.calls if c[:1] == ["pull"]]) == 1


async def test_a_pull_that_takes_too_long_is_refused():
    docker = ImageDocker(images={BUNDLE: _image()})
    docker.pull_gate = asyncio.Event()  # never set
    with pytest.raises(SandboxUnavailableError, match="took over"):
        await _docker_backend(docker, image_pull_timeout=0.05).provision(SandboxSpec(session_id="r", session_image=TASK_IMAGE))


@pytest.mark.parametrize("client, daemon", [("1.47", "1.51"), ("1.51", "1.47")])
async def test_a_client_or_daemon_before_image_mounts_is_refused(client, daemon):
    docker = ImageDocker(images={TASK_IMAGE: _image(), BUNDLE: _image()}, api=daemon, client_api=client)
    with pytest.raises(SandboxUnavailableError, match="Docker 28") as raised:
        await _docker_backend(docker).provision(SandboxSpec(session_id="r", session_image=TASK_IMAGE))
    assert raised.value.classification == "config"


async def test_no_bundle_no_session_image():
    docker = ImageDocker(images={TASK_IMAGE: _image()})
    with pytest.raises(SandboxUnavailableError, match="executor_bundle_image is not set"):
        await _docker_backend(docker, executor_bundle_image="").provision(SandboxSpec(session_id="r", session_image=TASK_IMAGE))


async def test_an_image_of_another_architecture_is_refused():
    docker = ImageDocker(images={TASK_IMAGE: _image(arch="arm64"), BUNDLE: _image()})
    with pytest.raises(SandboxUnavailableError, match="arm64"):
        await _docker_backend(docker).provision(SandboxSpec(session_id="r", session_image=TASK_IMAGE))


async def test_an_executor_that_never_comes_up_says_why_and_leaves_nothing():
    docker = ImageDocker(images={TASK_IMAGE: _image(), BUNDLE: _image()})
    docker.logs = b"exec /opt/surogates/bin/python3: no such file or directory\n"
    with pytest.raises(SandboxUnavailableError, match="no such file or directory.*glibc") as raised:
        await _docker_backend(docker, _never_ready()).provision(SandboxSpec(session_id="r", session_image=TASK_IMAGE))
    assert raised.value.classification == "readiness"
    assert ["rm", "-f", "-v", "cid-1"] in docker.calls


async def test_another_reason_gets_no_glibc_hint():
    docker = ImageDocker(images={TASK_IMAGE: _image(), BUNDLE: _image()})
    docker.logs = b"Traceback: ImportError: something else\n"
    with pytest.raises(SandboxUnavailableError) as raised:
        await _docker_backend(docker, _never_ready()).provision(SandboxSpec(session_id="r", session_image=TASK_IMAGE))
    assert "ImportError" in str(raised.value) and "glibc" not in str(raised.value)


async def test_an_image_whose_executor_exits_is_told_at_once_not_at_the_timeout():
    docker = ImageDocker(images={TASK_IMAGE: _image(), BUNDLE: _image()})
    docker.exited = True
    docker.logs = b"[FATAL tini (7)] exec /opt/surogates/bin/python3 failed: No such file or directory\n"
    backend = DockerSandbox(image="sbx-test:1", executor_port_base=33000, ready_timeout=60, network="sbx-net",
                            executor_bundle_image=BUNDLE, docker=docker, httpx_transport=_never_ready())
    started = asyncio.get_running_loop().time()
    with pytest.raises(SandboxUnavailableError, match="stopped before its executor became ready.*tini"):
        await backend.provision(SandboxSpec(session_id="r", session_image=TASK_IMAGE))
    assert asyncio.get_running_loop().time() - started < 10


async def test_a_cancelled_provision_leaves_no_container():
    docker = ImageDocker(images={TASK_IMAGE: _image(), BUNDLE: _image()})
    backend = DockerSandbox(image="sbx-test:1", executor_port_base=33000, ready_timeout=60, network="sbx-net",
                            executor_bundle_image=BUNDLE, docker=docker, httpx_transport=_never_ready())
    task = asyncio.ensure_future(backend.provision(SandboxSpec(session_id="r", session_image=TASK_IMAGE)))
    while not any(c[:2] == ["run", "-d"] for c in docker.calls):
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ["rm", "-f", "-v", "cid-1"] in docker.calls


async def test_a_default_sandbox_never_asks_whether_its_container_runs():
    docker = ImageDocker()
    with pytest.raises(SandboxUnavailableError, match="did not become ready"):
        await _docker_backend(docker, _never_ready()).provision(SandboxSpec(session_id="r"))
    assert not any("{{.State.Running}}" in c for c in docker.calls)


async def test_a_bad_reference_never_reaches_dockers_arguments():
    docker = ImageDocker(images={BUNDLE: _image()})
    with pytest.raises(SandboxUnavailableError, match="Not an image reference"):
        await _docker_backend(docker).provision(SandboxSpec(session_id="r", session_image="--privileged"))
    assert docker.calls == [["info", "--format", "{{.NCPU}} {{.CPUCfsQuota}} {{.MemoryLimit}} {{.SwapLimit}} {{.PidsLimit}}"]]


def test_the_worker_builds_the_image_settings_into_its_backend(monkeypatch):
    from surogates.config import SandboxSettings

    monkeypatch.setenv("SUROGATES_SANDBOX_EXECUTOR_BUNDLE_IMAGE", BUNDLE)
    monkeypatch.setenv("SUROGATES_SANDBOX_DOCKER_IMAGE_PULL", "never")
    monkeypatch.setenv("SUROGATES_SANDBOX_DOCKER_IMAGE_PULL_TIMEOUT", "30")
    monkeypatch.setenv("SUROGATES_SANDBOX_ALLOWED_IMAGES", '["registry.example/tasks/*"]')
    settings = SandboxSettings()
    backend = DockerSandbox.from_settings(settings)
    assert (backend._bundle_image, backend._image_pull, backend._image_pull_timeout) == (BUNDLE, "never", 30)
    assert settings.allowed_images == ["registry.example/tasks/*"]


async def test_a_docker_client_given_up_on_is_killed(tmp_path, monkeypatch):
    """A pull past its time (a provision cancelled) leaves no docker client running on."""
    from surogates.sandbox.docker import _RealDocker

    fake = tmp_path / "docker"
    fake.write_text("#!/bin/sh\necho $$ > " + str(tmp_path / "pid") + "\nexec sleep 30\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ.get('PATH', '')}")
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(_RealDocker().run(["pull", "x"]), timeout=0.5)
    pid = int((tmp_path / "pid").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


# ── a session that names no image gets what it got before ──────────────────


async def test_a_session_without_an_image_gets_the_same_container_as_before():
    """The full ``docker run`` of a default session, as master builds it (357ec341b)."""
    docker = ImageDocker()
    await _docker_backend(docker).provision(SandboxSpec(session_id="root-1", env={"ORG_ID": "o"}))
    args = docker.run_args()
    token = _env(args)["TOOL_EXECUTOR_TOKEN"]
    assert args == [
        "run", "-d", "--rm", "-p", "127.0.0.1:33000:8071", "--network", "sbx-net",
        "--add-host", "host.docker.internal:host-gateway", "--label", "app=surogates-sandbox",
        "--label", "surogates.session_id=root-1",
        "--memory", str(8 * 2**30), "--memory-swap", str(8 * 2**30), "--cpus", "4", "--pids-limit", "4096",
        "-e", f"TOOL_EXECUTOR_TOKEN={token}", "-e", "WORKSPACE_DIR=/workspace", "-e", "TOOL_EXECUTOR_REQUIRE_FUSE=0",
        "-e", "ORG_ID=o", "sbx-test:1",
    ]
    assert not any(c[:1] in (["version"], ["pull"]) or c[:2] == ["image", "inspect"] for c in docker.calls)


# ── the executor's commands in a session's own image ────────────────────────


def test_commands_keep_the_images_environment_in_its_image():
    from surogates.tools.workspace_io import local

    environ = {"PATH": "/usr/local/go/bin:/usr/bin", "PYTHONPATH": "/app/lib", "GOPATH": "/go", "HOME": "/root",
               "MCP_PROXY_TOKEN": "secret", "USER_ID": "u", "SUROGATES_IMAGE_SESSION": "1",
               "SUROGATES_IMAGE_ENV_KEYS": "PATH,PYTHONPATH,GOPATH"}
    with patch.dict(os.environ, environ, clear=True):
        env = local._build_child_env()
    assert env == {"PATH": "/usr/local/go/bin:/usr/bin", "PYTHONPATH": "/app/lib", "GOPATH": "/go", "HOME": "/root"}


def test_commands_outside_a_session_image_keep_only_what_they_kept():
    from surogates.tools.workspace_io import local

    environ = {"PATH": "/usr/bin", "PYTHONPATH": "/x", "GOPATH": "/go", "HOME": "/home/sandbox",
               "SUROGATES_IMAGE_ENV_KEYS": "PYTHONPATH,GOPATH"}  # not an image session: the list means nothing
    with patch.dict(os.environ, environ, clear=True):
        env = local._build_child_env()
    assert env == {"PATH": "/usr/bin", "HOME": "/home/sandbox"}


async def test_a_command_in_a_session_image_has_its_users_home(tmp_path):
    from surogates.tools.workspace_io.local import LocalWorkspaceIO

    environ = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": "/root", "SUROGATES_IMAGE_SESSION": "1",
               "SUROGATES_IMAGE_ENV_KEYS": "PATH"}
    with patch.dict(os.environ, environ, clear=True):
        result = await LocalWorkspaceIO(workspace_path=str(tmp_path)).run(
            "echo $HOME; echo ${GIT_CONFIG_GLOBAL:-none}; pwd", workdir=None, timeout=10)
    assert result.output.split() == ["/root", "none", str(tmp_path)]


async def test_a_command_elsewhere_still_has_the_workspace_as_home(tmp_path):
    from surogates.tools.workspace_io.local import LocalWorkspaceIO

    with patch.dict(os.environ, {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": "/root"}, clear=True):
        result = await LocalWorkspaceIO(workspace_path=str(tmp_path)).run("echo $HOME", workdir=None, timeout=10)
    assert result.output.strip() == str(tmp_path)


def test_commands_get_the_images_own_values_of_the_names_the_harness_holds():
    import base64

    from surogates.tools.workspace_io import local

    shadowed = base64.b64encode(json.dumps({"PROJECT_DIR": "/app", "USER_ID": "1000"}).encode()).decode()
    environ = {"PATH": "/usr/bin", "HOME": "/root", "PROJECT_DIR": "", "USER_ID": "harness-user",
               "SUROGATES_IMAGE_SESSION": "1", "SUROGATES_IMAGE_ENV_KEYS": "PATH", "SUROGATES_IMAGE_ENV_SHADOWED": shadowed}
    with patch.dict(os.environ, environ, clear=True):
        env = local._build_child_env()
    assert env == {"PATH": "/usr/bin", "HOME": "/root", "PROJECT_DIR": "/app", "USER_ID": "1000"}


def test_a_background_command_in_a_session_image_has_only_the_images_environment_and_no_login_shell():
    from surogates.tools.utils import process_registry as pr

    environ = {"PATH": "/usr/local/go/bin:/usr/bin", "MCP_PROXY_TOKEN": "secret", "SUROGATES_IMAGE_SESSION": "1"}
    with patch.dict(os.environ, environ, clear=True):
        assert pr._base_env() == {}
        argv = pr._shell_argv("go test ./...")
    assert argv[1:] == ["-c", "go test ./..."] and argv[0] in ("/bin/bash", "/bin/sh")
    with patch.dict(os.environ, {"PATH": "/usr/bin", "SHELL": "/bin/sh"}, clear=True):
        assert pr._shell_argv("x") == ["/bin/sh", "-lic", "x"]
        assert dict(pr._base_env()) == {"PATH": "/usr/bin", "SHELL": "/bin/sh"}


async def test_a_background_command_runs_with_the_images_path(tmp_path):
    from surogates.tools.workspace_io.local import LocalWorkspaceIO
    from surogates.tools.utils.process_registry import process_registry

    environ = {"PATH": "/opt/tool/bin:" + os.environ.get("PATH", "/usr/bin:/bin"), "HOME": "/root",
               "MCP_PROXY_TOKEN": "secret", "SUROGATES_IMAGE_SESSION": "1", "SUROGATES_IMAGE_ENV_KEYS": "PATH"}
    with patch.dict(os.environ, environ, clear=True):
        started = await LocalWorkspaceIO(workspace_path=str(tmp_path)).start(
            "echo P=$PATH; echo T=${MCP_PROXY_TOKEN:-none}", workdir=None, task_id="t", pty=False,
            notify_on_complete=False, watcher_interval=None)
    for _ in range(100):
        polled = process_registry.poll(started["session_id"])
        if polled.get("status") != "running":
            break
        await asyncio.sleep(0.05)
    out = process_registry.read_log(started["session_id"], offset=0, limit=50)["output"]
    assert "P=/opt/tool/bin:" in out and "T=none" in out


def test_search_finds_the_bundles_ripgrep_in_a_session_image(tmp_path):
    from surogates.tools.workspace_io import local

    (tmp_path / "bin").mkdir()
    rg = tmp_path / "bin" / "rg"
    rg.write_text("#!/bin/sh\n")
    rg.chmod(0o755)
    environ = {"PATH": "/nonexistent", "SUROGATES_IMAGE_SESSION": "1", "SUROGATES_EXECUTOR_HOME": str(tmp_path)}
    with patch.dict(os.environ, environ, clear=True):
        assert local._find_ripgrep() == str(rg)
    with patch.dict(os.environ, {**environ, "SUROGATES_IMAGE_SESSION": "0"}, clear=True):
        assert local._find_ripgrep() is None


# ── what the model is told ──────────────────────────────────────────────────


def _context(config: dict) -> str:
    from datetime import datetime, timezone

    from surogates.harness.prompt import PromptBuilder
    from surogates.session.models import Session
    from surogates.tenant.context import TenantContext

    now = datetime.now(timezone.utc)
    session = Session(id=uuid4(), user_id=uuid4(), org_id=uuid4(), agent_id="agent-1", channel="api",
                      status="active", config={"workspace_path": "/data/ws", **config}, created_at=now, updated_at=now)
    tenant = TenantContext(org_id=session.org_id, user_id=session.user_id, org_config={}, user_preferences={},
                           permissions=frozenset(), asset_root="/tmp/test")
    return PromptBuilder(tenant, session=session, available_tools={"terminal", "read_file"})._context_section()


def test_a_session_image_is_told_its_folder_and_its_rules():
    section = _context({"sandbox_image": TASK_IMAGE, "sandbox_workdir": "/app"})
    assert "- **Workspace**: `/app` (your working directory), in this session's own container" in section
    assert "This session runs in its own container" in section
    assert "`$HOME`" not in section


def test_a_session_without_an_image_is_told_what_it_was_told():
    section = _context({})
    assert "- **Workspace**: `$HOME` (your working directory)" in section
    assert "own container" not in section


# ── reaped by the container's own clock, on any worker ─────────────────────


class Activity(httpx.AsyncBaseTransport):
    """The executors of the test's containers: /healthz ready, /retire answering as set."""

    def __init__(self, seen: dict):
        self.seen = seen
        self.asked: list[float] = []

    async def handle_async_request(self, request):
        if request.url.path == "/retire":
            from surogates.sandbox.docker_image import session_token

            assert request.headers["authorization"] == f"Bearer {session_token('s')}"
            body = json.loads(request.content)
            self.asked.append(body["min_idle_seconds"])
            seen = dict(self.seen)
            seen["retired"] = (not seen["running"] and not seen["background_running"]
                               and seen["idle_seconds"] >= body["min_idle_seconds"])
            return httpx.Response(200, json=seen)
        return httpx.Response(200, text="ok")


def _listing(docker: ImageDocker, rows: dict[str, tuple[str, str]]):
    """rows: cid -> (state, finished_at); every container is session "s"'s."""
    real = docker.run

    async def run(args):
        joined = " ".join(args)
        if args[:2] == ["ps", "-a"] and any(a == "label=app=surogates-image-sandbox" for a in args):
            docker.calls.append(args)
            return 0, "".join(f"{cid} {st}\n" for cid, (st, _f) in rows.items()).encode(), b""
        if args[:1] == ["inspect"] and "FinishedAt" in joined:
            docker.calls.append(args)
            return 0, f"s {rows[args[-1]][1]}\n".encode(), b""
        if args[:1] == ["inspect"] and "NetworkSettings.Ports" in joined:
            docker.calls.append(args)
            return 0, b'{"8071/tcp":[{"HostIp":"127.0.0.1","HostPort":"33999"}]}\ns\n', b""
        return await real(args)
    docker.run = run


async def test_the_reaper_removes_only_containers_at_rest_and_idle_by_their_own_clock():
    from surogates.sandbox.docker_image import deployment_id

    old = "2026-01-01T00:00:00.123456789Z"
    rows = {"idle": ("running", "0001-01-01T00:00:00Z"), "stopped-long": ("exited", old),
            "stopped-now": ("exited", "2999-01-01T00:00:00Z")}
    docker = ImageDocker()
    _listing(docker, rows)
    retire = Activity({"idle_seconds": 900, "running": 0, "background_running": 0})
    backend = _docker_backend(docker, retire)

    async def at_rest(session):
        return True
    assert await backend.reap_images(at_rest, idle_seconds=300) == [("s", "idle"), ("s", "stopped-long")]
    assert [c[-1] for c in docker.calls if c[:1] == ["rm"]] == ["idle", "stopped-long"]
    assert retire.asked == [300]
    # Only this deployment's, whatever network they are on.
    ps = next(c for c in docker.calls if c[:2] == ["ps", "-a"])
    assert f"label=surogates.deployment={deployment_id()}" in ps

    async def busy(session):
        return False
    assert await backend.reap_images(busy, idle_seconds=300) == []


@pytest.mark.parametrize("seen", [
    {"idle_seconds": 30, "running": 0, "background_running": 0},
    {"idle_seconds": 900, "running": 1, "background_running": 0},
    {"idle_seconds": 900, "running": 0, "background_running": 2},
])
async def test_a_container_still_in_use_by_its_own_clock_is_kept(seen):
    docker = ImageDocker()
    _listing(docker, {"c": ("running", "0001-01-01T00:00:00Z")})
    backend = _docker_backend(docker, Activity(seen))

    async def at_rest(session):
        return True
    assert await backend.reap_images(at_rest, idle_seconds=300) == []
    assert not any(c[:1] == ["rm"] for c in docker.calls)


async def test_no_bundle_no_reaping_call():
    docker = ImageDocker()
    backend = _docker_backend(docker, executor_bundle_image="")

    async def at_rest(session):
        return True
    assert await backend.reap_images(at_rest, idle_seconds=300) == [] and docker.calls == []


async def test_the_pool_leaves_image_sandboxes_to_the_backends_reaper_and_a_shutdown_keeps_them():
    from surogates.sandbox.base import SandboxStatus
    from surogates.sandbox.pool import SandboxPool

    class Backend:
        def __init__(self):
            self.n, self.destroyed, self.reaped, self.gone, self.forgotten = 0, [], [], set(), []

        async def provision(self, spec):
            self.n += 1
            return f"sbx-{self.n}"

        async def status(self, sid):
            return SandboxStatus.TERMINATED if sid in self.gone else SandboxStatus.RUNNING

        async def destroy(self, sid):
            self.destroyed.append(sid)

        def forget(self, sid):
            self.forgotten.append(sid)

        def created(self, sid):
            return True

        async def reap_images(self, at_rest, *, idle_seconds):
            return list(self.reaped)

    backend = Backend()
    pool = SandboxPool(backend)
    await pool.ensure("img", SandboxSpec(session_image=TASK_IMAGE))
    await pool.ensure("plain", SandboxSpec())

    async def at_rest(key):
        return True
    assert await pool.reap_at_rest(at_rest, idle_seconds=0, image_idle_seconds=60) == ["plain"]
    assert backend.destroyed == ["sbx-2"] and pool.sandbox_of("img") == "sbx-1"
    # Another worker's reaper removed it: this pool lets its entry go, and destroys nothing.
    backend.gone.add("sbx-1")
    backend.reaped = [("img", "cid-1")]
    assert await pool.reap_at_rest(at_rest, idle_seconds=0, image_idle_seconds=60) == ["img"]
    assert pool.sandbox_of("img") is None and backend.destroyed == ["sbx-2"] and backend.forgotten == ["sbx-1"]
    # With the default sandboxes' reaping off, the session images are still reaped.
    backend.reaped = []
    await pool.ensure("img3", SandboxSpec(session_image=TASK_IMAGE))
    assert await pool.reap_at_rest(at_rest, idle_seconds=float("inf"), image_idle_seconds=60) == []
    # A worker's shutdown never ends a session's own image (another worker may work in it).
    await pool.destroy_all()
    assert backend.destroyed == ["sbx-2"] and pool.sandbox_of("img3") is None


async def test_a_new_container_is_reported_once():
    from surogates.sandbox.pool import SandboxPool

    class Backend:
        async def provision(self, spec):
            return "sbx-1"

        def created(self, sid):
            return True

    pool = SandboxPool(Backend())
    await pool.ensure("img", SandboxSpec(session_id="r", session_image=TASK_IMAGE))
    assert pool.image_new("img") and not pool.image_new("img")


async def test_the_session_is_told_only_when_it_had_worked_in_a_container_before():
    from surogates.harness import tool_exec

    calls = [SimpleNamespace(id=5, data={"name": "todo"}), SimpleNamespace(id=7, data={"name": "terminal"})]

    async def get_events(sid, *, types, until=None):
        return [e for e in calls if until is None or e.id <= until]
    store = SimpleNamespace(get_events=get_events)
    session = SimpleNamespace(id=uuid4())
    assert await tool_exec._worked_in_a_sandbox_before(store, session, str(session.id), 9)
    assert not await tool_exec._worked_in_a_sandbox_before(store, session, str(session.id), 6)
    calls[:] = [SimpleNamespace(id=3, data={"name": "mcp__crm__search"})]
    assert not await tool_exec._worked_in_a_sandbox_before(store, session, str(session.id), 9)


def test_the_entry_clears_the_images_control_names_before_the_executor_reads_them():
    from surogates.image_entry import prepare

    environ = {"SUROGATES_IMAGE_ENV_UNSET": "PROJECT_DIR,TERMINAL_TIMEOUT", "PROJECT_DIR": "/app", "TERMINAL_TIMEOUT": "",
               "PATH": "/x"}
    prepare(environ)
    assert environ == {"SUROGATES_IMAGE_ENV_UNSET": "PROJECT_DIR,TERMINAL_TIMEOUT", "PATH": "/x",
                       "SUROGATES_HOME": "/tmp/.surogates", "SUROGATES_CHECKPOINT_DIR": "/tmp/.surogates/checkpoints"}


def test_the_executors_activity_counts_calls_not_the_workers_drains(tmp_path):
    from fastapi.testclient import TestClient

    from surogates.sandbox import executor_server

    app = executor_server.create_app(token="tok", workspace=str(tmp_path), require_fuse=False)
    with patch.object(executor_server, "execute_in_child", AsyncMock(return_value='{"ok": true}')):
        client = TestClient(app)
        auth = {"Authorization": "Bearer tok"}
        assert client.get("/activity").status_code == 401
        client.post("/execute", json={"name": "read_file", "args": {"path": "x"}}, headers=auth)
        first = client.get("/activity", headers=auth).json()
        assert first["running"] == 0 and first["idle_seconds"] < 5 and first["background_running"] == 0


def test_the_executor_retires_only_unused_and_then_refuses_calls(tmp_path):
    from fastapi.testclient import TestClient

    from surogates.sandbox import executor_server

    app = executor_server.create_app(token="tok", workspace=str(tmp_path), require_fuse=False)
    auth = {"Authorization": "Bearer tok"}
    with patch.object(executor_server, "execute_in_child", AsyncMock(return_value='{"ok": true}')):
        client = TestClient(app)
        assert client.post("/retire", json={"min_idle_seconds": 0}).status_code == 401
        assert client.post("/retire", json={"min_idle_seconds": 3600}, headers=auth).json()["retired"] is False
        assert client.post("/execute", json={"name": "read_file", "args": {}}, headers=auth).status_code == 200
        assert client.post("/retire", json={"min_idle_seconds": 0}, headers=auth).json()["retired"] is True
        assert client.post("/execute", json={"name": "read_file", "args": {}}, headers=auth).status_code == 410


async def test_a_retired_executor_is_an_unavailable_sandbox():
    from aiohttp import web

    from surogates.sandbox._executor_client import ExecutorHTTPClient

    async def gone(request):
        return web.Response(status=410, text="this sandbox is retired")
    app = web.Application()
    app.router.add_post("/execute", gone)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        with pytest.raises(SandboxUnavailableError, match="retired"):
            await ExecutorHTTPClient().execute(host="127.0.0.1", port=port, token="t", name="x", args_str="{}", timeout=5)
    finally:
        await runner.cleanup()


def test_only_commands_a_session_waits_on_keep_a_sandbox_in_use(tmp_path):
    from fastapi.testclient import TestClient

    from surogates.sandbox import executor_server
    from surogates.tools.utils.process_registry import process_registry

    forgotten = SimpleNamespace(exited=False, notify_on_complete=False, watch_lines=False)
    waited = SimpleNamespace(exited=False, notify_on_complete=True, watch_lines=False)
    app = executor_server.create_app(token="tok", workspace=str(tmp_path), require_fuse=False)
    with patch.dict(process_registry._running, {"a": forgotten}, clear=True):
        assert TestClient(app).get("/activity", headers={"Authorization": "Bearer tok"}).json()["background_running"] == 0
    with patch.dict(process_registry._running, {"a": forgotten, "b": waited}, clear=True):
        assert TestClient(app).get("/activity", headers={"Authorization": "Bearer tok"}).json()["background_running"] == 1
