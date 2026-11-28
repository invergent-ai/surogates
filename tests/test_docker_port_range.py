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
        self.inspect_garbage = False
        self.cancel_next_run = False
        self._default_runs = 0

    async def run(self, args: list[str]) -> tuple[int, bytes, bytes]:
        if args[:2] == ["run", "-d"]:
            await asyncio.sleep(0)  # Docker takes a moment: other starts run meanwhile
            if self.cancel_next_run:
                self.cancel_next_run = False
                self.calls.append(args)
                raise asyncio.CancelledError
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
        if args[:4] == ["inspect", "--type", "container", "--format"] and "HostConfig.PortBindings" in args[4]:
            self.calls.append(args)
            lines, missing = [], False
            for cid in args[5:]:
                box = self.boxes.get(cid)
                if box is None:
                    missing = True
                    continue
                port = box["port"]
                bindings = json.dumps({"8071/tcp": [{"HostIp": box["host"], "HostPort": str(port) if port else ""}]})
                worker = box["labels"].get("surogates.worker", "<no value>")
                lines.append("not json" if self.inspect_garbage else f"{worker}\t{bindings}")
            return (1 if missing else 0), "".join(f"{line}\n" for line in lines).encode(), (b"No such object" if missing else b"")
        if args[:1] == ["inspect"] and "HostConfig.PortBindings" in " ".join(args):
            self.calls.append(args)
            name, c = self._by_id(args[-1])
            if c is None:
                return 1, b"", b"No such object"
            box = self.boxes[c[0]]
            asked = {"8071/tcp": [{"HostIp": box["host"], "HostPort": str(box["port"]) if box["port"] else ""}]}
            # Where Docker published it: a port of its choosing for one that asked for none; none for a stopped one.
            published = {"8071/tcp": [{"HostIp": box["host"], "HostPort": str(box["port"] or 41000)}]} \
                if c[1] == "running" else {}
            running = "true" if c[1] == "running" else "false"
            return 0, f"/{name}\n{running}\n{json.dumps(asked)}\n{json.dumps(published)}\n".encode(), b""
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


async def test_default_and_image_sandboxes_share_the_range_without_overlap():
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


async def test_any_containers_port_in_this_range_is_skipped_whoever_started_it():
    docker = PortDocker()
    other = _range_backend(docker, count=3)
    await other.provision(_image_spec("root-1"))  # 20000, by another worker of the same range
    mine = _range_backend(docker, count=3)
    await mine.provision(_image_spec("root-2"))
    assert docker.published() == ["127.0.0.1:20000:8071", "127.0.0.1:20001:8071"]


async def test_starts_in_flight_hold_their_ports_when_the_range_comes_round():
    """Three starts at once on a range of two: the third finds both ports taken by starts Docker does not list yet
    and is refused; without the start-in-flight set it would come round to a port being started."""
    class Forgetful(set):
        def add(self, item):
            pass

    for forget in (False, True):
        docker = PortDocker()
        backend = _range_backend(docker, count=2)
        if forget:
            backend._ports_starting = Forgetful()
        results = await asyncio.gather(*(backend.provision(_image_spec(f"root-{i}")) for i in range(3)),
                                       return_exceptions=True)
        ports = [p.split(":")[1] for p in docker.published()]
        if forget:
            assert len(set(ports)) < len(ports)  # the collision the set exists to prevent
        else:
            assert sorted(ports) == ["20000", "20001"]
            assert sum(isinstance(r, SandboxUnavailableError) for r in results) == 1


async def test_a_freed_port_is_the_last_one_given_again():
    docker = PortDocker()
    backend = _range_backend(docker, count=3)
    first = await backend.provision(SandboxSpec(session_id="plain-1"))   # 20000
    await backend.destroy(first)                                        # removed: 20000 free
    await backend.provision(SandboxSpec(session_id="plain-2"))          # 20001, not 20000 again at once
    await backend.provision(SandboxSpec(session_id="plain-3"))          # 20002
    await backend.provision(SandboxSpec(session_id="plain-4"))          # 20000, the range gone round
    assert [p.split(":")[1] for p in docker.published()] == ["20000", "20001", "20002", "20000"]


async def test_a_start_cut_off_mid_run_keeps_its_port_set_aside():
    # Docker may still create the container of a docker run whose client was killed.
    docker = PortDocker()
    docker.cancel_next_run = True
    backend = _range_backend(docker, count=3)
    with pytest.raises(asyncio.CancelledError):
        await backend.provision(_image_spec("root-1"))
    assert 20000 in backend._ports_set_aside and backend._ports_starting == set()
    await backend.provision(_image_spec("root-2"))
    assert docker.published()[-1] == "127.0.0.1:20001:8071"


async def test_a_port_held_outside_the_sandboxes_is_skipped_by_the_next_start():
    docker = PortDocker()
    docker.port_conflict = True
    backend = _range_backend(docker, count=3)
    with pytest.raises(SandboxUnavailableError):
        await backend.provision(SandboxSpec(session_id="plain-1"))
    docker.port_conflict = False
    await backend.provision(SandboxSpec(session_id="plain-2"))
    assert [p.split(":")[1] for p in docker.published()] == ["20000", "20001"]


async def test_an_unreadable_binding_is_refused_never_read_as_free():
    docker = PortDocker()
    backend = _range_backend(docker, count=3)
    await backend.provision(_image_spec("root-1"))
    docker.inspect_garbage = True
    with pytest.raises(SandboxUnavailableError, match="could not be read"):
        await backend.provision(_image_spec("root-2"))


async def test_a_container_made_before_range_mode_is_read_where_docker_published_it():
    docker = PortDocker()
    before = DockerSandbox(image="sbx-test:1", executor_port_base=20000, ready_timeout=1, network="sbx-net",
                           executor_bundle_image=BUNDLE, image_deployment="stack-a", docker=docker,
                           httpx_transport=_ready())
    await before.provision(_image_spec("root-1"))      # asked for no port: Docker's choice, 41000 here
    after = _range_backend(docker, count=3)
    sid = await after.provision(_image_spec("root-1"))  # the same session: its running container taken over
    assert after._entries[sid].host_port == 41000
    assert docker.published() == ["127.0.0.1::8071"]


async def test_a_stopped_image_container_starts_again_on_its_own_port():
    docker = PortDocker()
    backend = _range_backend(docker, count=3)
    first = await backend.provision(_image_spec("root-1"))
    await backend.destroy(first)                         # stopped, kept
    again = await backend.provision(_image_spec("root-1"))
    assert backend._entries[again].host_port == 20000
    assert docker.published() == ["127.0.0.1:20000:8071"]
