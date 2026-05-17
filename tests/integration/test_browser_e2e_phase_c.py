"""Phase C end-to-end browser smoke test against a real cluster.

Setup:

    kind create cluster --name surogates-test
    helm install surogates <PATH_TO_CHART> \
        --namespace surogates --create-namespace \
        --set browser.backend=kubernetes
    PUSH=0 ./images/build.sh latest browser
    kind load docker-image \
        ghcr.io/invergent-ai/surogates-agent-browser:latest \
        --name surogates-test

Run with a reachable API server and service-account token:

    BROWSER_E2E_API_BASE=http://localhost:8000 \
    BROWSER_E2E_TOKEN=... \
    uv run pytest -m browser_e2e_k8s \
        tests/integration/test_browser_e2e_phase_c.py -v -s

Skipped by the default pytest marker expression.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator

import pytest
import websockets
from httpx import AsyncClient


pytestmark = pytest.mark.browser_e2e_k8s

API_BASE = os.environ.get("BROWSER_E2E_API_BASE", "http://localhost:8000")
TOKEN = os.environ.get("BROWSER_E2E_TOKEN", "")
MODEL = os.environ.get("BROWSER_E2E_MODEL", "gpt-4.1-mini")


@pytest.fixture()
async def session_with_browser() -> AsyncIterator[str]:
    if not TOKEN:
        pytest.skip("BROWSER_E2E_TOKEN is required")

    headers = {"Authorization": f"Bearer {TOKEN}"}
    async with AsyncClient(base_url=API_BASE, timeout=30.0) as client:
        created = await client.post(
            "/v1/sessions",
            json={"model": MODEL},
            headers=headers,
        )
        assert created.status_code in {200, 201}, created.text
        session_id = created.json()["id"]

        sent = await client.post(
            f"/v1/sessions/{session_id}/messages",
            json={
                "content": "Open https://example.com in the browser, then stop.",
            },
            headers=headers,
        )
        assert sent.status_code in {200, 202}, sent.text

        await _wait_for_browser_provisioned(client, session_id, headers)
        yield session_id


async def _wait_for_browser_provisioned(
    client: AsyncClient,
    session_id: str,
    headers: dict[str, str],
) -> None:
    deadline = asyncio.get_running_loop().time() + 120
    last_event_id = 0
    while asyncio.get_running_loop().time() < deadline:
        events = await client.get(
            f"/v1/sessions/{session_id}/events/poll",
            params={"after": last_event_id},
            headers=headers,
        )
        assert events.status_code == 200, events.text
        for event in events.json().get("events", []):
            event_id = event.get("id")
            if isinstance(event_id, int):
                last_event_id = max(last_event_id, event_id)
            if event.get("type") == "browser.provisioned":
                return
        await asyncio.sleep(1.0)
    raise AssertionError("browser.provisioned was not observed within 120s")


async def test_state_endpoint_returns_live_after_provision(
    session_with_browser: str,
) -> None:
    async with AsyncClient(base_url=API_BASE, timeout=30.0) as client:
        response = await client.get(
            f"/v1/sessions/{session_with_browser}/browser/state",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "live"


async def test_acquire_then_release_round_trip(session_with_browser: str) -> None:
    headers = {"Authorization": f"Bearer {TOKEN}"}
    async with AsyncClient(base_url=API_BASE, timeout=30.0) as client:
        acquired = await client.post(
            f"/v1/sessions/{session_with_browser}/browser/control",
            json={"action": "acquire"},
            headers=headers,
        )
        assert acquired.status_code == 200, acquired.text
        assert acquired.json()["outcome"] in {"granted", "refreshed"}

        held = await client.get(
            f"/v1/sessions/{session_with_browser}/browser/state",
            headers=headers,
        )
        assert held.status_code == 200, held.text
        assert held.json()["status"] == "user-control"

        released = await client.post(
            f"/v1/sessions/{session_with_browser}/browser/control",
            json={"action": "release"},
            headers=headers,
        )
        assert released.status_code == 200, released.text


async def test_live_view_html_is_served(session_with_browser: str) -> None:
    async with AsyncClient(base_url=API_BASE, timeout=30.0) as client:
        response = await client.get(
            f"/v1/sessions/{session_with_browser}/browser/live/",
            params={"token": TOKEN},
        )
    assert response.status_code == 200, response.text
    assert b"<html" in response.content.lower() or b"<!doctype" in response.content.lower()


async def test_websocket_connects_and_receives_server_frame(
    session_with_browser: str,
) -> None:
    ws_url = (
        API_BASE.replace("http", "ws", 1)
        + f"/v1/sessions/{session_with_browser}/browser/live/ws"
        + f"?token={TOKEN}"
    )
    async with websockets.connect(ws_url, subprotocols=["binary"]) as websocket:
        first = await asyncio.wait_for(websocket.recv(), timeout=5.0)

    assert isinstance(first, bytes | bytearray)
    assert len(first) > 0
