"""Opt-in end-to-end browser smoke tests.

Run explicitly with:

    uv run pytest -m browser_e2e tests/integration/test_browser_e2e.py -v

Requires Docker and the kernel-images Chromium image.
"""

from __future__ import annotations

import os

import pytest

from surogates.browser.base import BrowserSpec
from surogates.browser.client import KernelBrowserClient
from surogates.browser.process import ProcessBrowserBackend


pytestmark = pytest.mark.browser_e2e

E2E_IMAGE = os.environ.get(
    "BROWSER_E2E_IMAGE",
    "ghcr.io/onkernel/chromium-headful:stable",
)


@pytest.fixture()
async def backend():
    browser_backend = ProcessBrowserBackend(
        image=E2E_IMAGE,
        rest_port_base=39000,
        cdp_port_base=39100,
        live_view_port_base=39200,
    )
    yield browser_backend


@pytest.fixture()
async def browser(backend):
    browser_id, endpoint = await backend.provision(
        BrowserSpec(image=E2E_IMAGE, pod_ready_timeout=60)
    )
    try:
        yield browser_id, endpoint
    finally:
        await backend.destroy(browser_id)


async def test_navigate_and_get_state(browser) -> None:
    _browser_id, endpoint = browser
    async with KernelBrowserClient(rest_url=endpoint.rest_url) as client:
        result = await client.navigate("https://example.com")
        assert "Example" in result["title"]

        state = await client.get_state(interactive_only=True)
        assert any(node["role"] == "link" for node in state["tree"])


async def test_screenshot_returns_png(browser) -> None:
    _browser_id, endpoint = browser
    async with KernelBrowserClient(rest_url=endpoint.rest_url) as client:
        await client.navigate("https://example.com")
        result = await client.screenshot()
        assert result["png_bytes"].startswith(b"\x89PNG")
        assert len(result["png_bytes"]) > 1000
