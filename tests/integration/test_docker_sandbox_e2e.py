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
