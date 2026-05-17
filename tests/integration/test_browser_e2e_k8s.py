"""Opt-in end-to-end browser test against a real Kubernetes cluster.

Setup before running:

    kind create cluster --name surogates-test
    PUSH=0 ./images/build.sh latest browser
    kind load docker-image \
        ghcr.io/invergent-ai/surogates-agent-browser:latest \
        --name surogates-test
    helm install surogates <PATH_TO_CHART> \
        --namespace surogates --create-namespace \
        --set browser.backend=kubernetes \
        --set browser.image=ghcr.io/invergent-ai/surogates-agent-browser:latest

Run:

    uv run pytest -m browser_e2e_k8s tests/integration/test_browser_e2e_k8s.py -v

Skipped by default.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator

import httpx
import pytest

from surogates.browser.base import BrowserEndpoint, BrowserSpec, BrowserStatus
from surogates.browser.client import KernelBrowserClient
from surogates.browser.kubernetes import K8sBrowserBackend


pytestmark = pytest.mark.browser_e2e_k8s

NAMESPACE = os.environ.get("BROWSER_K8S_NAMESPACE", "surogates")
SERVICE_ACCOUNT = os.environ.get("BROWSER_K8S_SA", "surogates-browser")
LOCAL_REST_PORT = int(os.environ.get("BROWSER_K8S_LOCAL_REST_PORT", "39101"))
IMAGE = os.environ.get(
    "BROWSER_E2E_IMAGE",
    "ghcr.io/invergent-ai/surogates-agent-browser:latest",
)


@pytest.fixture()
async def backend() -> AsyncIterator[K8sBrowserBackend]:
    yield K8sBrowserBackend(
        namespace=NAMESPACE,
        service_account=SERVICE_ACCOUNT,
        pod_ready_timeout=120,
        image=IMAGE,
    )


@pytest.fixture()
async def browser(
    backend: K8sBrowserBackend,
) -> AsyncIterator[tuple[str, BrowserEndpoint]]:
    browser_id, endpoint = await backend.provision(
        BrowserSpec(image=IMAGE, pod_ready_timeout=120),
        session_id="e2e-session",
        org_id="e2e-org",
        user_id="e2e-user",
    )
    try:
        yield browser_id, endpoint
    finally:
        await backend.destroy(browser_id)


@pytest.fixture()
async def rest_url(
    backend: K8sBrowserBackend,
    browser: tuple[str, BrowserEndpoint],
) -> AsyncIterator[str]:
    browser_id, _endpoint = browser
    service_name = backend._pods[browser_id].service_name
    proc = await asyncio.create_subprocess_exec(
        "kubectl",
        "-n",
        NAMESPACE,
        "port-forward",
        f"svc/{service_name}",
        f"{LOCAL_REST_PORT}:10001",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with httpx.AsyncClient(timeout=1.0) as http:
            for _ in range(120):
                if proc.returncode is not None:
                    stderr = b""
                    if proc.stderr is not None:
                        stderr = await proc.stderr.read()
                    raise RuntimeError(
                        "kubectl port-forward exited: "
                        f"{stderr.decode(errors='replace')}",
                    )
                try:
                    resp = await http.get(
                        f"http://127.0.0.1:{LOCAL_REST_PORT}/spec.json"
                    )
                    if resp.status_code == 200:
                        break
                except Exception:
                    await asyncio.sleep(0.25)
            else:
                raise RuntimeError("kubectl port-forward did not become ready")

        yield f"http://127.0.0.1:{LOCAL_REST_PORT}"
    finally:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()


async def test_provision_creates_running_pod(
    backend: K8sBrowserBackend,
    browser: tuple[str, BrowserEndpoint],
) -> None:
    browser_id, _endpoint = browser
    assert await backend.status(browser_id) == BrowserStatus.RUNNING


async def test_navigate_through_port_forward(rest_url: str) -> None:
    async with KernelBrowserClient(rest_url=rest_url) as client:
        result = await client.navigate("https://example.com")
        assert "Example" in result["title"]


async def test_find_by_session_returns_endpoint(
    backend: K8sBrowserBackend,
    browser: tuple[str, BrowserEndpoint],
) -> None:
    _browser_id, _endpoint = browser
    found = await backend.find_by_session("e2e-session")
    assert found is not None
