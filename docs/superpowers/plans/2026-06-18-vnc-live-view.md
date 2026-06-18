# VNC-over-WebSocket Browser Live View Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the managed-agent browser live view's neko/WebRTC transport with VNC/RFB-over-WebSocket so "take control" works through the existing TCP proxy chain (no TURN/UDP) and keeps a CDP-free login window.

**Architecture:** An in-pod `x11vnc` attaches to chromium's existing Xorg `:1`; `websockify` bridges it to a WebSocket on `:8080` (the port the harness already proxies as `live_view_url`). The harness WS proxy and agent-suspend logic already exist and only need verification. The ops proxy drops its neko-specific iframe rewriting. The SDK renders the RFB stream with `@novnc/novnc` instead of an `<iframe>`.

**Tech Stack:** Docker (onkernel/chromium-headful base), supervisord, x11vnc, websockify, Python 3.12 + pytest (harness & ops), React + TypeScript + Vitest + `@novnc/novnc` (SDK).

**Spec:** `docs/superpowers/specs/2026-06-18-vnc-live-view-design.md`

## Progress

Updated before each commit. `[x]` done · `[~]` in progress · `[ ]` not started.

- [x] A1 — Image: x11vnc + websockify on :8080, neko disabled (RFB-handshake test) — verified: `RFB 003.008` on :8080, neko STOPPED
- [x] B1 — Harness: agent-suspend coverage across every browser tool — verified: all 10 `_browser_*_handler`s return `paused_by_user` under the control lock (no production change; guards already present)
- [x] B2 — Harness: live-view WS proxy control-required against an RFB upstream — verified: 4403 for non-holder, RFB banner proxied for holder (no production change)
- [x] B3 — Harness: parse client-side RFB messages across WS frame boundaries (`RFBClientMessageGate`) — verified: split/coalesced input gating; WS proxy uses a per-connection gate (85 browser tests green)
- [x] C1 — Ops: delete neko HTML-rewrite/interceptor; keep RFB WS proxy + auth — deleted the whole `get_live_browser_asset` GET route + orphaned cookie helpers (ruff clean, 25 ops tests pass); lands in `surogate-ops`
- [x] D1 — SDK: `browserLiveViewUrl` drops `pwd=admin` — verified (15 frontend tests pass); lands in `surogate-ops`
- [~] D2 — SDK: `BrowserLiveView` noVNC RFB canvas
- [ ] D3 — SDK: mount RFB only under control; `browser-pane` wiring
- [ ] E1 — End-to-end: build/deploy + acceptance (manual)

## Global Constraints

- Conventional Commits (`type(scope): subject`); **no `Co-Authored-By` trailer**; **never reference plan/task/phase/step numbers** in code comments or commit messages.
- Each repo change happens on its own branch. Image/harness/SDK changes land in `/work/surogates`; ops-proxy changes land in `/work/surogate-ops`.
- In `surogate-ops`, **do not use `uv run`** (it reinstalls the pinned `surogates` wheel and clobbers the local dev install); use the project venv / `pytest` directly.
- The chromium launch flags must stay clean: **no `--enable-automation`, no `--headless`** (keeps `navigator.webdriver` false). Do not add automation flags anywhere.
- Live view path/port: the harness service exposes `SERVICE_PORT_LIVE_VIEW=443` → `TARGET_PORT_LIVE_VIEW=8080`; the in-pod live-view listener is `:8080`.
- The SDK package `@invergent/agent-chat-react` is published to npm; keep its public component props stable unless a task says otherwise.

---

## File Structure

**Image (`/work/surogates`)**
- Modify: `images/browser/Dockerfile` — install `x11vnc`+`websockify`, disable neko, drop neko-branding rewrites.
- Create: `images/browser/supervisor/x11vnc.conf`, `images/browser/supervisor/websockify.conf` — supervised live-view services.
- Create: `images/browser/test_live_view_rfb.py` — build-and-verify integration check.

**Harness (`/work/surogates`)** — mostly verification, plus RFB stream-gating hardening.
- Modify: `surogates/browser/rfb.py` — replace single-frame `is_input_frame` assumptions with a buffered RFB client-message gate.
- Modify: `surogates/api/routes/browser.py` — keep one gate instance per live-view WS connection and filter client bytes before forwarding upstream.
- Test: `tests/test_browser_rfb.py` — split/coalesced RFB message coverage.
- Modify (tests): `tests/test_browser_route_ws.py` — RFB-upstream + control-required coverage.
- Modify (tests): `tests/test_browser_tools.py` — agent-suspend coverage across every browser tool entry point.

**Ops proxy (`/work/surogate-ops`)**
- Modify: `surogate_ops/server/routes/sessions.py` — delete neko iframe rewrite/interceptor + neko asset proxying; keep the RFB WS proxy + auth + state/control/preview/DELETE.
- Modify (tests): `tests/test_sessions_live_proxy.py` — existing live-session/browser proxy coverage; assert the HTML interceptor is gone and the RFB WS proxy remains.

**SDK (`/work/surogates/sdk/agent-chat-react`)**
- Modify: `package.json` — add `@novnc/novnc` and `@types/novnc__novnc`.
- Modify: `/work/surogates/pnpm-lock.yaml` — workspace lockfile updated by pnpm.
- Modify: `src/components/browser/browser-live-view.tsx` — `<iframe>` → noVNC RFB canvas.
- Modify: `src/components/browser/browser-pane.tsx` — pass a `wss://` URL; mount RFB only when `hasUserControl`.
- Modify (consumer): `frontend/src/features/work/work-agent-chat-adapter.ts` (in `/work/surogate-ops`) — `browserLiveViewUrl` drops `pwd=admin`, returns the WS path.
- Create (tests): `src/components/browser/__tests__/browser-live-view.test.tsx`.

---

## Phase A — Browser image serves RFB-over-WebSocket on :8080

### Task A1: Supervised x11vnc + websockify, neko disabled

**Files:**
- Modify: `images/browser/Dockerfile`
- Create: `images/browser/supervisor/x11vnc.conf`
- Create: `images/browser/supervisor/websockify.conf`
- Test: `images/browser/test_live_view_rfb.py`

**Interfaces:**
- Produces: a container that listens on `:8080` and speaks RFB-over-WebSocket (websockify → x11vnc → Xorg `:1`). The harness `live_view_url=ws://<svc>:8080` consumes this.

- [ ] **Step 1: Write the failing integration test**

```python
# images/browser/test_live_view_rfb.py
"""Build the browser image and assert :8080 speaks RFB-over-WebSocket.

Run from the surogates repo root. Requires docker + the `websocket-client`
package (pip install websocket-client).
"""
import base64, json, os, subprocess, time, uuid
import urllib.request
import websocket  # websocket-client

IMAGE = "surogates-agent-browser:rfb-test"
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def _build():
    subprocess.run(
        ["docker", "build", "-f", "images/browser/Dockerfile", "-t", IMAGE, "."],
        cwd=ROOT, check=True,
    )

def test_live_view_serves_rfb_over_ws():
    _build()
    name = f"rfb-test-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        ["docker", "run", "-d", "--rm", "--name", name, "-p", "18080:8080", IMAGE],
        check=True,
    )
    try:
        # Wait for websockify to come up.
        deadline = time.time() + 60
        ws = None
        while time.time() < deadline:
            try:
                ws = websocket.create_connection(
                    "ws://127.0.0.1:18080/", subprotocols=["binary"], timeout=3,
                )
                break
            except Exception:
                time.sleep(1)
        assert ws is not None, "websockify never accepted a WS connection on :8080"
        # x11vnc's first bytes are the RFB ProtocolVersion banner: "RFB 003.008\n".
        banner = ws.recv()
        if isinstance(banner, str):
            banner = banner.encode()
        assert banner.startswith(b"RFB 00"), f"not an RFB stream: {banner!r}"
        ws.close()
    finally:
        subprocess.run(["docker", "rm", "-f", name], check=False)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd /work/surogates && python -m pytest images/browser/test_live_view_rfb.py -v`
Expected: FAIL — the current image runs neko on `:8080`, so either the WS handshake differs or the first bytes are not an `RFB 00…` banner.

- [ ] **Step 3: Create the supervisor configs**

```ini
# images/browser/supervisor/x11vnc.conf
[program:x11vnc]
command=/usr/bin/x11vnc -display :1 -rfbport 5900 -localhost -forever -shared -nopw -noxdamage -wait 20 -defer 20
autostart=true
autorestart=true
startsecs=2
stdout_logfile=/var/log/supervisord/x11vnc
redirect_stderr=true
```

```ini
# images/browser/supervisor/websockify.conf
[program:websockify]
command=/usr/bin/websockify --heartbeat=30 0.0.0.0:8080 127.0.0.1:5900
autostart=true
autorestart=true
startsecs=2
stdout_logfile=/var/log/supervisord/websockify
redirect_stderr=true
```

- [ ] **Step 4: Modify the Dockerfile**

In `images/browser/Dockerfile`, in the existing `RUN apt-get … install` block, add `x11vnc` and `websockify` to the package list, and **replace** the line that enables neko (`sed -i 's/^autostart=false$/autostart=true/' /etc/supervisor/conf.d/services/neko.conf`) with one that disables it:

```dockerfile
    && sed -i 's/^autostart=true$/autostart=false/' \
        /etc/supervisor/conf.d/services/neko.conf \
```

Then copy the new service configs (after the existing `COPY images/browser/logo.svg …` line, or alongside it):

```dockerfile
COPY images/browser/supervisor/x11vnc.conf /etc/supervisor/conf.d/services/x11vnc.conf
COPY images/browser/supervisor/websockify.conf /etc/supervisor/conf.d/services/websockify.conf
```

Remove the neko-branding `RUN` block (the `/var/www/...` logo/title rewrites) — it only applies to the neko web UI, which is now disabled.

- [ ] **Step 5: Run the test to verify it passes**

Run: `cd /work/surogates && python -m pytest images/browser/test_live_view_rfb.py -v`
Expected: PASS — `:8080` accepts a binary WS and the first frame begins with `RFB 00`.

- [ ] **Step 6: Commit**

```bash
cd /work/surogates
git add images/browser/Dockerfile images/browser/supervisor/ images/browser/test_live_view_rfb.py
git commit -m "feat(browser-image): serve live view as RFB-over-WebSocket via x11vnc+websockify"
```

---

## Phase B — Harness: verify suspend behavior and harden RFB gating

### Task B1: Agent-suspend coverage across every browser tool

**Files:**
- Modify (read first): `surogates/tools/builtin/browser.py`
- Test: `tests/test_browser_tools.py`

**Interfaces:**
- Consumes: `tools/builtin/browser.py:_resolve_session_browser` returns `_paused_by_user_result()` (a JSON string containing `"error": "paused_by_user"`) when `browser_control.get(session_id)` is set; `browser_close` has the same check at `browser.py:250`.

- [ ] **Step 1: Write a parametrized failing test**

```python
# tests/test_browser_tools.py  (add to the existing module)
import json
import pytest

# Minimal valid args for every registered browser tool entry point.
BROWSER_TOOL_ARGS = {
    "browser_navigate": {"url": "https://example.com"},
    "browser_get_state": {},
    "browser_screenshot": {},
    "browser_click": {"x": 10, "y": 10},
    "browser_type": {"text": "hello"},
    "browser_press_key": {"keys": ["Enter"]},
    "browser_scroll": {"x": 10, "y": 10, "delta_y": 100},
    "browser_drag": {"path": [[10, 10], [20, 20]]},
    "browser_wait": {"ms": 1},
    "browser_close": {},
}

@pytest.mark.parametrize("tool_name", BROWSER_TOOL_NAMES)
async def test_browser_tool_paused_while_user_holds_control(
    tool_name,
    tenant,
    tmp_path,
) -> None:
    from surogates.governance.policy import GovernanceGate, PolicyDecision
    from surogates.tools.registry import ToolRegistry
    from surogates.tools.router import ToolRouter
    from surogates.tools.runtime import ToolRuntime

    class AllowAll(GovernanceGate):
        def __init__(self) -> None:
            pass

        def check(self, *args: Any, **kwargs: Any) -> PolicyDecision:
            return PolicyDecision(
                allowed=True,
                reason="test",
                tool_name=str(args[0]),
            )

    registry = ToolRegistry()
    ToolRuntime(registry).register_builtins()
    pool = FakePool()

    result = await ToolRouter(
        registry=registry,
        sandbox_pool=None,  # type: ignore[arg-type]
        governance=AllowAll(),
    ).execute(
        name=tool_name,
        arguments=BROWSER_TOOL_ARGS[tool_name],
        tenant=tenant,
        session_id=uuid4(),
        browser_pool=pool,
        browser_control=FakeControlStore(holder="user-holding-control"),
        workspace_path=str(tmp_path),
        _client_factory=lambda endpoint: FakeClient(),
    )
    payload = json.loads(result)
    assert payload.get("error") == "paused_by_user", (
        f"{tool_name} ran while the user held control"
    )
    assert pool.ensures == []
    assert pool.destroyed == []
```

Use the existing `BROWSER_TOOL_NAMES`, `FakePool`, `FakeControlStore`, `FakeClient`, and `tenant` fixture in `tests/test_browser_tools.py`. Do not invent tool names (`browser_act`, `browser_extract`, `browser_state`) — they are not registered in this repo.

- [ ] **Step 2: Run it to verify it fails (or surfaces a gap)**

Run: `cd /work/surogates && python -m pytest tests/test_browser_tools.py -k paused_while_user -v`
Expected: PASS for tools already gated; FAIL for any tool path that bypasses `_resolve_session_browser`/the lock check — that failure names the gap.

- [ ] **Step 3: Close any gap found**

For each failing tool, add the same preflight guard the others use, immediately before any `browser_pool.ensure`/`KernelBrowserClient` call:

```python
    if browser_control is not None and await browser_control.get(sid) is not None:
        return _paused_by_user_result()
```

(If all tools already pass, no code change — record that the coverage is complete.)

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd /work/surogates && python -m pytest tests/test_browser_tools.py -k paused_while_user -v`
Expected: PASS for every tool in `BROWSER_TOOL_NAMES`.

- [ ] **Step 5: Commit**

```bash
cd /work/surogates
git add tests/test_browser_tools.py surogates/tools/builtin/browser.py
git commit -m "test(browser): assert all browser tools suspend while user holds control"
```

### Task B2: Live-view WS proxy is control-required against an RFB upstream

**Files:**
- Modify (read first): `surogates/api/routes/browser.py:458-558` (`proxy_live_view_ws`)
- Test: `tests/test_browser_route_ws.py`

**Interfaces:**
- Consumes: `proxy_live_view_ws` closes with code `4403` when `control.held_by(session) != effective_user`; otherwise connects `_connect_live_view_ws(live_view_url)` and pumps frames both ways, gating client byte frames via `_should_forward_client_frame`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_browser_route_ws.py  (add)
import pytest

async def test_live_view_ws_rejects_non_control_holder(ws_proxy_env):
    # ws_proxy_env: fixture wiring a resolver returning a fake endpoint and a
    # control store whose held_by(session) returns a DIFFERENT user id.
    env = ws_proxy_env(control_holder="someone-else")
    close = await env.connect_live_view(user="me")
    assert close.code == 4403

async def test_live_view_ws_proxies_rfb_for_holder(ws_proxy_env):
    # Fake upstream websockify that sends the RFB banner then echoes input.
    env = ws_proxy_env(control_holder="me", upstream_sends=[b"RFB 003.008\n"])
    frames = await env.connect_and_collect(user="me", count=1)
    assert frames[0].startswith(b"RFB 00")
```

Add a local `ws_proxy_env` fixture in `tests/test_browser_route_ws.py` that builds a Starlette test app mounting the `browser` router with fakes for `browser_resolver`, `browser_control`, and a stub upstream WS server (or monkeypatch `_connect_live_view_ws` to return an async iterable yielding `upstream_sends`).

- [ ] **Step 2: Run to verify it fails**

Run: `cd /work/surogates && python -m pytest tests/test_browser_route_ws.py -k live_view_ws -v`
Expected: FAIL until the fixture/stubs exist (the proxy code itself already implements the behavior).

- [ ] **Step 3: Implement only the test scaffolding**

No production change expected — `proxy_live_view_ws` already enforces control and pumps frames. Implement the fixture/stubs so the tests exercise the real handler.

- [ ] **Step 4: Run to verify it passes**

Run: `cd /work/surogates && python -m pytest tests/test_browser_route_ws.py -k live_view_ws -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /work/surogates
git add tests/test_browser_route_ws.py
git commit -m "test(browser): live-view WS proxy is control-required and proxies RFB frames"
```

### Task B3: Parse client-side RFB messages across WS frame boundaries

**Files:**
- Modify: `surogates/browser/rfb.py`
- Modify: `surogates/api/routes/browser.py`
- Test: `tests/test_browser_rfb.py`
- Test: `tests/test_browser_route_ws.py`

**Interfaces:**
- Produces: a per-connection gate that forwards handshake bytes immediately, buffers complete RFB ClientMessages after the x11vnc no-auth handshake, and drops only input messages (`KeyEvent`, `PointerEvent`, `ClientCutText`) when the caller no longer holds browser control.
- Consumes: `_should_forward_client_frame` currently returns one boolean for one WS frame. Replace that call site with chunk filtering so split and coalesced RFB messages are handled correctly.

- [ ] **Step 1: Write failing RFB parser tests**

```python
# tests/test_browser_rfb.py
from surogates.browser.rfb import RFBClientMessageGate

def test_gate_forwards_split_pointer_event_only_when_complete():
    gate = RFBClientMessageGate()
    # x11vnc no-auth client-side handshake: ProtocolVersion, selected None security,
    # ClientInit. These bytes are not RFB ClientMessages and must pass through.
    assert gate.filter_client_bytes(b"RFB 003.008\n\x01\x01", input_allowed=True) == [
        b"RFB 003.008\n\x01\x01",
    ]

    assert gate.filter_client_bytes(b"\x05\x00", input_allowed=True) == []
    assert gate.filter_client_bytes(b"\x00\x0a\x00\x0b", input_allowed=True) == [
        b"\x05\x00\x00\x0a\x00\x0b",
    ]

def test_gate_drops_input_after_control_expires_but_keeps_framebuffer_requests():
    gate = RFBClientMessageGate()
    assert gate.filter_client_bytes(b"RFB 003.008\n\x01\x01", input_allowed=True)

    update_request = b"\x03\x00\x00\x00\x00\x00\x10\x00\x10\x00"
    pointer_event = b"\x05\x01\x00\x0a\x00\x0b"
    assert gate.filter_client_bytes(
        update_request + pointer_event,
        input_allowed=False,
    ) == [update_request]

def test_gate_handles_coalesced_key_and_cut_text_input():
    gate = RFBClientMessageGate()
    assert gate.filter_client_bytes(b"RFB 003.008\n\x01\x01", input_allowed=True)

    key_event = b"\x04\x01\x00\x00\x00\x00\xff\r"
    cut_text = b"\x06\x00\x00\x00\x00\x00\x00\x05hello"
    assert gate.filter_client_bytes(key_event + cut_text, input_allowed=False) == []
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd /work/surogates && python -m pytest tests/test_browser_rfb.py -k gate -v`
Expected: FAIL because `RFBClientMessageGate` does not exist.

- [ ] **Step 3: Implement the buffered RFB client-message gate**

```python
# surogates/browser/rfb.py
"""RFB ClientMessage parsing for live-view WebSocket proxying."""

from __future__ import annotations

RFB_INPUT_TYPES: frozenset[int] = frozenset({4, 5, 6})
_HANDSHAKE_CLIENT_BYTES = 14  # ProtocolVersion(12) + SecurityType(1) + ClientInit(1)


def _client_message_len(buffer: bytes) -> int | None:
    if not buffer:
        return None
    message_type = buffer[0]
    if message_type == 0:
        return 20
    if message_type == 2:
        if len(buffer) < 4:
            return None
        return 4 + int.from_bytes(buffer[2:4], "big") * 4
    if message_type == 3:
        return 10
    if message_type == 4:
        return 8
    if message_type == 5:
        return 6
    if message_type == 6:
        if len(buffer) < 8:
            return None
        return 8 + int.from_bytes(buffer[4:8], "big")
    return 1


class RFBClientMessageGate:
    def __init__(self) -> None:
        self._handshake_remaining = _HANDSHAKE_CLIENT_BYTES
        self._buffer = bytearray()

    def filter_client_bytes(
        self,
        data: bytes,
        *,
        input_allowed: bool,
    ) -> list[bytes]:
        if not data:
            return []
        out: list[bytes] = []
        view = memoryview(data)
        if self._handshake_remaining:
            n = min(self._handshake_remaining, len(view))
            out.append(bytes(view[:n]))
            self._handshake_remaining -= n
            view = view[n:]
            if not view:
                return out

        self._buffer.extend(view)
        while self._buffer:
            length = _client_message_len(bytes(self._buffer))
            if length is None or len(self._buffer) < length:
                break
            message = bytes(self._buffer[:length])
            del self._buffer[:length]
            if message[0] in RFB_INPUT_TYPES and not input_allowed:
                continue
            out.append(message)
        return out


def is_input_frame(frame: bytes) -> bool:
    """Compatibility helper for existing focused tests."""
    return bool(frame) and frame[0] in RFB_INPUT_TYPES
```

- [ ] **Step 4: Use the gate in the live-view WS proxy**

In `surogates/api/routes/browser.py`, import `RFBClientMessageGate` and create one gate inside `proxy_live_view_ws` before `client_to_upstream`:

```python
from surogates.browser.rfb import RFBClientMessageGate, is_input_frame
```

```python
    rfb_gate = RFBClientMessageGate()

    async def client_to_upstream() -> None:
        try:
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    return
                frame = _live_view_client_payload(message)
                if frame is None:
                    continue
                if isinstance(frame, bytes):
                    input_allowed = (
                        tenant.user_id is not None
                        and await control.held_by(str(session_id)) == str(tenant.user_id)
                    )
                    for chunk in rfb_gate.filter_client_bytes(
                        frame,
                        input_allowed=input_allowed,
                    ):
                        await upstream.send(chunk)
                    continue
                await upstream.send(frame)
        except WebSocketDisconnect:
            return
```

After this change, `_should_forward_client_frame` can remain for its focused unit tests, but the production WS path must use `RFBClientMessageGate`.

- [ ] **Step 5: Run parser and WS helper tests**

Run: `cd /work/surogates && python -m pytest tests/test_browser_rfb.py tests/test_browser_route_ws.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
cd /work/surogates
git add surogates/browser/rfb.py surogates/api/routes/browser.py tests/test_browser_rfb.py tests/test_browser_route_ws.py
git commit -m "fix(browser): gate RFB input across WebSocket frame boundaries"
```

---

## Phase C — Ops proxy: delete neko iframe plumbing

### Task C1: Remove the neko HTML-rewrite/interceptor; keep the RFB WS proxy

**Files:**
- Modify: `/work/surogate-ops/surogate_ops/server/routes/sessions.py`
- Test: `/work/surogate-ops/tests/test_sessions_live_proxy.py`

**Interfaces:**
- Consumes (kept): `websocket_live_browser` (RFB WS proxy at `/{session_id}/browser/live/{path}`), `verify_ws_token`, and the `browser/state`, `browser/control`, `browser/preview.png`, `DELETE /browser` routes.
- Removed: `_inject_ws_token_interceptor`, `_LIVE_VIEW_WS_INTERCEPTOR`, the `text/html` rewrite branch in `get_live_browser_asset`.

- [ ] **Step 1: Write the failing test**

```python
# /work/surogate-ops/tests/test_sessions_live_proxy.py
import surogate_ops.server.routes.sessions as s

def test_neko_html_interceptor_is_gone():
    # The neko-only iframe rewriting must not exist anymore.
    assert not hasattr(s, "_inject_ws_token_interceptor")
    assert not hasattr(s, "_LIVE_VIEW_WS_INTERCEPTOR")

def test_rfb_ws_proxy_and_auth_are_kept():
    assert hasattr(s, "websocket_live_browser")
    assert hasattr(s, "get_live_browser_state")
    assert hasattr(s, "post_live_browser_control")
    assert hasattr(s, "get_live_browser_preview")
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd /work/surogate-ops && python -m pytest tests/test_sessions_live_proxy.py -k neko_html_interceptor -v`
Expected: FAIL on `test_neko_html_interceptor_is_gone` (symbols still present).

- [ ] **Step 3: Delete the neko plumbing**

In `surogate_ops/server/routes/sessions.py`:
- Delete `_LIVE_VIEW_WS_INTERCEPTOR` (the `b"<script>…"` blob) and `_inject_ws_token_interceptor`.
- In `get_live_browser_asset`, delete the `if "text/html" in content_type … request.query_params.get("token")` rewrite branch (the body-read + `_inject_ws_token_interceptor` + `_STRIP_FROM_REWRITE` Response path); keep the streaming `else` branch as the single response path. The asset route may now be unused entirely once the SDK stops loading neko HTML — if no caller remains, delete the route too (verify with a repo grep for `/browser/live/` GET usage before removing).
- Keep `websocket_live_browser`, `verify_ws_token`, `get_live_browser_state`, `post_live_browser_control`, `get_live_browser_preview`, `delete_session_browser`.

- [ ] **Step 4: Run to verify it passes**

Run: `cd /work/surogate-ops && python -m pytest tests/test_sessions_live_proxy.py -k "neko_html_interceptor or rfb_ws_proxy" -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /work/surogate-ops
git add surogate_ops/server/routes/sessions.py tests/test_sessions_live_proxy.py
git commit -m "refactor(sessions): drop neko iframe rewrite; keep RFB live-view WS proxy"
```

---

## Phase D — SDK: render RFB with noVNC instead of an iframe

### Task D1: `browserLiveViewUrl` returns a WS path without the neko password

**Files:**
- Modify: `/work/surogate-ops/frontend/src/features/work/work-agent-chat-adapter.ts:851-856`
- Test: `/work/surogate-ops/frontend/src/features/work/__tests__/work-agent-chat-adapter.test.ts`

**Interfaces:**
- Produces: `browserLiveViewUrl(sessionId)` returns the live-view path `…/browser/live/?token=<jwt>` (no `pwd=admin`). The SDK upgrades it to a `wss://` URL for noVNC.

- [ ] **Step 1: Write the failing test**

```ts
// __tests__/work-agent-chat-adapter.test.ts
it("browserLiveViewUrl omits the neko password", () => {
  const adapter = createWorkAgentChatAdapter("agent-1");
  const url = adapter.browserLiveViewUrl("sess-123");
  expect(url).not.toContain("pwd=admin");
  expect(url).toContain("/browser/live/");
  expect(url).toContain("token=");
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd /work/surogate-ops/frontend && npm run test -- work-agent-chat-adapter`
Expected: FAIL — current URL contains `pwd: "admin"`.

- [ ] **Step 3: Remove `pwd` from the builder**

```ts
// work-agent-chat-adapter.ts
browserLiveViewUrl(sessionId) {
  return scopedSessionUrl(sessionId, "/browser/live/", agentId, {
    token: getAuthToken() ?? undefined,
  });
},
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd /work/surogate-ops/frontend && npm run test -- work-agent-chat-adapter`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /work/surogate-ops
git add frontend/src/features/work/work-agent-chat-adapter.ts frontend/src/features/work/__tests__/work-agent-chat-adapter.test.ts
git commit -m "feat(work): live-view url drops neko password for RFB transport"
```

### Task D2: `BrowserLiveView` renders a noVNC RFB canvas

**Files:**
- Modify: `/work/surogates/sdk/agent-chat-react/package.json`
- Modify: `/work/surogates/sdk/agent-chat-react/src/components/browser/browser-live-view.tsx`
- Test: `/work/surogates/sdk/agent-chat-react/src/components/browser/__tests__/browser-live-view.test.tsx`

**Interfaces:**
- Consumes: `src` is the live-view URL from `browserLiveViewUrl`. The component converts it to `wss://`/`ws://` and connects with `@novnc/novnc`'s `RFB`.
- Produces: same `BrowserLiveViewProps` (`{ src, testId? }`) so `browser-pane.tsx` is unchanged in its call shape.

- [ ] **Step 1: Add the dependency**

```bash
cd /work/surogates/sdk/agent-chat-react
pnpm add @novnc/novnc@^1.7.0
pnpm add -D @types/novnc__novnc@^1.6.0
```

- [ ] **Step 2: Write the failing test**

```tsx
// src/components/browser/__tests__/browser-live-view.test.tsx
import { render } from "@testing-library/react";
import { vi, expect, it } from "vitest";
import { BrowserLiveView } from "../browser-live-view";

const connect = vi.fn();
vi.mock("@novnc/novnc/lib/rfb", () => ({
  default: vi.fn().mockImplementation((el: HTMLElement, url: string) => {
    connect(url);
    return { disconnect: vi.fn(), addEventListener: vi.fn(), viewOnly: false };
  }),
}));

it("connects RFB to a wss:// url derived from src", () => {
  render(<BrowserLiveView src="https://ops.example/api/sessions/s1/browser/live/?token=t" />);
  expect(connect).toHaveBeenCalledWith(
    "wss://ops.example/api/sessions/s1/browser/live/?token=t",
  );
});
```

- [ ] **Step 3: Run to verify it fails**

Run: `cd /work/surogates/sdk/agent-chat-react && npm run test -- browser-live-view`
Expected: FAIL — current component renders an `<iframe>`, never constructs `RFB`.

- [ ] **Step 4: Implement the noVNC canvas**

```tsx
// src/components/browser/browser-live-view.tsx
import { useEffect, useRef } from "react";
import RFB from "@novnc/novnc/lib/rfb";

interface BrowserLiveViewProps {
  src: string;
  testId?: string;
}

function toWsUrl(src: string): string {
  const u = new URL(src, window.location.href);
  u.protocol = u.protocol === "http:" ? "ws:" : "wss:";
  return u.toString();
}

export function BrowserLiveView({
  src,
  testId = "browser-rfb",
}: BrowserLiveViewProps) {
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!containerRef.current) return;
    const rfb = new RFB(containerRef.current, toWsUrl(src), {
      wsProtocols: ["binary"],
    });
    rfb.viewOnly = false;
    rfb.scaleViewport = true;
    return () => rfb.disconnect();
  }, [src]);

  return (
    <div
      ref={containerRef}
      data-testid={testId}
      className="h-full w-full bg-black"
    />
  );
}
```

- [ ] **Step 5: Run to verify it passes**

Run: `cd /work/surogates/sdk/agent-chat-react && npm run test -- browser-live-view`
Expected: PASS.

- [ ] **Step 6: Typecheck + build the SDK**

Run: `cd /work/surogates/sdk/agent-chat-react && npm run typecheck && npm run build`
Expected: no type errors; `dist/` rebuilt.

- [ ] **Step 7: Commit**

```bash
cd /work/surogates
git add sdk/agent-chat-react/package.json pnpm-lock.yaml sdk/agent-chat-react/src/components/browser/
git commit -m "feat(agent-chat): render browser live view with noVNC RFB canvas"
```

### Task D3: Mount RFB only under control; verify `browser-pane`

**Files:**
- Modify (read first): `/work/surogates/sdk/agent-chat-react/src/components/browser/browser-pane.tsx:59-115`
- Test: `/work/surogates/sdk/agent-chat-react/src/components/browser/__tests__/browser-pane.test.tsx`

**Interfaces:**
- Consumes: `canUseLiveView = hasLiveView && hasUserControl && Boolean(liveViewUrl)` already gates whether `BrowserLiveView` mounts. Confirm `BrowserLiveView` is only rendered when `canUseLiveView` is true (so noVNC connects only for the control holder; non-holders see `preview.png`).

- [ ] **Step 1: Write the failing/guard test**

```tsx
// __tests__/browser-pane.test.tsx
it("does not mount the RFB view without user control", () => {
  const { queryByTestId } = renderPane({ hasUserControl: false, status: "live" });
  expect(queryByTestId("browser-rfb")).toBeNull();
});

it("mounts the RFB view when the user holds control", () => {
  const { getByTestId } = renderPane({ hasUserControl: true, status: "live" });
  expect(getByTestId("browser-rfb")).toBeTruthy();
});
```

`renderPane` is a small helper that renders `BrowserPane` with a stub adapter exposing `browserLiveViewUrl` and the given control/status; mock `@novnc/novnc/lib/rfb` as in Task D2.

- [ ] **Step 2: Run to verify current behavior**

Run: `cd /work/surogates/sdk/agent-chat-react && npm run test -- browser-pane`
Expected: the "without control" test PASSES if gating is already correct; the "with control" test FAILS only if the testId/wiring changed. Fix wiring if needed (no logic change expected beyond the new testId).

- [ ] **Step 3: Reconcile testId / mount condition if needed**

If `browser-pane` referenced the old `browser-iframe` testId or rendered the view outside the `canUseLiveView` guard, update it to render `BrowserLiveView` only when `canUseLiveView` is true.

- [ ] **Step 4: Run to verify it passes**

Run: `cd /work/surogates/sdk/agent-chat-react && npm run test -- browser-pane`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /work/surogates
git add sdk/agent-chat-react/src/components/browser/
git commit -m "test(agent-chat): RFB view mounts only for the browser control holder"
```

---

## Phase E — End-to-end acceptance (manual)

### Task E1: Build, deploy to a test environment, and run the acceptance scenario

**Files:** none (operational).

- [ ] **Step 1: Build and push the image**

```bash
cd /work/surogates
docker build -f images/browser/Dockerfile -t ghcr.io/invergent-ai/surogates-agent-browser:rfb-rc1 .
docker push ghcr.io/invergent-ai/surogates-agent-browser:rfb-rc1
```

- [ ] **Step 2: Point the fleet config at the RC image** in the target environment's browser-fleet config (the `cfg.image` consumed by `surogate_ops/core/compute/browser_fleet`), and roll a fresh warm pod.

- [ ] **Step 3: Verify the pod** serves RFB on `:8080` and neko is disabled:

```bash
kubectl exec -n browser-fleet <pod> -c chromium -- sh -c \
  'supervisorctl status | grep -E "neko|x11vnc|websockify"'
```
Expected: `neko` STOPPED/absent; `x11vnc` and `websockify` RUNNING.

- [ ] **Step 4: Acceptance scenario.** Start a managed session that opens a browser, take control from Studio, and confirm:
  - the live view renders and is interactive (type into a form field, click);
  - while control is held, agent browser tools return `paused_by_user`;
  - perform a real **Google login** through the live view (CDP-free window) and confirm no "this browser or app may not be secure" block;
  - release control and confirm the agent resumes and sees the logged-in state.

- [ ] **Step 5: Record results** in the PR description (screens/notes). No code commit.

---

## Self-Review

- **Spec coverage:** Image (Phase A), harness agent-suspend verification plus RFB stream-gating hardening (Phase B), ops neko-removal (Phase C), SDK noVNC (Phase D), acceptance incl. real Google login (Phase E) — every spec section maps to a task. Out-of-scope items (residential IP, profile reuse) are intentionally excluded.
- **Placeholders:** none — every code/conf/command step carries real content; the only "verify, maybe no change" tasks (B2, D3) are explicit about that and still produce a test deliverable.
- **Type/name consistency:** `BrowserLiveViewProps {src, testId}` preserved (D2/D3); `paused_by_user` used consistently (B1); `RFBClientMessageGate` covers split/coalesced client messages (B3); `browserLiveViewUrl` shape consistent (D1/D2); `live_view_url`/`:8080` consistent (A1/B2/B3).
