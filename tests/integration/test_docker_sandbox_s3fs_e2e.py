"""Opt-in: DockerSandbox s3fs mode writes to R2 (real Docker + real bucket).

Skipped unless Docker is present AND SUROGATES_TEST_DOCKER_S3FS=1 AND R2 creds
are in the environment. Run locally with creds exported from config.dev.yaml:

    SUROGATES_TEST_DOCKER_S3FS=1 \
    R2_ENDPOINT=... R2_KEY=... R2_SECRET=... R2_BUCKET=... \
    pytest tests/integration/test_docker_sandbox_s3fs_e2e.py -v
"""

from __future__ import annotations

import json
import os
import shutil
from types import SimpleNamespace

import pytest

from surogates.sandbox.base import Resource, SandboxSpec, SandboxStatus
from surogates.sandbox.docker import DockerSandbox

_REQUIRED = ("R2_ENDPOINT", "R2_KEY", "R2_SECRET", "R2_BUCKET")

pytestmark = pytest.mark.skipif(
    shutil.which("docker") is None
    or os.environ.get("SUROGATES_TEST_DOCKER_S3FS") != "1"
    or not all(os.environ.get(k) for k in _REQUIRED),
    reason="requires Docker, SUROGATES_TEST_DOCKER_S3FS=1, and R2_* creds",
)

_IMAGE = os.environ.get(
    "SUROGATES_TEST_SANDBOX_IMAGE",
    "ghcr.io/invergent-ai/surogates-agent-sandbox:latest",
)


async def test_s3fs_write_reaches_r2(tmp_path):
    bucket = os.environ["R2_BUCKET"]
    prefix = "itest/docker-s3fs"  # disjoint test prefix
    storage = SimpleNamespace(
        endpoint=os.environ["R2_ENDPOINT"],
        access_key=os.environ["R2_KEY"],
        secret_key=os.environ["R2_SECRET"],
        region="auto",
    )
    backend = DockerSandbox(
        image=_IMAGE, executor_port_base=34100, ready_timeout=120,
        storage_settings=storage,
    )
    spec = SandboxSpec(
        session_id="00000000-0000-0000-0000-0000000000ff",
        workspace_path="/workspace",
        resources=[Resource(
            source_ref=f"s3://{bucket}/{prefix}/sessions/root",
            mount_path="/workspace",
        )],
        timeout=60,
    )
    sid = None
    try:
        sid = await backend.provision(spec)
        assert await backend.status(sid) == SandboxStatus.RUNNING

        marker = "r2-roundtrip-ok"
        result = json.loads(await backend.execute(
            sid, "terminal",
            json.dumps({"command": f"echo {marker} > /workspace/itest.txt && sync"}),
        ))
        assert result.get("exit_code", 0) == 0

        # Read it back through a fresh listing in the same container.
        out = json.loads(await backend.execute(
            sid, "terminal", json.dumps({"command": "cat /workspace/itest.txt"})))
        assert marker in (out.get("output") or "")
    finally:
        if sid is not None:
            await backend.destroy(sid)
        await backend.aclose()
