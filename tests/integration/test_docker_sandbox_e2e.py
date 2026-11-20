"""Opt-in end-to-end test for DockerSandbox against a real Docker daemon.

Skipped unless Docker is available AND SUROGATES_TEST_DOCKER_SANDBOX=1 is set,
so CI without a daemon (or without the sandbox image pulled) stays green.

Run locally with:
    SUROGATES_TEST_DOCKER_SANDBOX=1 pytest tests/integration/test_docker_sandbox_e2e.py -v
"""

from __future__ import annotations

import json
import os
import shutil

import pytest

from surogates.sandbox.base import SandboxSpec, SandboxStatus
from surogates.sandbox.docker import DockerSandbox

pytestmark = pytest.mark.skipif(
    shutil.which("docker") is None
    or os.environ.get("SUROGATES_TEST_DOCKER_SANDBOX") != "1",
    reason="requires Docker and SUROGATES_TEST_DOCKER_SANDBOX=1",
)

_IMAGE = os.environ.get(
    "SUROGATES_TEST_SANDBOX_IMAGE",
    "ghcr.io/invergent-ai/surogates-agent-sandbox:latest",
)


async def test_provision_execute_workspace_destroy(tmp_path):
    backend = DockerSandbox(image=_IMAGE, executor_port_base=34000, ready_timeout=120)
    sid = None
    try:
        spec = SandboxSpec(
            session_id="00000000-0000-0000-0000-0000000000ee",
            workspace_path=str(tmp_path),
            timeout=60,
        )
        sid = await backend.provision(spec)
        assert await backend.status(sid) == SandboxStatus.RUNNING

        # Write a file through the terminal tool into the bind-mounted workspace.
        result = json.loads(await backend.execute(
            sid, "terminal", json.dumps({"command": "echo hello > /workspace/out.txt"}),
        ))
        assert result.get("exit_code", 0) == 0

        # The file is visible on the host bind-mount.
        assert (tmp_path / "out.txt").read_text().strip() == "hello"
    finally:
        if sid is not None:
            await backend.destroy(sid)
        await backend.aclose()


async def _inspect(fmt: str, container_id: str) -> str:
    import asyncio

    proc = await asyncio.create_subprocess_exec(
        "docker", "inspect", "--format", fmt, container_id,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, _ = await proc.communicate()
    return out.decode().strip()


async def test_a_memory_hog_is_killed_at_the_limit_and_the_sandbox_keeps_serving(tmp_path):
    """A command that grows without end dies at the sandbox's memory limit, in seconds, with a tool result
    that says so; the host keeps its memory and the sandbox serves the next call."""
    import time

    backend = DockerSandbox(
        image=_IMAGE, executor_port_base=int(os.environ.get("SUROGATES_TEST_SANDBOX_PORT_BASE", "34100")),
        ready_timeout=120, pids_limit=256, network=os.environ.get("SUROGATES_TEST_SANDBOX_NETWORK", "bridge"),
    )
    sid = None
    tmp_path.chmod(0o777)  # the sandbox's user enters it, whoever runs the test
    try:
        spec = SandboxSpec(
            session_id=os.environ.get("SUROGATES_TEST_SANDBOX_SESSION", "00000000-0000-0000-0000-0000000000ef"),
            workspace_path=str(tmp_path), timeout=240, memory_limit="1Gi", cpu_limit="1",
        )
        sid = await backend.provision(spec)
        container = backend._entries[sid].container_id
        limits = await _inspect(
            "{{.HostConfig.Memory}} {{.HostConfig.MemorySwap}} {{.HostConfig.NanoCpus}} {{.HostConfig.PidsLimit}}",
            container,
        )
        assert limits == f"{2**30} {2**30} 1000000000 256"

        hog = "python3 -c 'x = []\nwhile True: x.append(bytearray(64 * 2**20))'"
        started = time.monotonic()
        result = json.loads(await backend.execute(
            sid, "terminal", json.dumps({"command": hog, "timeout": 200}),
        ))
        took = time.monotonic() - started
        assert took < 60, f"the hog ran {took:.0f}s: it was not stopped at the limit"
        assert result["exit_code"] in (137, -9), result
        assert "ran out of memory" in result.get("exit_code_meaning", ""), result
        assert await _inspect("{{.State.Running}}", container) == "true"
        # The kernel's OOM kill in the container's cgroup (v2), not some other SIGKILL.
        events = json.loads(await backend.execute(
            sid, "terminal", json.dumps({"command": "cat /sys/fs/cgroup/memory.events 2>/dev/null || true"}),
        ))["output"]
        if "oom_kill" in events:
            assert int(events.split("oom_kill ")[1].split()[0]) >= 1, events

        # The sandbox still serves.
        again = json.loads(await backend.execute(sid, "terminal", json.dumps({"command": "echo still here"})))
        assert again["exit_code"] == 0 and again["output"] == "still here"

        # The process limit holds: threads stop being made well before a fork bomb's thousands.
        threads = (
            "python3 -c 'import threading, time\nn = 0\ntry:\n"
            "    while n < 5000:\n        threading.Thread(target=time.sleep, args=(3,), daemon=True).start(); n += 1\n"
            "except RuntimeError:\n    pass\nprint(n)'"
        )
        made = json.loads(await backend.execute(sid, "terminal", json.dumps({"command": threads})))
        assert made["exit_code"] == 0, made
        assert int(made["output"].split()[-1]) < 256
    finally:
        if sid is not None:
            await backend.destroy(sid)
        await backend.aclose()
