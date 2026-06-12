"""Live K8s integration for the persistent executor daemon (opt-in).

Provisions a real sandbox pod, asserts warm-tool latency and batch
overlap, then verifies unreachable-daemon classification.

Requires a cluster reachable via kubeconfig plus S3 settings for a
scratch workspace prefix:

    SUROGATES_K8S_INTEGRATION=1 \
    SANDBOX_NAMESPACE=surogates-sandboxes \
    SANDBOX_SERVICE_ACCOUNT=surogates-sandbox \
    SANDBOX_IMAGE=ghcr.io/invergent-ai/surogates-agent-sandbox:dev \
    S3FS_IMAGE=ghcr.io/invergent-ai/surogates-s3fs:latest \
    S3_ENDPOINT=http://... S3_ACCESS_KEY=... S3_SECRET_KEY=... \
    S3_WORKSPACE_REF=s3://bucket/executor-integration-test/ \
    uv run pytest tests/test_executor_integration.py -m live -v

Pod IPs are only routable from inside the cluster (where the worker
runs in production).  When running this test from a dev machine, set
``SANDBOX_PORT_FORWARD=1`` to tunnel the daemon port through
``kubectl port-forward`` instead — same HTTP path, just through the
API server.  In-pod reachability of the raw pod IP is independently
proven by the kubelet readiness probe (the pod cannot go Ready without
it).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from types import SimpleNamespace

import pytest

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("SUROGATES_K8S_INTEGRATION") != "1",
        reason="set SUROGATES_K8S_INTEGRATION=1 to run against a live cluster",
    ),
]


@pytest.fixture()
def live_sandbox():
    from surogates.sandbox.kubernetes import K8sSandbox

    return K8sSandbox(
        namespace=os.environ.get("SANDBOX_NAMESPACE", "surogates-sandboxes"),
        service_account=os.environ.get("SANDBOX_SERVICE_ACCOUNT", "surogates-sandbox"),
        pod_ready_timeout=120,
        executor_port=8071,
        storage_settings=SimpleNamespace(
            endpoint=os.environ["S3_ENDPOINT"],
            access_key=os.environ["S3_ACCESS_KEY"],
            secret_key=os.environ["S3_SECRET_KEY"],
            region=os.environ.get("S3_REGION", ""),
        ),
        s3fs_image=os.environ["S3FS_IMAGE"],
        s3_endpoint=os.environ["S3_ENDPOINT"],
    )


async def _port_forward(namespace: str, pod_name: str, remote_port: int):
    """Tunnel *remote_port* of *pod_name* to a random local port.

    Returns ``(process, local_port)``; the caller terminates the process.
    """
    proc = await asyncio.create_subprocess_exec(
        "kubectl", "port-forward", "-n", namespace,
        f"pod/{pod_name}", f"0:{remote_port}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    line = (await asyncio.wait_for(proc.stdout.readline(), 15)).decode()
    m = re.search(r"127\.0\.0\.1:(\d+)", line)
    if not m:
        proc.terminate()
        raise RuntimeError(f"kubectl port-forward failed: {line!r}")
    return proc, int(m.group(1))


async def test_latency_overlap_and_failure_classification(live_sandbox):
    from surogates.sandbox.base import (
        Resource,
        SandboxSpec,
        SandboxUnavailableError,
    )

    spec = SandboxSpec(
        image=os.environ["SANDBOX_IMAGE"],
        resources=[Resource(
            source_ref=os.environ["S3_WORKSPACE_REF"],
            mount_path="/workspace",
        )],
    )
    sandbox_id = await live_sandbox.provision(spec)
    pf_proc = None
    try:
        if os.environ.get("SANDBOX_PORT_FORWARD") == "1":
            entry = live_sandbox._pods[sandbox_id]
            pf_proc, local_port = await _port_forward(
                entry.namespace, entry.pod_name, live_sandbox._executor_port,
            )
            entry.pod_ip = "127.0.0.1"
            live_sandbox._executor_port = local_port
        list_args = json.dumps({"pattern": "*"})

        # First call may pay cold geesefs metadata (S3 LIST) — allow
        # headroom, but it must be nowhere near the old ~5s exec floor.
        start = time.monotonic()
        result = json.loads(
            await live_sandbox.execute(sandbox_id, "list_files", list_args),
        )
        first = time.monotonic() - start
        assert result.get("matches") is not None or "error" not in result
        assert first < 5, f"first warm call too slow: {first:.2f}s"

        start = time.monotonic()
        await live_sandbox.execute(sandbox_id, "list_files", list_args)
        single = time.monotonic() - start
        assert single < 1.0, f"warm list_files too slow: {single:.2f}s"

        # Batch of 4 must overlap: wall ~= max, not sum.
        start = time.monotonic()
        await asyncio.gather(*[
            live_sandbox.execute(sandbox_id, "list_files", list_args)
            for _ in range(4)
        ])
        batch = time.monotonic() - start
        assert batch < single * 2.5 + 0.5, (
            f"batch serialized: 4 calls took {batch:.2f}s vs single {single:.2f}s"
        )

        # Kill the pod out-of-band -> next call must classify as
        # SandboxUnavailableError (connect failure), not hang.
        api = await live_sandbox._get_api()
        entry = live_sandbox._pods[sandbox_id]
        await api.delete_namespaced_pod(
            entry.pod_name, entry.namespace, grace_period_seconds=0,
        )
        await asyncio.sleep(5)
        with pytest.raises(SandboxUnavailableError):
            await live_sandbox.execute(sandbox_id, "list_files", list_args)
    finally:
        if pf_proc is not None:
            pf_proc.terminate()
            try:
                await asyncio.wait_for(pf_proc.wait(), 10)
            except asyncio.TimeoutError:
                pf_proc.kill()
        try:
            await live_sandbox.destroy(sandbox_id)
        except Exception:
            pass
        await live_sandbox.aclose()
