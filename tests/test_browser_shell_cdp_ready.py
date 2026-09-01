"""The shell must tolerate CDP coming up after the browser reports ready.

``ProcessBrowserBackend._wait_ready`` polls the kernel REST API on :10001.
Chrome's debug port on :9222 opens later, so between those two moments the
registry holds a browser whose CDP endpoint still refuses connections. Every
earlier consumer went through the REST API and never saw the gap; the shell
dials CDP directly and does.
"""

from __future__ import annotations

import httpx
import pytest

from surogates.api.routes.browser import _cdp_browser_ws_url

SOCKET = "ws://127.0.0.1:31002/devtools/browser/abc-123"


def _transport(refusals: int) -> httpx.MockTransport:
    """Refuse the first ``refusals`` calls, the way a not-yet-open port does."""

    state = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        if state["calls"] <= refusals:
            raise httpx.ConnectError("All connection attempts failed")
        return httpx.Response(200, json={"webSocketDebuggerUrl": SOCKET})

    transport = httpx.MockTransport(handler)
    transport.state = state  # type: ignore[attr-defined]
    return transport


async def test_returns_the_socket_when_cdp_is_already_up() -> None:
    transport = _transport(refusals=0)
    async with httpx.AsyncClient(transport=transport) as client:
        assert await _cdp_browser_ws_url(
            "ws://127.0.0.1:31002", client=client
        ) == SOCKET


async def test_waits_through_a_port_that_is_not_open_yet() -> None:
    # The real failure: a viewer opened the pane four seconds into a six
    # second browser_navigate, and the connection was refused outright.
    transport = _transport(refusals=5)
    async with httpx.AsyncClient(transport=transport) as client:
        assert await _cdp_browser_ws_url(
            "ws://127.0.0.1:31002", client=client, timeout=5.0
        ) == SOCKET
    assert transport.state["calls"] == 6  # type: ignore[attr-defined]


async def test_gives_up_once_the_window_has_passed() -> None:
    # Waiting is bounded: a browser that is genuinely gone must not hold a
    # viewer's socket open indefinitely.
    transport = _transport(refusals=10_000)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(httpx.ConnectError):
            await _cdp_browser_ws_url(
                "ws://127.0.0.1:31002", client=client, timeout=0.3
            )


async def test_does_not_retry_a_reachable_endpoint_that_answers_badly() -> None:
    # A 200 with no webSocketDebuggerUrl is a broken browser, not a slow one;
    # retrying would just delay a failure that will not fix itself.
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"Browser": "Chrome/147"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(KeyError):
            await _cdp_browser_ws_url(
                "ws://127.0.0.1:31002", client=client, timeout=5.0
            )
    assert calls["n"] == 1
