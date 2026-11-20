"""A Docker sandbox runs held to its spec's limits: memory (swap included), CPUs and processes.

Before, ``docker run`` got none of them, and a command that grew without end
in one session's container took the whole host's memory.
"""

from __future__ import annotations

import logging

import pytest

from surogates.config import SandboxSettings
from surogates.sandbox.base import SandboxSpec, SandboxUnavailableError
from surogates.sandbox.docker import DEFAULT_PIDS_LIMIT, DockerHost, DockerSandbox, docker_host, limit_args
from tests.test_docker_sandbox import FakeDocker, _backend, healthz_transport  # noqa: F401


class FakeDaemon(FakeDocker):
    """A Docker daemon that answers ``docker info``: its CPUs and the limits it applies."""

    def __init__(self, info: bytes = b"64 true true true true\n", run_warning: bytes = b"") -> None:
        super().__init__()
        self.info = info
        self.run_warning = run_warning

    async def run(self, args):
        if args[0] == "info":
            self.calls.append(args)
            return 0, self.info, b""
        code, out, err = await super().run(args)
        if args[:2] == ["run", "-d"] and code == 0:
            err = self.run_warning
        return code, out, err


def _flag(args: list[str], name: str) -> str | None:
    return args[args.index(name) + 1] if name in args else None


async def _run_call(spec: SandboxSpec, healthz_transport, docker: FakeDocker | None = None, **kw) -> list[str]:
    docker = docker or FakeDaemon()
    backend = _backend(docker, healthz_transport, **kw)
    try:
        await backend.provision(spec)
    finally:
        await backend.aclose()
    return next(c for c in docker.calls if c[:2] == ["run", "-d"])


async def test_the_default_spec_runs_at_its_limits(healthz_transport):
    run = await _run_call(SandboxSpec(session_id="root-1"), healthz_transport)
    assert _flag(run, "--memory") == str(8 * 2**30)
    # No swap past the limit: the kernel kills inside the container, the host does not swap.
    assert _flag(run, "--memory-swap") == str(8 * 2**30)
    assert _flag(run, "--cpus") == "4"
    assert _flag(run, "--pids-limit") == str(DEFAULT_PIDS_LIMIT) == "4096"
    # Flags, then the image: docker reads nothing after the image as its own.
    assert run.index("--memory") < run.index("sbx-test:1") == len(run) - 1


async def test_a_specs_own_limits_are_the_containers(healthz_transport):
    spec = SandboxSpec(session_id="root-1", memory_limit="2Gi", cpu_limit="500m")
    run = await _run_call(spec, healthz_transport, pids_limit=256)
    assert _flag(run, "--memory") == _flag(run, "--memory-swap") == "2147483648"
    assert _flag(run, "--cpus") == "0.5"
    assert _flag(run, "--pids-limit") == "256"


async def test_the_settings_reach_the_container_the_worker_runs(healthz_transport, monkeypatch):
    """SUROGATES_SANDBOX_DEFAULT_*_LIMIT through the spec the worker builds, the pids setting through the backend."""
    from surogates.sandbox.base import default_sandbox_spec

    monkeypatch.setenv("SUROGATES_SANDBOX_DEFAULT_MEMORY_LIMIT", "3Gi")
    monkeypatch.setenv("SUROGATES_SANDBOX_DEFAULT_CPU_LIMIT", "1")
    monkeypatch.setenv("SUROGATES_SANDBOX_DOCKER_PIDS_LIMIT", "512")
    spec = default_sandbox_spec()
    spec.session_id = "root-1"
    built = DockerSandbox.from_settings(SandboxSettings())
    assert built._pids_limit == 512
    run = await _run_call(spec, healthz_transport, pids_limit=built._pids_limit)
    assert _flag(run, "--memory") == str(3 * 2**30)
    assert _flag(run, "--cpus") == "1"
    assert _flag(run, "--pids-limit") == "512"
    await built.aclose()


def test_the_worker_builds_its_docker_backend_from_the_settings(monkeypatch):
    """The worker's backend is ``from_settings``, and every docker setting reaches it."""
    from pathlib import Path

    import surogates.orchestrator.worker as worker

    assert "DockerSandbox.from_settings(" in Path(worker.__file__).read_text()
    assert SandboxSettings().docker_pids_limit == DEFAULT_PIDS_LIMIT
    for key, value in {
        "DOCKER_IMAGE": "sbx:7", "DOCKER_EXECUTOR_PORT_BASE": "27700", "DOCKER_READY_TIMEOUT": "9",
        "DOCKER_NETWORK": "sbx-net", "DOCKER_PIDS_LIMIT": "300",
    }.items():
        monkeypatch.setenv(f"SUROGATES_SANDBOX_{key}", value)
    built = DockerSandbox.from_settings(SandboxSettings(), mcp_proxy_url="http://proxy", storage_settings="storage")
    assert (built._image, built._port_base, built._ready_timeout, built._network, built._pids_limit) == (
        "sbx:7", 27700, 9, "sbx-net", 300,
    )
    assert (built._mcp_proxy_url, built._storage) == ("http://proxy", "storage")


async def test_the_cpus_are_at_most_the_daemons_not_the_workers(healthz_transport):
    """Docker Desktop / colima: the Mac has 10 CPUs, Docker's VM 2, and Docker refuses --cpus 4."""
    run = await _run_call(SandboxSpec(session_id="root-1"), healthz_transport, docker=FakeDaemon(b"2 true true true true\n"))
    assert _flag(run, "--cpus") == "2"


async def test_a_daemon_without_cpu_quota_gets_no_cpus_flag_and_says_so(healthz_transport, caplog):
    """Rootless Docker without the cpu controller refuses any --cpus: the sandbox still starts, held to memory."""
    with caplog.at_level(logging.WARNING, logger="surogates.sandbox.docker"):
        run = await _run_call(
            SandboxSpec(session_id="root-1"), healthz_transport, docker=FakeDaemon(b"8 false true true true\n"),
        )
    assert "--cpus" not in run
    assert _flag(run, "--memory") == str(8 * 2**30)
    assert "cannot apply a CPU limit" in caplog.text


async def test_limits_the_daemon_cannot_apply_are_logged(healthz_transport, caplog):
    daemon = FakeDaemon(b"8 true true false true\n", run_warning=b"WARNING: Your kernel does not support swap limit capabilities")
    with caplog.at_level(logging.WARNING, logger="surogates.sandbox.docker"):
        await _run_call(SandboxSpec(session_id="root-1"), healthz_transport, docker=daemon)
    assert "cannot apply a swap limit" in caplog.text
    assert "does not support swap limit" in caplog.text


async def test_docker_info_is_asked_once_per_backend(healthz_transport):
    daemon = FakeDaemon()
    backend = _backend(daemon, healthz_transport)
    await backend.provision(SandboxSpec(session_id="root-1"))
    await backend.provision(SandboxSpec(session_id="root-2"))
    assert [c[0] for c in daemon.calls].count("info") == 1
    await backend.aclose()


async def test_a_daemon_that_does_not_answer_info_is_taken_to_apply_every_limit_and_asked_again(healthz_transport):
    class Silent(FakeDaemon):
        silent = True

        async def run(self, args):
            if args[0] == "info" and self.silent:
                self.calls.append(args)
                return 1, b"", b"Cannot connect to the Docker daemon"
            return await super().run(args)

    docker = Silent(b"2 true true true true\n")
    host = await docker_host(docker)
    assert host.cpu_quota and host.memory_limit and host.swap_limit and host.pids_limit and not host.answered
    docker.calls.clear()
    backend = _backend(docker, healthz_transport)
    await backend.provision(SandboxSpec(session_id="root-1"))
    # Once the daemon answers, its CPUs count: a guess from a daemon still starting is not kept.
    docker.silent = False
    await backend.provision(SandboxSpec(session_id="root-2"))
    runs = [c for c in docker.calls if c[:2] == ["run", "-d"]]
    assert _flag(runs[-1], "--cpus") == "2"
    assert [c[0] for c in docker.calls].count("info") == 2
    await backend.aclose()


@pytest.mark.parametrize(
    ("memory", "expected"),
    [
        ("8Gi", 8 * 2**30),
        ("512Mi", 512 * 2**20),
        ("1.5Gi", int(1.5 * 2**30)),
        ("2G", 2 * 10**9),
        ("750M", 750 * 10**6),
        ("6Mi", 6 * 2**20),
        ("1073741824", 2**30),
    ],
)
def test_memory_quantities_are_bytes(memory, expected):
    args = limit_args(SandboxSpec(memory_limit=memory, cpu_limit=""), pids_limit=0)
    assert args == ["--memory", str(expected), "--memory-swap", str(expected)]


@pytest.mark.parametrize(
    ("cpu", "expected"),
    [("4", "4"), ("500m", "0.5"), ("1.5", "1.5"), ("2500m", "2.5"), ("16", "8"), ("1m", "0.01"), ("1.23456789", "1.235")],
)
def test_cpu_quantities_are_cpus_at_most_the_daemons(cpu, expected):
    args = limit_args(SandboxSpec(memory_limit="", cpu_limit=cpu), pids_limit=0, host=DockerHost(cpus=8))
    assert args == ["--cpus", expected]


@pytest.mark.parametrize("none", ["", "0", "0Gi", "0m", "0.0"])
def test_an_empty_or_zero_limit_sets_none(none):
    assert limit_args(SandboxSpec(memory_limit=none, cpu_limit=none), pids_limit=0) == []
    assert limit_args(SandboxSpec(memory_limit=none, cpu_limit=none), pids_limit=-1) == []


@pytest.mark.parametrize(
    ("field", "bad", "message"),
    [
        ("memory_limit", "lots", "not a quantity"),
        ("memory_limit", "Gi", "not a quantity"),
        ("memory_limit", "-1Gi", "not a quantity"),
        ("memory_limit", "8 GB", "not a quantity"),
        ("memory_limit", "nan", "not a quantity"),
        ("memory_limit", "inf", "not a quantity"),
        # Half a byte: Docker would take the 0 it rounds to as no limit.
        ("memory_limit", "500m", "under the 6Mi"),
        ("memory_limit", "4Mi", "under the 6Mi"),
        ("cpu_limit", "two", "not a quantity"),
        ("cpu_limit", "-1", "not a quantity"),
    ],
)
async def test_a_limit_docker_cannot_hold_fails_the_provision_before_any_container(field, bad, message, healthz_transport):
    docker = FakeDaemon()
    backend = _backend(docker, healthz_transport)
    with pytest.raises(SandboxUnavailableError, match=message):
        await backend.provision(SandboxSpec(session_id="root-1", **{field: bad}))
    assert [c for c in docker.calls if c[0] != "info"] == []
    await backend.aclose()
