"""``sandbox.docker_image_port_mode = "range"``: every sandbox of a worker, a session's own image's included, asks for a
fixed loopback port of the worker's range, held while its container exists; the default ("docker") is unchanged."""

from __future__ import annotations

import asyncio
import json
import re

import pytest

from surogates.sandbox.base import SandboxSpec, SandboxUnavailableError
from surogates.sandbox.docker import DockerSandbox
from tests.test_session_sandbox_image import BUNDLE, TASK_IMAGE, ImageDocker, _flag, _image, _ready


class PortDocker(ImageDocker):
    """ImageDocker that also records each container's labels and the port it asked for (``-p``), lists containers by
    label (repeated filters ANDed, stopped ones included) and answers ``HostConfig.PortBindings``, which Docker keeps
    whether or not the network published the port."""

    def __init__(self, **kw) -> None:
        super().__init__(images={TASK_IMAGE: _image(), BUNDLE: _image()}, **kw)
        self.boxes: dict[str, dict] = {}
        self.ps_error = b""
        self.port_conflict = False
        self._default_runs = 0

    async def run(self, args: list[str]) -> tuple[int, bytes, bytes]:
        if args[:2] == ["run", "-d"]:
            if self.port_conflict:
                self.calls.append(args)
                return 125, b"", b"Bind for 127.0.0.1:20000 failed: port is already allocated"
            if "--name" in args:
                code, out, err = await super().run(args)
            else:  # a default sandbox: a container of its own each time
                self.calls.append(args)
                self._default_runs += 1
                code, out, err = 0, f"dflt-{self._default_runs}\n".encode(), b""
            if code == 0:
                cid = out.decode().split()[0]
                published = _flag(args, "-p")[0]
                host, port, inner = published.split(":")
                labels = dict(label.split("=", 1) for label in _flag(args, "--label"))
                self.boxes[cid] = {"labels": labels, "port": int(port) if port else None, "host": host}
            return code, out, err
        if args[:2] == ["ps", "-aq"] and all(a.startswith("label=") for a in _flag(args, "--filter")):
            self.calls.append(args)
            if self.ps_error:
                return 1, b"", self.ps_error
            wanted = [f.removeprefix("label=").split("=", 1) for f in _flag(args, "--filter")]
            ids = [cid for cid, box in self.boxes.items() if all(box["labels"].get(k) == v for k, v in wanted)]
            return 0, "".join(f"{cid}\n" for cid in ids).encode(), b""
        if args[:3] == ["inspect", "--format", "{{json .HostConfig.PortBindings}}"]:
            self.calls.append(args)
            lines, missing = [], False
            for cid in args[3:]:
                box = self.boxes.get(cid)
                if box is None:
                    missing = True
                    continue
                port = box["port"]
                lines.append(json.dumps({"8071/tcp": [{"HostIp": box["host"], "HostPort": str(port) if port else ""}]}))
            return (1 if missing else 0), "".join(f"{line}\n" for line in lines).encode(), (b"No such object" if missing else b"")
        if args[:1] == ["inspect"] and "HostConfig.PortBindings" in " ".join(args):
            self.calls.append(args)
            name, c = self._by_id(args[-1])
            if c is None:
                return 1, b"", b"No such object"
            box = self.boxes[c[0]]
            ports = {"8071/tcp": [{"HostIp": box["host"], "HostPort": str(box["port"])}]}
            return 0, f"/{name}\n{'true' if c[1] == 'running' else 'false'}\n{json.dumps(ports)}\n".encode(), b""
        if args[:1] == ["rm"]:
            code, out, err = await super().run(args)
            self.boxes.pop(args[-1], None)
            return code, out, err
        return await super().run(args)

    def published(self) -> list[str]:
        return [_flag(c, "-p")[0] for c in self.calls if c[:2] == ["run", "-d"]]


def _range_backend(docker: PortDocker, count: int = 3, **kw) -> DockerSandbox:
    return DockerSandbox(image="sbx-test:1", executor_port_base=20000, ready_timeout=1, network="sbx-net",
                         executor_bundle_image=BUNDLE, image_deployment="stack-a", image_port_mode="range",
                         executor_port_count=count, docker=docker, httpx_transport=_ready(), **kw)


def _image_spec(session: str) -> SandboxSpec:
    return SandboxSpec(session_id=session, session_image=TASK_IMAGE)


# -- the default: unchanged ---------------------------------------------------------------------------------------

async def test_by_default_docker_picks_an_image_sessions_port_and_nothing_lists_ports():
    docker = PortDocker()
    backend = DockerSandbox(image="sbx-test:1", executor_port_base=20000, ready_timeout=1, network="sbx-net",
                            executor_bundle_image=BUNDLE, image_deployment="stack-a", docker=docker,
                            httpx_transport=_ready())
    await backend.provision(_image_spec("root-1"))
    assert docker.published() == ["127.0.0.1::8071"]
    assert not any("HostConfig.PortBindings" in " ".join(c) for c in docker.calls)
    assert any("NetworkSettings.Ports" in " ".join(c) for c in docker.calls)
    assert not any(c[:2] == ["ps", "-aq"] and "label=app=surogates-image-sandbox" in c for c in docker.calls)


def test_the_settings_default_to_dockers_ports():
    from surogates.config import SandboxSettings

    settings = SandboxSettings()
    assert settings.docker_image_port_mode == "docker" and settings.docker_executor_port_count == 0


@pytest.mark.parametrize("kw", [
    {"image_port_mode": "range", "executor_port_count": 0},
    {"image_port_mode": "range", "executor_port_count": 70000},
    {"image_port_mode": "fixed", "executor_port_count": 3},
])
def test_a_range_needs_ports_and_a_known_mode(kw):
    with pytest.raises(ValueError):
        DockerSandbox(image="sbx-test:1", executor_port_base=20000, docker=PortDocker(), **kw)


def test_the_worker_takes_the_mode_and_the_range_from_its_settings():
    from surogates.config import SandboxSettings

    settings = SandboxSettings(docker_image_port_mode="range", docker_executor_port_base=21000,
                               docker_executor_port_count=50)
    backend = DockerSandbox.from_settings(settings)
    assert backend._port_range == range(21000, 21050)


# -- "range" ------------------------------------------------------------------------------------------------------

async def test_an_image_session_asks_for_a_fixed_port_of_the_range():
    docker = PortDocker()
    backend = _range_backend(docker)
    sid = await backend.provision(_image_spec("root-1"))
    assert docker.published() == ["127.0.0.1:20000:8071"]
    assert backend._entries[sid].host_port == 20000
    # Its port is read as asked for: a network that publishes nothing records nothing else.
    assert any("HostConfig.PortBindings" in " ".join(c) for c in docker.calls if c[:1] == ["inspect"])


async def test_ports_held_by_any_sandbox_container_are_skipped_default_ones_included():
    docker = PortDocker()
    backend = _range_backend(docker, count=4)
    await backend.provision(SandboxSpec(session_id="plain-1"))         # a default sandbox: 20000
    await backend.provision(_image_spec("root-1"))                     # 20001
    await backend.provision(SandboxSpec(session_id="plain-2"))         # 20002
    assert docker.published() == ["127.0.0.1:20000:8071", "127.0.0.1:20001:8071", "127.0.0.1:20002:8071"]


async def test_a_stopped_image_container_keeps_its_port_and_a_full_range_is_refused():
    docker = PortDocker()
    backend = _range_backend(docker, count=2)
    a = await backend.provision(_image_spec("root-1"))
    await backend.provision(_image_spec("root-2"))
    await backend.destroy(a)  # stopped, kept for the session's next call: still asks for 20000
    with pytest.raises(SandboxUnavailableError, match="no free sandbox port in this worker's range 20000-20001"):
        await backend.provision(_image_spec("root-3"))
    assert docker.published() == ["127.0.0.1:20000:8071", "127.0.0.1:20001:8071"]


async def test_a_port_is_given_again_once_its_container_is_removed():
    docker = PortDocker()
    backend = _range_backend(docker, count=1)
    first = await backend.provision(SandboxSpec(session_id="plain-1"))
    with pytest.raises(SandboxUnavailableError, match="no free sandbox port"):
        await backend.provision(SandboxSpec(session_id="plain-2"))
    await backend.destroy(first)  # a default sandbox is removed at its end
    await backend.provision(SandboxSpec(session_id="plain-2"))
    assert docker.published() == ["127.0.0.1:20000:8071", "127.0.0.1:20000:8071"]


async def test_starts_at_the_same_moment_never_ask_for_one_port():
    docker = PortDocker()
    backend = _range_backend(docker, count=5)
    await asyncio.gather(*(backend.provision(_image_spec(f"root-{i}")) for i in range(4)),
                         backend.provision(SandboxSpec(session_id="plain-1")))
    ports = [p.split(":")[1] for p in docker.published()]
    assert sorted(ports) == ["20000", "20001", "20002", "20003", "20004"]


async def test_a_port_held_outside_the_sandboxes_is_an_error_not_another_port():
    docker = PortDocker()
    docker.port_conflict = True
    backend = _range_backend(docker)
    with pytest.raises(SandboxUnavailableError, match="port 20000 of this worker's sandbox port range is held"):
        await backend.provision(SandboxSpec(session_id="plain-1"))
    assert len([c for c in docker.calls if c[:2] == ["run", "-d"]]) == 1
    assert backend._ports_starting == set()


async def test_docker_unsure_of_its_containers_is_refused_never_read_as_free():
    docker = PortDocker()
    docker.ps_error = b"Cannot connect to the Docker daemon"
    backend = _range_backend(docker)
    with pytest.raises(SandboxUnavailableError, match="docker ps failed"):
        await backend.provision(_image_spec("root-1"))
    assert docker.published() == []


async def test_another_workers_container_in_this_range_is_skipped():
    docker = PortDocker()
    other = _range_backend(docker, count=3)
    await other.provision(_image_spec("root-1"))  # 20000, by another worker of the same range
    mine = _range_backend(docker, count=3)
    await mine.provision(_image_spec("root-2"))
    assert docker.published() == ["127.0.0.1:20000:8071", "127.0.0.1:20001:8071"]
