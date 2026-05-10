# Agent Browser — Phase C: Live View, Control & SPA UI — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the agent's browser visible to the user. Add the API server endpoints for browser state and control acquisition (with the spec's three-branch conflict semantics), an authenticated HTTP/WebSocket proxy that streams the kernel-images NoVNC live view to the SPA with input-frame gating, the harness wake-on-release loop, and the SPA changes — a stacked right pane (`BrowserPane` above `WorkspacePanel`), a take-control toggle, an activity-group component that collapses consecutive `browser_*` tool calls in the chat thread, and inline event markers for the new `browser.*` lifecycle events. End state: the user can watch the agent drive the browser in real time and take over for CAPTCHAs, MFA, or login flows.

**Architecture:** The live agent API is the only surface that talks to browser pods, and browser pods stay on the cluster network. Direct Surogates clients use `/v1/sessions/{id}/browser/*`; surogate-ops users go through the existing ops backend proxy at `/api/sessions/{id}/browser/*`, which authenticates the ops user and calls the live agent API with the agent service account. The live API resolves the pod via `BrowserResolver` (Redis primary, K8s label fallback with tenant metadata), then proxies HTTP and WebSocket traffic bidirectionally. Inbound RFB ClientMessage frames of types `KeyEvent (4)`, `PointerEvent (5)`, and `ClientCutText (6)` are dropped unless `BrowserControlStore` records the connecting user as the holder. POST `/v1/sessions/{id}/browser/control` flips the flag and emits `BROWSER_CONTROL_GRANTED` / `BROWSER_CONTROL_RETURNED`; release also enqueues a wake on the session so the harness resumes.

**Tech Stack:** FastAPI WebSocket routes (already used elsewhere — see `surogates/api/routes/`), httpx + websockets for upstream proxying, redis-py for cross-process state (already wired), React 19 + TypeScript in `@invergent/agent-chat-react` (the SDK published from `sdk/agent-chat-react/`), `@novnc/novnc` for the in-iframe client, vitest for SDK tests, pytest + ASGITransport for API tests.

**Spec:** [`docs/superpowers/specs/2026-05-10-agent-browser-design.md`](../specs/2026-05-10-agent-browser-design.md) §7 (live view), §8 (UI design), §9 (events), §10 (security).

**Predecessors:**
- [Phase A](2026-05-10-agent-browser-phase-a.md) — `BrowserPool`, `BrowserRegistry`, `BrowserControlStore`, `KernelBrowserClient`, all `browser_*` tools (control short-circuit included).
- [Phase B](2026-05-10-agent-browser-phase-b.md) — `K8sBrowserBackend.find_by_session`, the surogates-agent-browser image, browser RBAC + NetworkPolicy, helm wiring.

Phase C mostly consumes those interfaces. It adds one metadata-preserving
K8s lookup wrapper around Phase B's `find_by_session` so API fallback
resolution remains tenant-scoped.

---

## TODO

- [x] **Completed:** Review/correct Phase C plan against the Phase A/B implementation.
- [x] **Completed:** Task 1 — add browser control event types.
- [x] **Completed:** Task 2 — add tenant-scoped BrowserResolver with K8s fallback.
- [x] **Completed:** Task 3 — add RFB input-frame gating helper.
- [x] **Completed:** Task 4 — add browser state API route.
- [x] **Completed:** Task 5 — add browser control API route.
- [x] **Completed:** Task 6 — restrict query-param auth and add WebSocket auth helper.
- [ ] **In progress:** Task 7 — proxy NoVNC static assets through the API server.
- [ ] **Still left to do:** Implement Task 7 through Task 18 in order, committing at each task boundary.
- [ ] **Still left to do:** Run the backend, SDK, frontend, Helm, and opt-in K8s verification listed in Final verification.
- [ ] **Completed:** Phase A and Phase B prerequisites exist on this branch.

---

## File Structure

```
surogates/browser/
├── resolver.py              (NEW — BrowserResolver: Registry primary + K8s fallback)
├── rfb.py                   (NEW — RFB ClientMessage type sniffer + frame gating helper)
├── kubernetes.py            (MODIFY — expose tenant metadata in session fallback lookup)

surogates/api/routes/
└── browser.py               (NEW — GET /state, POST /control, HTTP/WS /live/{path})

surogates/tenant/auth/
└── middleware.py            (MODIFY — restrict query-param JWT auth to SSE +
                              browser live-view paths, and add a WebSocket
                              tenant-auth helper)

surogates/api/app.py          (MODIFY — register browser router)

surogates/session/events.py   (MODIFY — add BROWSER_CONTROL_GRANTED, BROWSER_CONTROL_RETURNED)

surogates/harness/loop.py     (MODIFY — pre-iteration check: when BrowserControlStore
                              holds, prepend a one-time system-injected note)

surogates/orchestrator/dispatcher.py
                              (no change — release endpoint enqueues via existing
                              enqueue_session helper; same pattern as clarify)

helm/surogates/templates/api-rbac.yaml
helm/surogates/templates/api-deployment.yaml
                              (MODIFY both — egress to browser pods on 6080 + 10001;
                              labels match the browser NetworkPolicy ingress rule
                              already added in Phase B Task 12)

# Apply every helm change to BOTH chart locations:
/work/surogate-ops/surogate_ops/agent_chart/templates/api-rbac.yaml
/work/surogate-ops/surogate_ops/agent_chart/templates/api-deployment.yaml

sdk/agent-chat-react/src/
├── types.ts                 (MODIFY — add browser.* event types and pane state)
├── runtime/events.ts        (MODIFY — recognise browser.* event family)
├── runtime/reducer.ts       (MODIFY — fold browser events into chat state)
├── api/                     (NEW directory if it doesn't exist; the SDK uses
│   └── browser.ts            adapter callbacks rather than direct fetch — wire
│                              the new endpoints through AgentChatAdapter)
├── components/
│   └── browser/
│       ├── browser-pane.tsx              (NEW — stacked container header + state)
│       ├── browser-live-view.tsx         (NEW — iframe pointing at /live/vnc.html)
│       ├── browser-control-bar.tsx       (NEW — Take/Return control button)
│       ├── browser-status-dot.tsx        (NEW — shared visual)
│       └── browser-activity-group.tsx    (NEW — collapses consecutive browser_*
│                                          tool calls in ChatThread)
├── components/chat/chat-thread.tsx       (MODIFY — invoke activity grouping)
├── agent-chat.tsx                        (MODIFY — stack BrowserPane above
│                                          WorkspacePanel in the right column)
└── adapter context                       (MODIFY — extend AgentChatAdapter with
                                            browser.getState / acquireControl /
                                            releaseControl + liveViewUrl helper)

/work/surogate-ops/surogate_ops/server/routes/sessions.py
                              (MODIFY — proxy browser state/control and live-view
                              HTTP/WS through the ops backend, matching the
                              existing workspace/artifact live-session proxy)

sdk/agent-chat-react/tests/
├── browser-pane.test.tsx    (NEW)
├── browser-activity-group.test.tsx
                             (NEW)
└── reducer.test.ts          (MODIFY — assert new browser.* events are folded)

tests/test_browser_resolver.py        (NEW)
tests/test_browser_rfb.py             (NEW)
tests/test_browser_route.py           (NEW — REST endpoints with ASGI client)
tests/test_browser_route_ws.py        (NEW — WS proxy with a fake upstream)
tests/test_browser_pause_injection.py (NEW — harness pause-message injection)
tests/integration/test_browser_e2e_phase_c.py  (NEW — opt-in, real cluster,
                                                live-view round-trip)
```

The SDK is published as `@invergent/agent-chat-react` and the surogate-ops
frontend pins a version (`^1.5.10` at the time of writing). Phase C makes
breaking-but-additive SDK changes; Task 18 covers the version bump and
release flow.

---

## Conventions used in every task

- **Backend tests:** `pytest` + `pytest-asyncio` (`asyncio_mode = "auto"`). REST tests use the `httpx.AsyncClient(transport=ASGITransport(app=app))` pattern from `tests/integration/test_api.py`. WebSocket tests use `httpx_ws` or `websockets.connect` against an `ASGITransport`-mounted app — see `tests/test_browser_route_ws.py` (created in Task 7) for the canonical setup we'll use.
- **Frontend tests:** `vitest` + `@testing-library/react` — same pattern as `sdk/agent-chat-react/tests/agent-chat.test.tsx`.
- **Redis tests:** hand-rolled `FakeRedis` per the pattern in `tests/test_rate_limit_guard.py` (also used in Phase A's tests).
- **Helm tasks** (16-17): apply each change to **both** `helm/surogates/templates/` (this repo) and `surogate_ops/agent_chart/templates/` (surogate-ops repo), with two separate commits per task — one in `/work/surogates`, one in `/work/surogate-ops`.
- Commit at the end of every task with the message shown.

---

## Task 1: Add `BROWSER_CONTROL_GRANTED` / `BROWSER_CONTROL_RETURNED` event types

**Files:**
- Modify: `surogates/session/events.py`
- Test: `tests/test_browser_resolver.py` (new file; we'll grow it across tasks)

- [ ] **Step 1: Write the failing test**

`tests/test_browser_resolver.py`:

```python
"""Phase C foundation tests."""

from __future__ import annotations

from surogates.session.events import EventType


def test_browser_control_event_types_exist() -> None:
    assert EventType.BROWSER_CONTROL_GRANTED.value == "browser.control_granted"
    assert EventType.BROWSER_CONTROL_RETURNED.value == "browser.control_returned"


def test_existing_browser_events_unchanged() -> None:
    # Phase A added these; Phase C must not touch them.
    assert EventType.BROWSER_PROVISIONED.value == "browser.provisioned"
    assert EventType.BROWSER_DESTROYED.value == "browser.destroyed"
```

- [ ] **Step 2: Run** — `pytest tests/test_browser_resolver.py -v` → `AttributeError`.

- [ ] **Step 3: Add the events**

In `surogates/session/events.py`, find the Phase A browser block:

```python
    # Agent browser lifecycle (Phase A: provision/destroy only;
    # control + recording events arrive in Phases C/D).
    BROWSER_PROVISIONED = "browser.provisioned"
    BROWSER_DESTROYED = "browser.destroyed"
```

Replace the comment and append the new types:

```python
    # Agent browser lifecycle.
    BROWSER_PROVISIONED = "browser.provisioned"
    BROWSER_DESTROYED = "browser.destroyed"
    # Phase C: live-view control acquisition.
    BROWSER_CONTROL_GRANTED = "browser.control_granted"
    BROWSER_CONTROL_RETURNED = "browser.control_returned"
```

- [ ] **Step 4: Run** — both tests PASS.

- [ ] **Step 5: Commit**

```bash
git add surogates/session/events.py tests/test_browser_resolver.py
git commit -m "feat(browser): add control_granted/control_returned event types"
```

---

## Task 2: `BrowserResolver` — Registry primary + K8s label fallback

**Files:**
- Create: `surogates/browser/resolver.py`
- Modify: `surogates/browser/kubernetes.py`
- Test: `tests/test_browser_resolver.py` (extend)

`BrowserResolver` is the single API the API server uses to find a session's
browser. It hits Redis first (fast, set by the worker on provision); on
miss it asks the K8s backend for a label-derived registry entry. It verifies
tenant scope before returning the entry. The fallback must not return an
endpoint unless the pod labels include an `org_id` that matches the caller;
otherwise a Redis miss could become a cross-tenant browser leak.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_browser_resolver.py`:

```python
from datetime import datetime, timezone

import pytest

from surogates.browser.base import BrowserEndpoint
from surogates.browser.registry import BrowserEntry
from surogates.browser.resolver import BrowserResolver, ResolvedBrowser


class FakeRegistry:
    def __init__(self) -> None:
        self.entries: dict[str, BrowserEntry] = {}

    async def get(self, session_id: str) -> BrowserEntry | None:
        return self.entries.get(session_id)


class FakeBackend:
    def __init__(self) -> None:
        self.found: dict[str, BrowserEntry] = {}
        self.calls: list[str] = []

    async def find_entry_by_session(self, session_id: str) -> BrowserEntry | None:
        self.calls.append(session_id)
        return self.found.get(session_id)


def _entry(session: str, *, org: str = "org-1", user: str = "user-1") -> BrowserEntry:
    return BrowserEntry(
        session_id=session, org_id=org, user_id=user,
        rest_url=f"http://browser-{session[:6]}.svc:10001",
        cdp_url=f"ws://browser-{session[:6]}.svc:9222",
        live_view_url=f"ws://browser-{session[:6]}.svc:443",
        provisioned_at=datetime(2026, 5, 10, tzinfo=timezone.utc),
    )


class TestResolveFromRegistry:
    async def test_hits_registry(self) -> None:
        reg = FakeRegistry()
        reg.entries["sess-1"] = _entry("sess-1")
        backend = FakeBackend()
        r = BrowserResolver(registry=reg, backend=backend)  # type: ignore[arg-type]

        result = await r.resolve("sess-1", expected_org_id="org-1")
        assert isinstance(result, ResolvedBrowser)
        assert result.session_id == "sess-1"
        assert result.endpoint.rest_url == "http://browser-sess-1.svc:10001"
        assert backend.calls == []  # never asked the backend

    async def test_tenant_mismatch_returns_none(self) -> None:
        reg = FakeRegistry()
        reg.entries["sess-1"] = _entry("sess-1", org="org-OWN")
        r = BrowserResolver(registry=reg, backend=FakeBackend())  # type: ignore[arg-type]

        # Wrong org → treat as not found.
        assert await r.resolve("sess-1", expected_org_id="org-OTHER") is None


class TestFallbackToBackend:
    async def test_uses_backend_when_registry_misses(self) -> None:
        reg = FakeRegistry()  # empty
        backend = FakeBackend()
        backend.found["sess-1"] = BrowserEntry(
            session_id="sess-1", org_id="org-1", user_id="user-1",
            rest_url="http://browser-x.svc:10001",
            cdp_url="ws://browser-x.svc:9222",
            live_view_url="ws://browser-x.svc:443",
            provisioned_at=datetime(2026, 5, 10, tzinfo=timezone.utc),
        )
        r = BrowserResolver(registry=reg, backend=backend)  # type: ignore[arg-type]

        result = await r.resolve("sess-1", expected_org_id="org-1")
        assert result is not None
        assert result.endpoint.rest_url == "http://browser-x.svc:10001"
        assert result.org_id == "org-1"
        assert result.source == "k8s_fallback"

    async def test_backend_tenant_mismatch_returns_none(self) -> None:
        backend = FakeBackend()
        backend.found["sess-1"] = _entry("sess-1", org="org-OTHER")
        r = BrowserResolver(registry=FakeRegistry(), backend=backend)  # type: ignore[arg-type]

        assert await r.resolve("sess-1", expected_org_id="org-1") is None

    async def test_returns_none_when_neither_path_finds(self) -> None:
        r = BrowserResolver(
            registry=FakeRegistry(), backend=FakeBackend(),  # type: ignore[arg-type]
        )
        assert await r.resolve("never", expected_org_id="org-1") is None


class TestNoBackend:
    async def test_no_backend_means_registry_only(self) -> None:
        # Process backend doesn't expose find_by_session, so resolver
        # accepts None and degrades to registry-only.
        reg = FakeRegistry()
        reg.entries["sess-1"] = _entry("sess-1")
        r = BrowserResolver(registry=reg, backend=None)
        result = await r.resolve("sess-1", expected_org_id="org-1")
        assert result is not None
```

- [ ] **Step 2: Run** — 5 FAIL.

- [ ] **Step 3: Implement**

`surogates/browser/resolver.py`:

```python
"""BrowserResolver — find a session's browser pod across processes.

API server flow:
  1. Resolve via :class:`BrowserRegistry` (Redis hash, written by the worker
     when it provisions).
  2. If the registry misses (Redis was flushed, the entry was evicted, or
     the API server is faster than the worker on a write race), fall back
     to a label-keyed K8s lookup via the backend's ``find_entry_by_session``.
  3. If both miss, return None.

Tenant scoping: when ``expected_org_id`` is supplied, registry hits whose
``org_id`` doesn't match are treated as misses. The fallback path reconstructs
the same metadata from K8s labels and applies the same comparison before it
returns an endpoint.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

from surogates.browser.base import BrowserEndpoint
from surogates.browser.registry import BrowserEntry, BrowserRegistry

logger = logging.getLogger(__name__)


class _BackendWithFind(Protocol):
    async def find_entry_by_session(self, session_id: str) -> BrowserEntry | None: ...


@dataclass(slots=True)
class ResolvedBrowser:
    session_id: str
    endpoint: BrowserEndpoint
    org_id: str | None = None
    user_id: str | None = None
    source: str = "registry"     # "registry" | "k8s_fallback"


class BrowserResolver:
    def __init__(
        self,
        *,
        registry: BrowserRegistry,
        backend: _BackendWithFind | None,
    ) -> None:
        self._registry = registry
        self._backend = backend

    async def resolve(
        self,
        session_id: str,
        *,
        expected_org_id: str | None,
    ) -> ResolvedBrowser | None:
        entry = await self._registry.get(session_id)
        if entry is not None:
            if expected_org_id is not None and entry.org_id != expected_org_id:
                logger.warning(
                    "Browser registry hit for session %s but org %s != expected %s",
                    session_id, entry.org_id, expected_org_id,
                )
                return None
            return ResolvedBrowser(
                session_id=entry.session_id,
                endpoint=BrowserEndpoint(
                    rest_url=entry.rest_url,
                    cdp_url=entry.cdp_url,
                    live_view_url=entry.live_view_url,
                ),
                org_id=entry.org_id,
                user_id=entry.user_id,
                source="registry",
            )

        if self._backend is None:
            return None

        fallback_entry = await self._backend.find_entry_by_session(session_id)
        if fallback_entry is None:
            return None
        if (
            expected_org_id is not None
            and fallback_entry.org_id != expected_org_id
        ):
            logger.warning(
                "Browser K8s fallback hit for session %s but org %s != expected %s",
                session_id, fallback_entry.org_id, expected_org_id,
            )
            return None
        return ResolvedBrowser(
            session_id=fallback_entry.session_id,
            endpoint=BrowserEndpoint(
                rest_url=fallback_entry.rest_url,
                cdp_url=fallback_entry.cdp_url,
                live_view_url=fallback_entry.live_view_url,
            ),
            org_id=fallback_entry.org_id,
            user_id=fallback_entry.user_id,
            source="k8s_fallback",
        )
```

In `surogates/browser/kubernetes.py`, add a metadata-preserving wrapper
around Phase B's `find_by_session`:

```python
from datetime import datetime, timezone
from surogates.browser.registry import BrowserEntry

async def find_entry_by_session(self, session_id: str) -> BrowserEntry | None:
    api = await self._get_api()
    selector = f"app=surogates-browser,surogates.ai/session-id={session_id}"
    result = await api.list_namespaced_pod(
        self._namespace,
        label_selector=selector,
    )
    items = list(getattr(result, "items", []) or [])
    if not items:
        return None

    pod = items[0]
    labels = pod.metadata.labels or {}
    browser_id = labels.get("surogates.ai/browser-id")
    org_id = labels.get("surogates.ai/org-id")
    user_id = labels.get("surogates.ai/user-id")
    service_name = pod.metadata.name
    if not browser_id or not org_id or not user_id or not service_name:
        return None

    return BrowserEntry(
        session_id=session_id,
        org_id=org_id,
        user_id=user_id,
        rest_url=f"http://{service_name}.{self._namespace}.svc:{SERVICE_PORT_REST}",
        cdp_url=f"ws://{service_name}.{self._namespace}.svc:{SERVICE_PORT_CDP}",
        live_view_url=f"ws://{service_name}.{self._namespace}.svc:{SERVICE_PORT_LIVE_VIEW}",
        provisioned_at=datetime.now(timezone.utc),
    )
```

Keep Phase B's `find_by_session` method intact for existing tests; implement
it by delegating to `find_entry_by_session` and returning
`(browser_id, BrowserEndpoint)` if useful, or leave the existing code as-is.

- [ ] **Step 4: Run** — all PASS.

- [ ] **Step 5: Commit**

```bash
git add surogates/browser/resolver.py surogates/browser/kubernetes.py tests/test_browser_resolver.py
git commit -m "feat(browser): add BrowserResolver with registry + K8s fallback"
```

---

## Task 3: RFB frame gating helper

**Files:**
- Create: `surogates/browser/rfb.py`
- Test: `tests/test_browser_rfb.py`

The proxy needs to drop NoVNC ClientMessage frames of types 4 (KeyEvent),
5 (PointerEvent), and 6 (ClientCutText) when the connecting user does not
hold control. Other types (0 SetPixelFormat, 2 SetEncodings, 3 FramebufferUpdateRequest)
are forwarded so the read-only client can still negotiate. This task isolates
that single-byte sniff into a tiny pure helper that the WS proxy in Task 7
plugs in.

> **Note on framing:** noVNC over WebSocket sends one RFB ClientMessage per
> WS frame in its standard configuration, so a single byte-0 peek is
> sufficient. If a future RFB extension splits a ClientMessage across
> multiple WS frames, this helper would need a stream-aware variant; we
> leave that as future work.

- [ ] **Step 1: Write the failing test**

`tests/test_browser_rfb.py`:

```python
"""Tests for surogates.browser.rfb (RFB ClientMessage gating)."""

from __future__ import annotations

from surogates.browser.rfb import RFB_INPUT_TYPES, is_input_frame


class TestIsInputFrame:
    def test_key_event_is_input(self) -> None:
        # Type 4 (KeyEvent), padding, downflag, key (uint32). 8 bytes total.
        frame = bytes([4]) + bytes(7)
        assert is_input_frame(frame) is True

    def test_pointer_event_is_input(self) -> None:
        frame = bytes([5]) + bytes(5)
        assert is_input_frame(frame) is True

    def test_client_cut_text_is_input(self) -> None:
        frame = bytes([6]) + bytes(7)
        assert is_input_frame(frame) is True

    def test_set_pixel_format_is_not_input(self) -> None:
        # Type 0 — clients may renegotiate format even read-only.
        frame = bytes([0]) + bytes(19)
        assert is_input_frame(frame) is False

    def test_set_encodings_is_not_input(self) -> None:
        frame = bytes([2]) + bytes(7)
        assert is_input_frame(frame) is False

    def test_framebuffer_update_request_is_not_input(self) -> None:
        frame = bytes([3]) + bytes(9)
        assert is_input_frame(frame) is False

    def test_empty_frame_is_not_input(self) -> None:
        # An empty WS frame can't be an RFB ClientMessage; let it pass.
        assert is_input_frame(b"") is False

    def test_input_types_set(self) -> None:
        assert RFB_INPUT_TYPES == frozenset({4, 5, 6})
```

- [ ] **Step 2: Run** — 8 FAIL.

- [ ] **Step 3: Implement**

`surogates/browser/rfb.py`:

```python
"""RFB ClientMessage type gating for the live-view WebSocket proxy.

The kernel-images browser pod runs noVNC bound to the framebuffer.
ClientMessage types we drop when the connecting user does not hold
browser control:

- 4  KeyEvent       — keyboard input
- 5  PointerEvent   — mouse input
- 6  ClientCutText  — clipboard paste (also user-originating)

ClientMessage types we always forward:

- 0  SetPixelFormat
- 2  SetEncodings
- 3  FramebufferUpdateRequest

See spec §7.2.
"""

from __future__ import annotations

RFB_INPUT_TYPES: frozenset[int] = frozenset({4, 5, 6})


def is_input_frame(frame: bytes) -> bool:
    """True iff *frame* is an RFB ClientMessage that requires write
    access (KeyEvent, PointerEvent, or ClientCutText).

    Empty frames return False — they cannot be RFB messages and the
    upstream WS protocol may legitimately send a 0-byte ping/keepalive.
    """
    if not frame:
        return False
    return frame[0] in RFB_INPUT_TYPES
```

- [ ] **Step 4: Run** — all PASS.

- [ ] **Step 5: Commit**

```bash
git add surogates/browser/rfb.py tests/test_browser_rfb.py
git commit -m "feat(browser): add RFB ClientMessage input-frame gating helper"
```

---

## Task 4: REST — `GET /v1/sessions/{id}/browser/state`

**Files:**
- Create: `surogates/api/routes/browser.py`
- Modify: `surogates/api/app.py` (register the router)
- Test: `tests/test_browser_route.py`

- [ ] **Step 1: Write the failing test**

`tests/test_browser_route.py`:

```python
"""REST endpoints for the browser live-view + control surfaces."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.fixture()
def app_factory(monkeypatch, session_factory):
    """Build an isolated FastAPI app with a stubbed BrowserResolver
    + BrowserControlStore + SessionStore.

    Do not rely on create_app constructor kwargs; the real app factory
    currently takes no dependency-injection parameters. Build the normal
    app and override app.state after construction, matching the existing
    tests/integration/test_api.py fixture style.
    """

    from surogates.browser.base import BrowserEndpoint
    from surogates.browser.resolver import ResolvedBrowser
    from surogates.api.app import create_app

    class StubResolver:
        def __init__(self) -> None:
            self.entries: dict[str, ResolvedBrowser] = {}

        async def resolve(self, session_id: str, *, expected_org_id: str | None):
            entry = self.entries.get(session_id)
            if entry is None:
                return None
            if expected_org_id is not None and entry.org_id != expected_org_id:
                return None
            return entry

    class StubControl:
        def __init__(self) -> None:
            self.flag: dict[str, str] = {}  # session_id -> owner_user_id

        async def held_by(self, session_id: str) -> str | None:
            return self.flag.get(session_id)

    resolver = StubResolver()
    control = StubControl()

    def _build():
        app = create_app()
        app.state.session_factory = session_factory
        app.state.browser_resolver = resolver
        app.state.browser_control = control
        return app

    return _build, resolver, control


@pytest.fixture()
def jwt_for_session(app_factory):
    # Reuse create_access_token from tenant.auth.jwt to mint a token
    # tied to the test org_id/user_id; pattern mirrors test_api.py.
    from surogates.tenant.auth.jwt import create_access_token

    def make(*, org_id: str, user_id: str) -> str:
        return create_access_token(
            org_id=org_id, user_id=user_id, permissions=set(),
        )

    return make


class TestStateEndpoint:
    async def test_returns_404_when_no_browser(self, app_factory, jwt_for_session) -> None:
        build, resolver, control = app_factory
        app = build()
        sid = str(uuid4())
        token = jwt_for_session(org_id="org-1", user_id="user-1")

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get(
                f"/v1/sessions/{sid}/browser/state",
                headers={"Authorization": f"Bearer {token}"},
            )
        assert r.status_code == 404

    async def test_returns_state_when_browser_live(
        self, app_factory, jwt_for_session
    ) -> None:
        from surogates.browser.base import BrowserEndpoint
        from surogates.browser.resolver import ResolvedBrowser

        build, resolver, control = app_factory
        sid = str(uuid4())
        resolver.entries[sid] = ResolvedBrowser(
            session_id=sid,
            endpoint=BrowserEndpoint(
                rest_url="http://browser-x.svc:10001",
                cdp_url="ws://browser-x.svc:9222",
                live_view_url="ws://browser-x.svc:443",
            ),
            org_id="org-1", user_id="user-1", source="registry",
        )

        app = build()
        token = jwt_for_session(org_id="org-1", user_id="user-1")
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get(
                f"/v1/sessions/{sid}/browser/state",
                headers={"Authorization": f"Bearer {token}"},
            )
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "live"
        assert body["control_owner"] is None
        assert body["live_view_path"].endswith(f"/browser/live/")

    async def test_state_reports_user_control(
        self, app_factory, jwt_for_session
    ) -> None:
        from surogates.browser.base import BrowserEndpoint
        from surogates.browser.resolver import ResolvedBrowser

        build, resolver, control = app_factory
        sid = str(uuid4())
        resolver.entries[sid] = ResolvedBrowser(
            session_id=sid,
            endpoint=BrowserEndpoint(rest_url="r", cdp_url="c", live_view_url="l"),
            org_id="org-1", user_id="user-1", source="registry",
        )
        control.flag[sid] = "user-1"

        app = build()
        token = jwt_for_session(org_id="org-1", user_id="user-1")
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get(
                f"/v1/sessions/{sid}/browser/state",
                headers={"Authorization": f"Bearer {token}"},
            )
        body = r.json()
        assert body["status"] == "user-control"
        assert body["control_owner"] == "user-1"

    async def test_other_org_gets_404(self, app_factory, jwt_for_session) -> None:
        from surogates.browser.base import BrowserEndpoint
        from surogates.browser.resolver import ResolvedBrowser

        build, resolver, control = app_factory
        sid = str(uuid4())
        resolver.entries[sid] = ResolvedBrowser(
            session_id=sid,
            endpoint=BrowserEndpoint(rest_url="r", cdp_url="c", live_view_url="l"),
            org_id="org-OWN", user_id="user-1", source="registry",
        )

        app = build()
        token = jwt_for_session(org_id="org-OTHER", user_id="user-1")
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get(
                f"/v1/sessions/{sid}/browser/state",
                headers={"Authorization": f"Bearer {token}"},
            )
        assert r.status_code == 404   # tenant mismatch; treat as not-found
```

> Note: existing tests use `tests/integration/test_api.py::_create_test_tenant`
> and similar helpers; reuse those helpers where possible. If direct import
> would create an integration-test dependency cycle, copy only the minimal
> tenant fixture code into this test file.

- [ ] **Step 2: Run** — all FAIL.

- [ ] **Step 3: Implement the route**

`surogates/api/routes/browser.py`:

```python
"""Browser live-view + control endpoints (Phase C).

Routes:
  GET  /v1/sessions/{id}/browser/state
  GET  /v1/api/sessions/{id}/browser/state
                                      — provision status + control owner
  POST /v1/sessions/{id}/browser/control
  POST /v1/api/sessions/{id}/browser/control
                                      — acquire / release (Task 5)
  GET  /v1/sessions/{id}/browser/live/{path:path}
  GET  /v1/api/sessions/{id}/browser/live/{path:path}
                                          — HTTP/WS proxy (Tasks 6-8)

Resolution: every request goes through ``BrowserResolver`` (Registry
primary, K8s fallback) and is tenant-scoped to the caller's ``org_id``.
"""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from surogates.tenant.auth.middleware import get_current_tenant
from surogates.tenant.context import TenantContext

logger = logging.getLogger(__name__)

router = APIRouter()


class BrowserStateResponse(BaseModel):
    status: str  # "live" | "user-control"
    control_owner: str | None
    rest_url: str  # cluster-internal; SPA never uses this
    live_view_path: str  # frontend opens this relative path


@router.get(
    "/sessions/{session_id}/browser/state",
    response_model=BrowserStateResponse,
)
@router.get(
    "/api/sessions/{session_id}/browser/state",
    response_model=BrowserStateResponse,
)
async def get_browser_state(
    session_id: UUID,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> BrowserStateResponse:
    resolver = request.app.state.browser_resolver
    control = request.app.state.browser_control

    resolved = await resolver.resolve(
        str(session_id), expected_org_id=str(tenant.org_id),
    )
    if resolved is None:
        raise HTTPException(status_code=404, detail="No browser for session")

    holder = await control.held_by(str(session_id))

    return BrowserStateResponse(
        status="user-control" if holder else "live",
        control_owner=holder,
        rest_url=resolved.endpoint.rest_url,
        live_view_path=f"{'/v1/api' if request.url.path.startswith('/v1/api/') else '/v1'}/sessions/{session_id}/browser/live/",
    )
```

- [ ] **Step 4: Wire the router into `app.py`**

Register the router in `surogates/api/app.py` alongside the other `/v1`
routers. Do not add constructor kwargs to `create_app`; app dependencies are
created in the lifespan startup path and tests override `app.state` directly.

```python
from surogates.api.routes import browser as browser_routes

app.include_router(browser_routes.router, prefix="/v1", tags=["browser"])
```

- [ ] **Step 5: Run** — `pytest tests/test_browser_route.py -v` → 4 PASS.

- [ ] **Step 6: Commit**

```bash
git add surogates/api/routes/browser.py surogates/api/app.py \
        tests/test_browser_route.py
git commit -m "feat(browser): add GET /sessions/{id}/browser/state"
```

---

## Task 5: REST — `POST /v1/sessions/{id}/browser/control` (acquire / release)

**Files:**
- Modify: `surogates/api/routes/browser.py`
- Test: `tests/test_browser_route.py` (extend)

Three-branch acquire semantics (spec §7.3):

- **Unheld:** set entry, emit `BROWSER_CONTROL_GRANTED`, return `200`.
- **Held by same user:** idempotent refresh, no event re-emit, `200`.
- **Held by different user:** `409 Conflict` with the holder's identity.

Release: clears the flag, emits `BROWSER_CONTROL_RETURNED`, queues a wake
on the session via the orchestrator's Redis queue (same helper clarify
uses).

- [ ] **Step 1: Write the failing tests** — append:

```python
class TestControlEndpoint:
    async def test_acquire_when_unheld(self, app_factory, jwt_for_session) -> None:
        from surogates.browser.base import BrowserEndpoint
        from surogates.browser.control import AcquireOutcome, ControlEntry
        from surogates.browser.resolver import ResolvedBrowser

        build, resolver, control = app_factory
        sid = str(uuid4())
        resolver.entries[sid] = ResolvedBrowser(
            session_id=sid,
            endpoint=BrowserEndpoint(rest_url="r", cdp_url="c", live_view_url="l"),
            org_id="org-1", user_id="user-1", source="registry",
        )
        # Stub the control store to record acquire calls.
        events: list[tuple[str, str, dict]] = []

        async def fake_acquire(session_id: str, user_id: str):
            return AcquireOutcome.GRANTED, ControlEntry(
                owner_user_id=user_id, acquired_at=datetime.now(timezone.utc),
            )

        async def fake_held_by(session_id: str):
            return None

        control.acquire = fake_acquire  # type: ignore[attr-defined]
        control.held_by = fake_held_by  # type: ignore[attr-defined]

        app = build()
        # Inject an event-emitter that records what would be written.
        async def fake_emit(sid_, event_type, data):
            events.append((sid_, event_type, data))

        app.state.session_event_emitter = fake_emit
        app.state.session_wake = lambda sid: events.append((sid, "wake", {}))

        token = jwt_for_session(org_id="org-1", user_id="user-1")
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post(
                f"/v1/sessions/{sid}/browser/control",
                json={"action": "acquire"},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert r.status_code == 200
        body = r.json()
        assert body["outcome"] == "granted"
        # Event was emitted.
        types = [t for _, t, _ in events]
        assert "browser.control_granted" in types

    async def test_acquire_same_user_does_not_re_emit(
        self, app_factory, jwt_for_session, monkeypatch
    ) -> None:
        from surogates.browser.control import AcquireOutcome, ControlEntry
        build, resolver, control = app_factory
        sid = str(uuid4())
        _seed_browser(resolver, sid, org_id="org-1", user_id="user-1")
        events: list[tuple[str, str, dict]] = []
        control.acquire = _acquire_stub(AcquireOutcome.REFRESHED, "user-1")  # type: ignore[attr-defined]
        app = build()
        app.state.session_event_emitter = _event_recorder(events)
        app.state.session_wake = _wake_noop
        token = jwt_for_session(org_id="org-1", user_id="user-1")
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post(
                f"/v1/sessions/{sid}/browser/control",
                json={"action": "acquire"},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert r.status_code == 200
        assert r.json()["outcome"] == "refreshed"
        assert [t for _, t, _ in events] == []

    async def test_acquire_different_user_returns_409(
        self, app_factory, jwt_for_session, monkeypatch
    ) -> None:
        from surogates.browser.control import AcquireOutcome
        build, resolver, control = app_factory
        sid = str(uuid4())
        _seed_browser(resolver, sid, org_id="org-1", user_id="user-2")
        control.acquire = _acquire_stub(AcquireOutcome.CONFLICT, "user-2")  # type: ignore[attr-defined]
        app = build()
        app.state.session_event_emitter = _event_recorder([])
        app.state.session_wake = _wake_noop
        token = jwt_for_session(org_id="org-1", user_id="user-1")
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post(
                f"/v1/sessions/{sid}/browser/control",
                json={"action": "acquire"},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert r.status_code == 409
        assert r.json()["detail"]["holder_user_id"] == "user-2"

    async def test_release_owner_succeeds_and_wakes(
        self, app_factory, jwt_for_session
    ) -> None:
        build, resolver, control = app_factory
        sid = str(uuid4())
        _seed_browser(resolver, sid, org_id="org-1", user_id="user-1")
        events: list[tuple[str, str, dict]] = []
        wakes: list[str] = []
        control.release = _release_stub(True)  # type: ignore[attr-defined]
        app = build()
        app.state.session_event_emitter = _event_recorder(events)
        app.state.session_wake = _wake_recorder(wakes)
        token = jwt_for_session(org_id="org-1", user_id="user-1")
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post(
                f"/v1/sessions/{sid}/browser/control",
                json={"action": "release"},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert r.status_code == 200
        assert "browser.control_returned" in [t for _, t, _ in events]
        assert wakes == [sid]

    async def test_release_non_owner_returns_403(
        self, app_factory, jwt_for_session
    ) -> None:
        build, resolver, control = app_factory
        sid = str(uuid4())
        _seed_browser(resolver, sid, org_id="org-1", user_id="user-1")
        events: list[tuple[str, str, dict]] = []
        wakes: list[str] = []
        control.release = _release_stub(False)  # type: ignore[attr-defined]
        app = build()
        app.state.session_event_emitter = _event_recorder(events)
        app.state.session_wake = _wake_recorder(wakes)
        token = jwt_for_session(org_id="org-1", user_id="user-1")
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post(
                f"/v1/sessions/{sid}/browser/control",
                json={"action": "release"},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert r.status_code == 403
        assert events == []
        assert wakes == []
```

Add the helpers used by the tests above to the same test file:

```python
def _seed_browser(resolver, session_id: str, *, org_id: str, user_id: str) -> None:
    from surogates.browser.base import BrowserEndpoint
    from surogates.browser.resolver import ResolvedBrowser
    resolver.entries[session_id] = ResolvedBrowser(
        session_id=session_id,
        endpoint=BrowserEndpoint(rest_url="r", cdp_url="c", live_view_url="l"),
        org_id=org_id,
        user_id=user_id,
        source="registry",
    )

def _acquire_stub(outcome, owner_user_id: str):
    from datetime import datetime, timezone
    from surogates.browser.control import ControlEntry
    async def acquire(_session_id: str, _user_id: str):
        return outcome, ControlEntry(owner_user_id=owner_user_id, acquired_at=datetime.now(timezone.utc))
    return acquire

def _release_stub(value: bool):
    async def release(_session_id: str, _user_id: str) -> bool:
        return value
    return release

def _event_recorder(events: list[tuple[str, str, dict]]):
    async def emit(session_id: str, event_type: str, data: dict) -> None:
        events.append((session_id, event_type, data))
    return emit

def _wake_recorder(wakes: list[str]):
    async def wake(session_id: str) -> None:
        wakes.append(session_id)
    return wake

async def _wake_noop(_session_id: str) -> None:
    return None
```

- [ ] **Step 2: Run** — 5 FAIL.

- [ ] **Step 3: Implement**

Append to `surogates/api/routes/browser.py`:

```python
class ControlActionRequest(BaseModel):
    action: str  # "acquire" | "release"
    owner_user_id: str | None = None
    # owner_user_id is only used on /v1/api/* service-account proxy calls
    # from surogate-ops. Direct user JWT calls derive the owner from tenant.user_id.


@router.post("/api/sessions/{session_id}/browser/control")
@router.post("/sessions/{session_id}/browser/control")
async def post_browser_control(
    session_id: UUID,
    body: ControlActionRequest,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
):
    if body.action not in {"acquire", "release"}:
        raise HTTPException(400, detail="action must be 'acquire' or 'release'")

    resolver = request.app.state.browser_resolver
    control = request.app.state.browser_control
    emit = request.app.state.session_event_emitter
    wake = request.app.state.session_wake

    resolved = await resolver.resolve(
        str(session_id), expected_org_id=str(tenant.org_id),
    )
    if resolved is None:
        raise HTTPException(404, detail="No browser for session")

    owner_user_id = (
        body.owner_user_id
        if request.url.path.startswith("/v1/api/")
        else (str(tenant.user_id) if tenant.user_id is not None else None)
    )
    if owner_user_id is None:
        raise HTTPException(403, detail="browser control requires a user identity")

    if body.action == "acquire":
        from surogates.browser.control import AcquireOutcome
        outcome, entry = await control.acquire(str(session_id), owner_user_id)
        if outcome == AcquireOutcome.GRANTED:
            await emit(str(session_id), "browser.control_granted", {
                "session_id": str(session_id),
                "owner_user_id": entry.owner_user_id,
            })
            return {"outcome": "granted", "owner_user_id": entry.owner_user_id}
        if outcome == AcquireOutcome.REFRESHED:
            return {"outcome": "refreshed", "owner_user_id": entry.owner_user_id}
        # CONFLICT
        raise HTTPException(
            409, detail={
                "outcome": "conflict",
                "holder_user_id": entry.owner_user_id,
                "acquired_at": entry.acquired_at.isoformat(),
            },
        )

    # release
    released = await control.release(str(session_id), owner_user_id)
    if not released:
        raise HTTPException(403, detail="not the holder")
    await emit(str(session_id), "browser.control_returned", {
        "session_id": str(session_id),
        "released_by": str(tenant.user_id),
    })
    await wake(str(session_id))
    return {"outcome": "released"}
```

- [ ] **Step 4: Use async `session_event_emitter` and `session_wake` callables**

The route reads both callables from `app.state`; Task 10 wires the real
versions during lifespan startup. Tests assign fakes directly:

```python
async def fake_emit(session_id: str, event_type: str, data: dict) -> None:
    events.append((session_id, event_type, data))

async def fake_wake(session_id: str) -> None:
    wakes.append(session_id)

app.state.session_event_emitter = fake_emit
app.state.session_wake = fake_wake
```

The real wiring happens in `surogates/api/app.py` lifespan: emitter writes to
`SessionStore.emit_event`; wake calls `enqueue_session(redis, settings.agent_id,
session_id)`.

- [ ] **Step 5: Run** — all PASS.

- [ ] **Step 6: Commit**

```bash
git add surogates/api/routes/browser.py surogates/api/app.py \
        tests/test_browser_route.py
git commit -m "feat(browser): add POST /sessions/{id}/browser/control with conflict semantics"
```

---

## Task 6: Auth accepts `?token=<jwt>` only for SSE and browser live-view paths

**Files:**
- Modify: `surogates/tenant/auth/middleware.py`
- Test: extension to `tests/test_browser_route.py` or a focused unit test

Iframes can't easily send `Authorization` headers. To allow the SPA to
embed `<iframe src="/v1/sessions/{id}/browser/live/vnc.html?token=...">`,
the auth layer accepts a query-param JWT for live-view HTTP assets and the
live-view WebSocket. Keep the existing SSE exception for
`/v1/sessions/{id}/events` and `/v1/api/sessions/{id}/events`, but reject
query-param JWTs everywhere else. The wrapper module
`surogates/api/middleware/auth.py` only re-exports this implementation; do
not edit the wrapper.

- [ ] **Step 1: Write the failing test** — append:

```python
class TestQueryParamAuth:
    async def test_live_view_accepts_token_query_param(
        self, app_factory, jwt_for_session
    ) -> None:
        build, resolver, control = app_factory
        sid = str(uuid4())
        _seed_browser(resolver, sid, org_id="org-1", user_id="user-1")
        token = jwt_for_session(org_id="org-1", user_id="user-1")
        app = build()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get(
                f"/v1/sessions/{sid}/browser/live/vnc.html",
                params={"token": token},
            )
        # The proxy itself may 502 until Task 7; auth must not be the failure.
        assert r.status_code != 401

    async def test_other_paths_reject_token_query_param(
        self, app_factory, jwt_for_session
    ) -> None:
        build, resolver, control = app_factory
        sid = str(uuid4())
        _seed_browser(resolver, sid, org_id="org-1", user_id="user-1")
        token = jwt_for_session(org_id="org-1", user_id="user-1")
        app = build()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get(
                f"/v1/sessions/{sid}/browser/state",
                params={"token": token},
            )
        assert r.status_code == 401

    async def test_sse_still_accepts_token_query_param(
        self, app, jwt_for_session
    ) -> None:
        token = jwt_for_session(org_id="org-1", user_id="user-1")
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get(
                "/v1/sessions/00000000-0000-0000-0000-000000000001/events",
                params={"token": token},
            )
        assert r.status_code != 401
```

- [ ] **Step 2: Run** — 2 FAIL.

- [ ] **Step 3: Modify the real auth implementation**

In `surogates/tenant/auth/middleware.py`, add path helpers near the prefix
constants:

```python
_QUERY_TOKEN_PATH_RE = re.compile(
    r"^/v1/(api/)?sessions/[0-9a-f-]{36}/(?:events|browser/live/.*)$",
)

def _allows_query_token(path: str) -> bool:
    return bool(_QUERY_TOKEN_PATH_RE.match(path))

def _extract_header_or_allowed_query_token(request: Request) -> str:
    auth_header = headers.get("authorization")
    token = _extract_bearer(auth_header)
    if token:
        return token
    if _allows_query_token(request.url.path):
        return request.query_params.get("token", "")
    return ""
```

Use `_allows_query_token` in both `get_current_tenant` and the HTTP
middleware branch. Preserve service-account token checks exactly as they are.

Then add a WebSocket-specific helper in the same module for Task 8:

```python
async def authenticate_websocket_tenant(
    app: FastAPI,
    *,
    path: str,
    token: str | None,
) -> TenantContext:
    if not token or not _allows_query_token(path):
        raise HTTPException(status_code=401, detail="Missing authentication credentials.")
    # Reuse the JWT/service-account validation branches from get_current_tenant
    # by factoring the common token-to-context code into a private helper.
    return await _tenant_context_from_token(app.state.session_factory, token, app.state.settings.tenant_assets_root, path)
```

- [ ] **Step 4: Run** — query-token tests PASS and existing SSE tests still PASS.

- [ ] **Step 5: Commit**

```bash
git add surogates/tenant/auth/middleware.py tests/test_browser_route.py
git commit -m "feat(browser): accept ?token=<jwt> for live-view paths only"
```

---

## Task 7: HTTP proxy for NoVNC static assets

**Files:**
- Modify: `surogates/api/routes/browser.py`
- Test: `tests/test_browser_route.py` (extend)

NoVNC's HTML/JS/CSS lives at `vnc.html` and friends inside the kernel-images
container's port 6080. The SPA's iframe loads `vnc.html` over the proxy;
all subsequent asset fetches (`.js`, `.css`, `.svg`) hit the same proxy
prefix. Proxy reads the upstream response and forwards body + relevant
headers.

- [ ] **Step 1: Write the failing test** — append:

```python
class TestLiveViewHTTPProxy:
    async def test_vnc_html_is_proxied(
        self, app_factory, jwt_for_session, monkeypatch
    ) -> None:
        build, resolver, control = app_factory
        sid = str(uuid4())
        _seed_browser(resolver, sid, org_id="org-1", user_id="user-1")
        async def fake_request(self, method, url, **kwargs):
            return httpx.Response(200, text="<html>vnc</html>", headers={"content-type": "text/html"})
        monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)
        app = build()
        token = jwt_for_session(org_id="org-1", user_id="user-1")
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get(f"/v1/sessions/{sid}/browser/live/vnc.html", params={"token": token})
        assert r.status_code == 200
        assert r.text == "<html>vnc</html>"

    async def test_unknown_session_returns_404(self, app_factory, jwt_for_session) -> None:
        build, resolver, control = app_factory
        app = build()
        token = jwt_for_session(org_id="org-1", user_id="user-1")
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get(
                "/v1/sessions/00000000-0000-0000-0000-000000000001/browser/live/vnc.html",
                params={"token": token},
            )
        assert r.status_code == 404

    async def test_static_assets_use_token_query_param(
        self, app_factory, jwt_for_session
    ) -> None:
        build, resolver, control = app_factory
        sid = str(uuid4())
        _seed_browser(resolver, sid, org_id="org-1", user_id="user-1")
        seen: list[str] = []
        async def fake_request(self, method, url, **kwargs):
            seen.append(str(url))
            assert "token" not in kwargs.get("params", {})
            return httpx.Response(200, text="console.log('vnc')", headers={"content-type": "application/javascript"})
        monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)
        app = build()
        token = jwt_for_session(org_id="org-1", user_id="user-1")
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get(f"/v1/sessions/{sid}/browser/live/app.js", params={"token": token})
        assert r.status_code == 200
        assert seen and seen[0].endswith("/app.js")
```

- [ ] **Step 2: Run** — 3 FAIL.

- [ ] **Step 3: Implement** — append:

```python
import httpx as _httpx
from fastapi import Response


@router.api_route(
    "/api/sessions/{session_id}/browser/live/{path:path}",
    methods=["GET", "POST", "OPTIONS"],
)
@router.api_route(
    "/sessions/{session_id}/browser/live/{path:path}",
    methods=["GET", "POST", "OPTIONS"],
)
async def proxy_live_view(
    session_id: UUID,
    path: str,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
):
    resolver = request.app.state.browser_resolver
    resolved = await resolver.resolve(
        str(session_id), expected_org_id=str(tenant.org_id),
    )
    if resolved is None:
        raise HTTPException(404, detail="No browser for session")

    # The browser pod's NoVNC port on the cluster-internal Service:
    #   {scheme}://browser-{id}.{ns}.svc:443  (= targetPort 6080 inside pod)
    # We rebuild the upstream HTTP URL from the live_view_url field by
    # swapping the ws:// scheme for http://.
    upstream_base = resolved.endpoint.live_view_url.replace("ws://", "http://", 1)
    upstream_url = f"{upstream_base.rstrip('/')}/{path}" if path else f"{upstream_base.rstrip('/')}/"

    # Forward most headers; strip hop-by-hop and Authorization (the
    # upstream NoVNC has no auth).
    fwd_headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in {"host", "authorization", "cookie", "connection"}
    }

    body = await request.body()
    async with _httpx.AsyncClient(timeout=30.0) as client:
        r = await client.request(
            request.method, upstream_url,
            headers=fwd_headers,
            params={
                k: v for k, v in request.query_params.items()
                if k != "token"
            },
            content=body,
        )

    # Strip hop-by-hop response headers.
    resp_headers = {
        k: v for k, v in r.headers.items()
        if k.lower() not in {"connection", "transfer-encoding", "content-encoding"}
    }
    return Response(
        content=r.content,
        status_code=r.status_code,
        headers=resp_headers,
        media_type=r.headers.get("content-type"),
    )
```

- [ ] **Step 4: Run** — 3 PASS.

- [ ] **Step 5: Commit**

```bash
git add surogates/api/routes/browser.py tests/test_browser_route.py
git commit -m "feat(browser): proxy NoVNC static assets through the API server"
```

---

## Task 8: WebSocket proxy for the framebuffer + frame gating

**Files:**
- Modify: `surogates/api/routes/browser.py`
- Test: `tests/test_browser_route_ws.py` (new file)

This is the trickiest backend task. The proxy:

1. Accepts a WS upgrade at `/v1/sessions/{id}/browser/live/websockify`
   (NoVNC's standard WS endpoint).
2. Authenticates via query-param JWT using the WebSocket helper from Task 6;
   Starlette's HTTP middleware does not run for WebSocket scopes.
3. Resolves the browser pod via `BrowserResolver`.
4. Opens an upstream WS to `{live_view_url}/websockify`.
5. Pumps frames bidirectionally:
   - Upstream → client: forward all binary frames.
   - Client → upstream: read each frame; if `is_input_frame(...)` and
     `BrowserControlStore.held_by(session) != client_user`, drop. Otherwise
     forward.
6. Closes both sides on either disconnect.

- [ ] **Step 1: Write the failing test**

`tests/test_browser_route_ws.py`:

```python
"""WebSocket proxy with input-frame gating tests."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any
from uuid import uuid4

import pytest
import websockets
from httpx import ASGITransport, AsyncClient


# A fake upstream WS server that records what it receives and lets
# tests push frames toward the client.
class FakeUpstream:
    def __init__(self) -> None:
        self.received: list[bytes] = []
        self.outbound: asyncio.Queue[bytes] = asyncio.Queue()

    async def handler(self, ws):
        async def send_loop():
            while True:
                frame = await self.outbound.get()
                await ws.send(frame)

        async def recv_loop():
            async for frame in ws:
                if isinstance(frame, str):
                    frame = frame.encode()
                self.received.append(frame)

        send_task = asyncio.create_task(send_loop())
        try:
            await recv_loop()
        finally:
            send_task.cancel()


@pytest.fixture()
async def upstream_server():
    server = FakeUpstream()
    ws_server = await websockets.serve(server.handler, "127.0.0.1", 0)
    port = ws_server.sockets[0].getsockname()[1]
    try:
        yield server, f"ws://127.0.0.1:{port}"
    finally:
        ws_server.close()
        await ws_server.wait_closed()


# Stand up a tiny FastAPI app that exposes only the WS proxy route,
# pointing at the fake upstream.
@pytest.fixture()
def proxy_app(upstream_server, monkeypatch):
    from surogates.api.routes import browser as browser_routes
    from fastapi import FastAPI
    server, upstream_url = upstream_server

    app = FastAPI()
    app.include_router(browser_routes.router, prefix="/v1")
    app.state.browser_resolver = _Resolver(upstream_url)
    app.state.browser_control = _Control()
    app.state.session_factory = _session_factory_with_user()
    app.state.settings = SimpleNamespace(tenant_assets_root="/tmp/surogates-test")
    return app


class TestProxyWS:
    async def test_input_frames_dropped_when_no_control(
        self, proxy_app, upstream_server
    ) -> None:
        server, _ = upstream_server
        proxy_url = await _serve_app(proxy_app)  # helper that uvicorn-mounts the FastAPI app
        # Control NOT held — KeyEvent (type 4) must be dropped.
        ws_url = (
            proxy_url.replace("http", "ws", 1)
            + "/v1/sessions/00000000-0000-0000-0000-000000000001/browser/live/websockify"
            + "?token=" + _make_token(user="user-A")
        )
        async with websockets.connect(ws_url, subprotocols=["binary"]) as ws:
            await ws.send(bytes([4]) + bytes(7))   # KeyEvent
            # Give the proxy a tick to drop the frame.
            await asyncio.sleep(0.1)
        assert server.received == []

    async def test_input_frames_forwarded_when_control_held(
        self, proxy_app, upstream_server
    ) -> None:
        server, _ = upstream_server
        # Flip the control flag so user-A holds it.
        proxy_app.state.browser_control.flag["00000000-0000-0000-0000-000000000001"] = "user-A"

        proxy_url = await _serve_app(proxy_app)
        ws_url = (
            proxy_url.replace("http", "ws", 1)
            + "/v1/sessions/00000000-0000-0000-0000-000000000001/browser/live/websockify"
            + "?token=" + _make_token(user="user-A")
        )
        frame = bytes([4]) + bytes(7)
        async with websockets.connect(ws_url, subprotocols=["binary"]) as ws:
            await ws.send(frame)
            await asyncio.sleep(0.1)
        assert server.received == [frame]

    async def test_non_input_frames_always_forwarded(
        self, proxy_app, upstream_server
    ) -> None:
        server, _ = upstream_server
        proxy_url = await _serve_app(proxy_app)
        ws_url = (
            proxy_url.replace("http", "ws", 1)
            + "/v1/sessions/00000000-0000-0000-0000-000000000001/browser/live/websockify"
            + "?token=" + _make_token(user="user-A")
        )
        # SetEncodings (type 2) — must be forwarded even without control.
        frame = bytes([2]) + bytes(7)
        async with websockets.connect(ws_url, subprotocols=["binary"]) as ws:
            await ws.send(frame)
            await asyncio.sleep(0.1)
        assert server.received == [frame]

    async def test_upstream_to_client_always_forwards(
        self, proxy_app, upstream_server
    ) -> None:
        server, _ = upstream_server
        proxy_url = await _serve_app(proxy_app)
        ws_url = (
            proxy_url.replace("http", "ws", 1)
            + "/v1/sessions/00000000-0000-0000-0000-000000000001/browser/live/websockify"
            + "?token=" + _make_token(user="user-A")
        )
        # Push a fake framebuffer-update frame into the upstream queue
        # before the client connects.
        fb_frame = bytes([0]) + bytes(15)   # FramebufferUpdate (server -> client)
        await server.outbound.put(fb_frame)
        async with websockets.connect(ws_url, subprotocols=["binary"]) as ws:
            received = await asyncio.wait_for(ws.recv(), timeout=2.0)
        assert received == fb_frame

    async def test_close_propagates(self, proxy_app, upstream_server) -> None:
        server, _ = upstream_server
        proxy_url = await _serve_app(proxy_app)
        ws_url = (
            proxy_url.replace("http", "ws", 1)
            + "/v1/sessions/00000000-0000-0000-0000-000000000001/browser/live/websockify"
            + "?token=" + _make_token(user="user-A")
        )
        ws = await websockets.connect(ws_url, subprotocols=["binary"])
        await ws.close()
        # Upstream's recv loop should drop within a short window once the
        # proxy closes its side. We assert by trying to send and observing
        # the connection is gone — the upstream server context-managed
        # by the fixture will have cleaned up.
        await asyncio.sleep(0.2)


# Helpers used above ----------------------------------------------------------

async def _serve_app(app) -> str:
    """Mount *app* on uvicorn at a free port and return base URL.

    Use the standard uvicorn-in-asyncio pattern from
    tests/integration/conftest.py if a helper already exists; otherwise
    implement this as an async fixture that yields the base URL and shuts
    the server down in the fixture finalizer.
    """
    import uvicorn
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    serve_task = asyncio.create_task(server.serve())
    while not server.started:  # uvicorn sets this once bind succeeds
        await asyncio.sleep(0.01)
    sock = server.servers[0].sockets[0]
    base = f"http://{sock.getsockname()[0]}:{sock.getsockname()[1]}"
    return base


def _make_token(*, user: str) -> str:
    from surogates.tenant.auth.jwt import create_access_token
    return create_access_token(
        org_id="00000000-0000-0000-0000-000000000aaa",
        user_id=user, permissions=set(),
    )
```

- [ ] **Step 2: Run** — 5 FAIL.

- [ ] **Step 3: Implement**

Append to `surogates/api/routes/browser.py`:

```python
from fastapi import WebSocket, WebSocketDisconnect

import websockets as _websockets
from surogates.browser.rfb import is_input_frame
from surogates.tenant.auth.middleware import authenticate_websocket_tenant


@router.websocket("/api/sessions/{session_id}/browser/live/websockify")
@router.websocket("/sessions/{session_id}/browser/live/websockify")
async def proxy_live_view_ws(
    websocket: WebSocket,
    session_id: UUID,
):
    try:
        tenant = await authenticate_websocket_tenant(
            websocket.app,
            path=websocket.url.path,
            token=websocket.query_params.get("token"),
        )
    except HTTPException:
        await websocket.close(code=4401, reason="unauthenticated")
        return

    resolver = websocket.app.state.browser_resolver
    control = websocket.app.state.browser_control

    resolved = await resolver.resolve(
        str(session_id), expected_org_id=str(tenant.org_id),
    )
    if resolved is None:
        await websocket.close(code=4404, reason="no browser")
        return

    upstream_url = f"{resolved.endpoint.live_view_url}/websockify"
    try:
        upstream = await _websockets.connect(upstream_url, subprotocols=["binary"])
    except Exception as exc:
        logger.warning("Failed to connect upstream WS: %s", exc)
        await websocket.close(code=4502, reason="upstream unavailable")
        return

    await websocket.accept(subprotocol="binary")

    async def client_to_upstream() -> None:
        try:
            while True:
                frame = await websocket.receive_bytes()
                # Gate input frames per the control store.
                if is_input_frame(frame):
                    holder = await control.held_by(str(session_id))
                    if holder != str(tenant.user_id):
                        continue
                await upstream.send(frame)
        except WebSocketDisconnect:
            pass

    async def upstream_to_client() -> None:
        try:
            async for frame in upstream:
                if isinstance(frame, str):
                    frame = frame.encode()
                await websocket.send_bytes(frame)
        except _websockets.ConnectionClosed:
            pass

    try:
        await asyncio.gather(
            client_to_upstream(), upstream_to_client(),
            return_exceptions=False,
        )
    finally:
        with contextlib.suppress(Exception):
            await upstream.close()
        with contextlib.suppress(Exception):
            await websocket.close()
```

- [ ] **Step 4: Run** — all PASS.

- [ ] **Step 5: Commit**

```bash
git add surogates/api/routes/browser.py tests/test_browser_route_ws.py
git commit -m "feat(browser): WebSocket live-view proxy with input-frame gating"
```

---

## Task 9: Harness — one-time pause-injection on next iteration when user holds control

**Files:**
- Modify: `surogates/harness/loop.py`
- Test: `tests/test_browser_pause_injection.py`

Spec §7.3 step 5: on the next LLM iteration after the user takes control,
prepend a one-time system message ("The user has taken control of the
browser. Wait for them to finish before continuing.") so the LLM doesn't
spam more `browser_*` calls. Phase A's per-tool `paused_by_user` short-
circuit already prevents work, but doesn't tell the LLM *why* persistently —
without this injection a streamy LLM may try several tools before reading
the error.

The injection is one-time per held interval: track via `session.config`
("browser_pause_msg_injected": true) and clear it on `BROWSER_CONTROL_RETURNED`.

- [ ] **Step 1: Write the failing test**

`tests/test_browser_pause_injection.py`:

```python
"""Tests for the harness pause-message injection when user holds control."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

# The injection logic lives in a small standalone helper so we can test it
# without spinning up the full harness.
from surogates.harness.loop import maybe_inject_browser_pause


class TestInjection:
    async def test_injects_when_held_and_not_yet_injected(self) -> None:
        session = SimpleNamespace(
            id="sess-1",
            config={},
        )

        async def held_by(_: str) -> str | None:
            return "user-A"

        msg = await maybe_inject_browser_pause(
            session=session, browser_control=SimpleNamespace(held_by=held_by),
        )
        assert msg is not None
        assert "user has taken control" in msg.lower()
        assert session.config.get("browser_pause_msg_injected") is True

    async def test_no_inject_when_already_injected(self) -> None:
        session = SimpleNamespace(
            id="sess-1",
            config={"browser_pause_msg_injected": True},
        )

        async def held_by(_: str) -> str | None:
            return "user-A"

        msg = await maybe_inject_browser_pause(
            session=session, browser_control=SimpleNamespace(held_by=held_by),
        )
        assert msg is None

    async def test_clears_flag_when_no_longer_held(self) -> None:
        session = SimpleNamespace(
            id="sess-1",
            config={"browser_pause_msg_injected": True},
        )

        async def held_by(_: str) -> str | None:
            return None

        msg = await maybe_inject_browser_pause(
            session=session, browser_control=SimpleNamespace(held_by=held_by),
        )
        assert msg is None
        assert session.config.get("browser_pause_msg_injected") is False

    async def test_no_inject_when_no_control_store(self) -> None:
        session = SimpleNamespace(id="sess-1", config={})
        msg = await maybe_inject_browser_pause(
            session=session, browser_control=None,
        )
        assert msg is None
```

- [ ] **Step 2: Run** — 4 FAIL.

- [ ] **Step 3: Implement**

In `surogates/harness/loop.py`, add a small helper near the top:

```python
async def maybe_inject_browser_pause(
    *,
    session: Any,
    browser_control: "BrowserControlStore | None",
) -> str | None:
    """If the user holds browser control and we haven't injected the
    pause notice yet, return the system message string and mark the flag.
    If the user no longer holds control, clear the flag.

    Caller (the wake loop) prepends the message to the next LLM call.
    """
    if browser_control is None:
        return None

    holder = await browser_control.held_by(str(session.id))
    config = session.config if isinstance(session.config, dict) else {}

    if holder is not None:
        if config.get("browser_pause_msg_injected"):
            return None
        config["browser_pause_msg_injected"] = True
        return (
            "The user has taken control of the browser. Wait for them to "
            "finish before continuing — every browser_* tool call will "
            "return paused_by_user until they release control."
        )

    if config.get("browser_pause_msg_injected"):
        config["browser_pause_msg_injected"] = False
    return None
```

Wire the call into the wake loop just before the system prompt is built;
prepend the returned message to the system prompt's messages array. The
exact insertion point is in `AgentHarness.wake` where `messages` is
assembled — search for `system_prompt` in `loop.py` to find it.

- [ ] **Step 4: Run** — all 4 PASS.

- [ ] **Step 5: Commit**

```bash
git add surogates/harness/loop.py tests/test_browser_pause_injection.py
git commit -m "feat(browser): inject one-time pause notice when user holds control"
```

---

## Task 10: API server bootstrap — instantiate `BrowserResolver`, `BrowserControlStore`, emitter, wake

**Files:**
- Modify: `surogates/api/app.py`

The API server entry point must build the dependencies and pass them to
route handlers through `app.state`. The current API factory is
`surogates/api/app.py:create_app`; `surogates/api/__init__.py` is not the
dependency bootstrap point.

- [ ] **Step 1: Verify the API bootstrap point**

```bash
grep -n "async def lifespan\\|def create_app\\|SessionStore" surogates/api/app.py
```

Expected: `lifespan(app)` creates Redis, `SessionStore`, and other app-state
dependencies.

- [ ] **Step 2: Add the browser bootstrap inside `lifespan` after Redis and SessionStore**

```python
from surogates.browser.control import BrowserControlStore
from surogates.browser.registry import BrowserRegistry
from surogates.browser.resolver import BrowserResolver
from surogates.config import enqueue_session

# Optional K8s backend for the fallback path.
backend = None
if settings.browser.backend == "kubernetes":
    from surogates.browser.kubernetes import K8sBrowserBackend
    backend = K8sBrowserBackend(
        namespace=settings.browser.k8s_namespace,
        service_account=settings.browser.k8s_service_account,
        pod_ready_timeout=settings.browser.pod_ready_timeout,
        image=settings.browser.image,
    )

redis_client = app.state.redis
browser_registry = BrowserRegistry(redis_client)
browser_control = BrowserControlStore(redis_client)
browser_resolver = BrowserResolver(registry=browser_registry, backend=backend)


async def emit_session_event(session_id: str, event_type: str, data: dict) -> None:
    from uuid import UUID
    from surogates.session.events import EventType
    await app.state.session_store.emit_event(UUID(session_id), EventType(event_type), data)


async def wake_session(session_id: str) -> None:
    await enqueue_session(app.state.redis, settings.agent_id, session_id)


app.state.browser_resolver = browser_resolver
app.state.browser_control = browser_control
app.state.session_event_emitter = emit_session_event
app.state.session_wake = wake_session
```

- [ ] **Step 3: Verify the API server starts cleanly**

```bash
SUROGATES_BROWSER_BACKEND=process \
uv run uvicorn surogates.api:app --port 8001 &
sleep 1
curl -s http://localhost:8001/health
kill %1
```

Expected: `{"status":"ok"}` from the health check.

- [ ] **Step 4: Commit**

```bash
git add surogates/api/app.py
git commit -m "feat(browser): wire BrowserResolver/Control/wake into API bootstrap"
```

---

## Task 11: SDK — add `browser.*` event types and pane-state plumbing

**Files (all under `sdk/agent-chat-react/`):**
- Modify: `src/types.ts`
- Modify: `src/runtime/events.ts`
- Modify: `src/runtime/reducer.ts`
- Test: `tests/reducer.test.ts` (extend)

- [ ] **Step 1: Write the failing test** — append to `tests/reducer.test.ts`:

```typescript
import { describe, expect, it } from "vitest";
import { initialChatState, chatReducer } from "../src/runtime/reducer";

describe("browser events", () => {
  it("folds browser.provisioned into pane state", () => {
    const next = chatReducer(initialChatState, {
      kind: "sse",
      payload: {
        type: "browser.provisioned",
        eventId: 1,
        data: { session_id: "s", browser_id: "b" },
      },
    });
    expect(next.browser?.status).toBe("live");
  });

  it("transitions to user-control on browser.control_granted", () => {
    let s = chatReducer(initialChatState, {
      kind: "sse",
      payload: { type: "browser.provisioned", eventId: 1, data: {} },
    });
    s = chatReducer(s, {
      kind: "sse",
      payload: {
        type: "browser.control_granted",
        eventId: 2,
        data: { owner_user_id: "user-A" },
      },
    });
    expect(s.browser?.status).toBe("user-control");
    expect(s.browser?.controlOwner).toBe("user-A");
  });

  it("returns to live on browser.control_returned", () => {
    let s = chatReducer(initialChatState, {
      kind: "sse",
      payload: { type: "browser.provisioned", eventId: 1, data: {} },
    });
    s = chatReducer(s, {
      kind: "sse",
      payload: { type: "browser.control_granted", eventId: 2, data: { owner_user_id: "user-A" } },
    });
    s = chatReducer(s, {
      kind: "sse",
      payload: { type: "browser.control_returned", eventId: 3, data: {} },
    });
    expect(s.browser?.status).toBe("live");
    expect(s.browser?.controlOwner).toBeNull();
  });

  it("clears pane on browser.destroyed", () => {
    let s = chatReducer(initialChatState, {
      kind: "sse",
      payload: { type: "browser.provisioned", eventId: 1, data: {} },
    });
    s = chatReducer(s, {
      kind: "sse",
      payload: { type: "browser.destroyed", eventId: 2, data: {} },
    });
    expect(s.browser).toBeNull();
  });
});
```

- [ ] **Step 2: Run** — `pnpm --filter @invergent/agent-chat-react test reducer.test.ts` → 4 FAIL.

- [ ] **Step 3: Extend `AgentChatEventType` in `src/types.ts`**

```typescript
export type AgentChatEventType =
  | "user.message"
  // ... existing entries ...
  | "browser.provisioned"
  | "browser.destroyed"
  | "browser.control_granted"
  | "browser.control_returned";
```

Then add the pane state shape:

```typescript
export interface AgentChatBrowserState {
  status: "provisioning" | "live" | "user-control" | "closed";
  controlOwner: string | null;
}

export interface AgentChatState {
  // ... existing fields ...
  browser: AgentChatBrowserState | null;
}
```

- [ ] **Step 4: Update the reducer** in `src/runtime/reducer.ts`:

```typescript
case "browser.provisioned":
  return { ...state, browser: { status: "live", controlOwner: null } };
case "browser.control_granted":
  return {
    ...state,
    browser: {
      ...(state.browser ?? { status: "live", controlOwner: null }),
      status: "user-control",
      controlOwner: (event.data.owner_user_id as string | undefined) ?? null,
    },
  };
case "browser.control_returned":
  return {
    ...state,
    browser: {
      ...(state.browser ?? { status: "live", controlOwner: null }),
      status: "live",
      controlOwner: null,
    },
  };
case "browser.destroyed":
  return { ...state, browser: null };
```

- [ ] **Step 5: Surface inline markers in the message stream**

Per spec §8.4, lifecycle events also render as one-line inline markers
("⚡ browser ready", "⚠ user took control", "⚡ control returned to agent",
"⚡ browser closed"). The reducer pushes a synthetic system-kind message
into the messages array alongside the pane-state update.

Append to `tests/reducer.test.ts`:

```typescript
  it("appends an inline marker for browser.provisioned", () => {
    const next = chatReducer(initialChatState, {
      kind: "sse",
      payload: { type: "browser.provisioned", eventId: 1, data: {} },
    });
    const last = next.messages[next.messages.length - 1];
    expect(last.role).toBe("system");
    expect(last.systemKind).toBe("browser_marker");
    expect(last.content).toMatch(/browser ready/i);
  });

  it("appends a warning marker for browser.control_granted", () => {
    let s = chatReducer(initialChatState, {
      kind: "sse",
      payload: { type: "browser.provisioned", eventId: 1, data: {} },
    });
    s = chatReducer(s, {
      kind: "sse",
      payload: {
        type: "browser.control_granted", eventId: 2,
        data: { owner_user_id: "user-A" },
      },
    });
    const last = s.messages[s.messages.length - 1];
    expect(last.systemKind).toBe("browser_marker_warning");
    expect(last.content).toMatch(/took control/i);
  });
```

Add the marker-pushing logic next to the existing browser cases in the
reducer:

```typescript
function browserMarker(content: string, warning = false): AgentChatMessage {
  return {
    id: `browser-marker-${Date.now()}-${Math.random()}`,
    role: "system",
    systemKind: warning ? "browser_marker_warning" : "browser_marker",
    content,
    createdAt: new Date(),
    status: "complete",
  };
}

case "browser.provisioned":
  return {
    ...state,
    browser: { status: "live", controlOwner: null },
    messages: [...state.messages, browserMarker("browser ready")],
  };
case "browser.control_granted":
  return {
    ...state,
    browser: {
      ...(state.browser ?? { status: "live", controlOwner: null }),
      status: "user-control",
      controlOwner: (event.data.owner_user_id as string | undefined) ?? null,
    },
    messages: [...state.messages, browserMarker("user took control", true)],
  };
case "browser.control_returned":
  return {
    ...state,
    browser: {
      ...(state.browser ?? { status: "live", controlOwner: null }),
      status: "live",
      controlOwner: null,
    },
    messages: [...state.messages, browserMarker("control returned to agent")],
  };
case "browser.destroyed":
  return {
    ...state,
    browser: null,
    messages: [...state.messages, browserMarker("browser closed")],
  };
```

Add the `systemKind` values to `AgentChatSystemKind` in `types.ts`:

```typescript
export type AgentChatSystemKind =
  | "skill_invoked"
  | "artifact"
  | "error"
  | "browser_marker"
  | "browser_marker_warning";
```

`ChatThread` already renders system messages inline; `browser_marker`
gets the standard subtle one-line treatment, and
`browser_marker_warning` gets the warning style.

- [ ] **Step 6: Run** — all 6 tests PASS (4 from earlier + 2 new).

- [ ] **Step 7: Commit**

```bash
cd /work/surogates
git add sdk/agent-chat-react/src/types.ts \
        sdk/agent-chat-react/src/runtime/events.ts \
        sdk/agent-chat-react/src/runtime/reducer.ts \
        sdk/agent-chat-react/tests/reducer.test.ts
git commit -m "feat(sdk): fold browser.* events into reducer + inline markers"
```

---

## Task 12: SDK — extend `AgentChatAdapter` with browser methods

**Files:**
- Modify: `sdk/agent-chat-react/src/types.ts`
- Modify: `sdk/agent-chat-react/src/adapter-context.tsx`
- Modify: `/work/surogate-ops/surogate_ops/server/routes/sessions.py`
- Modify: surogate-ops adapter — `/work/surogate-ops/frontend/src/features/work/work-agent-chat-adapter.ts`

The SDK doesn't speak HTTP directly — it goes through the adapter the host
app provides. Phase C adds three browser methods to the adapter contract:
`getBrowserState`, `acquireBrowserControl`, `releaseBrowserControl`. The
SDK also exposes a `liveViewUrl(sessionId, token)` helper for building
the iframe `src`. In surogate-ops these methods go through the existing
ops backend proxy (`/api/sessions/...`), not directly to the live agent
API's `/v1/...` routes.

- [ ] **Step 1: Extend `AgentChatAdapter` in `src/types.ts`:**

```typescript
export interface AgentChatBrowserStateResponse {
  status: "live" | "user-control";
  controlOwner: string | null;
  liveViewPath: string;
}

export interface AgentChatAdapter {
  // ... existing methods ...
  getBrowserState(sessionId: string): Promise<AgentChatBrowserStateResponse | null>;
  acquireBrowserControl(sessionId: string): Promise<{ outcome: "granted" | "refreshed" | "conflict"; ownerUserId: string }>;
  releaseBrowserControl(sessionId: string): Promise<void>;
  // Returns the absolute or relative URL for the live-view iframe.
  // Implementations attach a session-scoped token via the ?token= query.
  browserLiveViewUrl(sessionId: string): string;
}
```

- [ ] **Step 2: Add surogate-ops backend proxy routes**

In `/work/surogate-ops/surogate_ops/server/routes/sessions.py`, add
browser state/control routes beside the existing workspace/artifact
live-session proxy routes:

```python
@router.get("/{session_id}/browser/state")
async def get_live_browser_state(
    session_id: str,
    request: Request,
    scope: SessionScope = Depends(resolve_session_scope),
    ops_session: AsyncSession = Depends(get_session),
):
    client = await _build_live_agent_client(
        scope.agent_id,
        request,
        ops_session,
        **_scope_service_account_kwargs(scope),
    )
    return await _forward_json(
        client,
        "GET",
        f"{_LIVE_API_PREFIX}/{session_id}/browser/state",
    )

@router.post("/{session_id}/browser/control")
async def post_live_browser_control(
    session_id: str,
    body: dict[str, Any],
    request: Request,
    scope: SessionScope = Depends(resolve_session_scope),
    ops_session: AsyncSession = Depends(get_session),
    subject: str = Depends(get_current_subject),
):
    client = await _build_live_agent_client(
        scope.agent_id,
        request,
        ops_session,
        **_scope_service_account_kwargs(scope),
    )
    return await _forward_json(
        client,
        "POST",
        f"{_LIVE_API_PREFIX}/{session_id}/browser/control",
        json_body={**body, "owner_user_id": subject},
    )
```

Also add HTTP and WebSocket proxy routes for
`/{session_id}/browser/live/{path:path}` that forward to
`f"{_LIVE_API_PREFIX}/{session_id}/browser/live/{path}"` using the
service-account client. Strip the ops-side `token` query parameter before
forwarding; the upstream live API is authenticated by the service-account
client.

- [ ] **Step 3: Implement in the surogate-ops adapter**

`work-agent-chat-adapter.ts`:

```typescript
import { authFetch } from "@/api/auth";
import { getAuthToken } from "@/features/auth";

async getBrowserState(sessionId: string) {
  const r = await authFetch(scopedSessionUrl(sessionId, "/browser/state", agentId));
  if (r.status === 404) return null;
  if (!r.ok) throw new Error("Failed to fetch browser state");
  const data = await r.json();
  return {
    status: data.status,
    controlOwner: data.control_owner,
    liveViewPath: data.live_view_path,
  };
},

async acquireBrowserControl(sessionId: string) {
  const r = await request<{ outcome: string; owner_user_id: string }>(
    scopedSessionUrl(sessionId, "/browser/control", agentId),
    "Failed to acquire browser control",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "acquire" }),
    },
  );
  return {
    outcome: r.outcome as "granted" | "refreshed" | "conflict",
    ownerUserId: r.owner_user_id,
  };
},

async releaseBrowserControl(sessionId: string) {
  await request<unknown>(
    scopedSessionUrl(sessionId, "/browser/control", agentId),
    "Failed to release browser control",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "release" }),
    },
  );
},

browserLiveViewUrl(sessionId: string) {
  const token = getAuthToken();
  return scopedSessionUrl(sessionId, "/browser/live/vnc.html", agentId, {
    token: token ?? "",
  });
},
```

- [ ] **Step 4: Add a default no-op for SDK consumers without a browser**

Provide a default implementation in the SDK fallback adapter (the one used
by the SDK's own tests + storybook). The simplest pattern is:

```typescript
const NO_BROWSER: Pick<AgentChatAdapter, "getBrowserState" | "acquireBrowserControl" | "releaseBrowserControl" | "browserLiveViewUrl"> = {
  async getBrowserState() { return null; },
  async acquireBrowserControl() { throw new Error("Not supported"); },
  async releaseBrowserControl() { /* noop */ },
  browserLiveViewUrl() { return ""; },
};
```

- [ ] **Step 5: Verify SDK tests still pass**

```bash
cd /work/surogates/sdk/agent-chat-react
pnpm test
```

Expected: green; no test depends on the new methods yet.

- [ ] **Step 6: Commit**

```bash
cd /work/surogates
git add sdk/agent-chat-react/src/types.ts \
        sdk/agent-chat-react/src/adapter-context.tsx
git commit -m "feat(sdk): add browser state/control methods to AgentChatAdapter"

cd /work/surogate-ops
git add surogate_ops/server/routes/sessions.py \
        frontend/src/features/work/work-agent-chat-adapter.ts
git commit -m "feat(work): proxy browser state/control/live view"
```

---

## Task 13: SDK — `BrowserPane` component (header + live-view iframe + control bar)

**Files:**
- Create: `sdk/agent-chat-react/src/components/browser/browser-pane.tsx`
- Create: `sdk/agent-chat-react/src/components/browser/browser-live-view.tsx`
- Create: `sdk/agent-chat-react/src/components/browser/browser-control-bar.tsx`
- Create: `sdk/agent-chat-react/src/components/browser/browser-status-dot.tsx`
- Test: `sdk/agent-chat-react/tests/browser-pane.test.tsx`

> Visual brief: pane has a header row with a lucide `Zap` icon, `Browser`,
> a status dot, and the current URL when available,
> the iframe filling the body, and a controls bar at the bottom
> (`Take control`, recording indicator, overflow menu). Match the existing minimal/terminal
> aesthetic. Refer to spec §8.2 for the state-to-visual mapping.

- [ ] **Step 1: Write the failing test** (component happy paths)

`tests/browser-pane.test.tsx`:

```typescript
import { describe, expect, it } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { BrowserPane } from "../src/components/browser/browser-pane";

const liveAdapter = {
  getBrowserState: async () => ({
    status: "live" as const,
    controlOwner: null,
    liveViewPath: "/v1/sessions/s/browser/live/",
  }),
  acquireBrowserControl: async () => ({ outcome: "granted" as const, ownerUserId: "u" }),
  releaseBrowserControl: async () => {},
  browserLiveViewUrl: () => "/v1/sessions/s/browser/live/vnc.html?token=x",
};

describe("BrowserPane", () => {
  it("renders an iframe in live state", () => {
    render(<BrowserPane sessionId="s" state={{ status: "live", controlOwner: null }} adapter={liveAdapter} />);
    expect(screen.getByTestId("browser-iframe")).toHaveAttribute(
      "src",
      "/v1/sessions/s/browser/live/vnc.html?token=x",
    );
  });

  it("shows Take control button in live state", () => {
    render(<BrowserPane sessionId="s" state={{ status: "live", controlOwner: null }} adapter={liveAdapter} />);
    expect(screen.getByRole("button", { name: /take control/i })).toBeInTheDocument();
  });

  it("shows Return control button when user has control", () => {
    render(
      <BrowserPane
        sessionId="s"
        state={{ status: "user-control", controlOwner: "user-A" }}
        adapter={liveAdapter}
      />,
    );
    expect(screen.getByRole("button", { name: /return control/i })).toBeInTheDocument();
  });

  it("shows skeleton in provisioning state", () => {
    render(
      <BrowserPane
        sessionId="s"
        state={{ status: "provisioning", controlOwner: null }}
        adapter={liveAdapter}
      />,
    );
    expect(screen.getByText(/starting browser/i)).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run** — 4 FAIL.

- [ ] **Step 3: Implement the components**

`src/components/browser/browser-status-dot.tsx`:

```typescript
import { cn } from "../../lib/utils";

export function BrowserStatusDot({ status }: { status: string }) {
  return (
    <span
      className={cn(
        "h-2 w-2 rounded-full inline-block",
        status === "live" && "bg-green-500",
        status === "user-control" && "bg-amber-500",
        status === "provisioning" && "bg-blue-500 animate-pulse",
        status === "closed" && "bg-zinc-500",
      )}
    />
  );
}
```

`src/components/browser/browser-live-view.tsx`:

```typescript
export function BrowserLiveView({ src }: { src: string }) {
  return (
    <iframe
      data-testid="browser-iframe"
      src={src}
      className="w-full h-full border-0 bg-black"
      allow="clipboard-read; clipboard-write"
    />
  );
}
```

`src/components/browser/browser-control-bar.tsx`:

```typescript
import { Button } from "../ui/button";
import type { AgentChatAdapter } from "../../types";

interface Props {
  sessionId: string;
  hasControl: boolean;
  adapter: AgentChatAdapter;
}

export function BrowserControlBar({ sessionId, hasControl, adapter }: Props) {
  return (
    <div className="flex items-center gap-2 border-t border-line bg-card px-3 py-2">
      {hasControl ? (
        <Button size="sm" variant="secondary"
          onClick={() => void adapter.releaseBrowserControl(sessionId)}>
          Return control
        </Button>
      ) : (
        <Button size="sm"
          onClick={() => void adapter.acquireBrowserControl(sessionId)}>
          Take control
        </Button>
      )}
    </div>
  );
}
```

`src/components/browser/browser-pane.tsx`:

```typescript
import { useMemo } from "react";
import { Zap } from "lucide-react";
import { BrowserControlBar } from "./browser-control-bar";
import { BrowserLiveView } from "./browser-live-view";
import { BrowserStatusDot } from "./browser-status-dot";
import type { AgentChatAdapter, AgentChatBrowserState } from "../../types";

interface Props {
  sessionId: string;
  state: AgentChatBrowserState;
  adapter: AgentChatAdapter;
}

export function BrowserPane({ sessionId, state, adapter }: Props) {
  const liveViewUrl = useMemo(
    () => adapter.browserLiveViewUrl(sessionId),
    [adapter, sessionId],
  );

  return (
    <div className="flex h-full min-h-0 flex-col bg-background">
      <header className="flex items-center gap-2 border-b border-line bg-card px-3 py-2 text-xs">
        <Zap className="h-3 w-3" aria-hidden="true" />
        <span>Browser</span>
        <BrowserStatusDot status={state.status} />
        {state.controlOwner && (
          <span className="text-amber-500">
            {state.controlOwner} has control
          </span>
        )}
      </header>
      <div className="flex-1 min-h-0">
        {state.status === "provisioning" ? (
          <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
            Starting browser…
          </div>
        ) : (
          <BrowserLiveView src={liveViewUrl} />
        )}
      </div>
      <BrowserControlBar
        sessionId={sessionId}
        hasControl={state.status === "user-control"}
        adapter={adapter}
      />
    </div>
  );
}
```

- [ ] **Step 4: Run** — all PASS.

- [ ] **Step 5: Commit**

```bash
git add sdk/agent-chat-react/src/components/browser/ \
        sdk/agent-chat-react/tests/browser-pane.test.tsx
git commit -m "feat(sdk): add BrowserPane component with live view + control bar"
```

---

## Task 14: SDK — stack `BrowserPane` above `WorkspacePanel` in the right column

**Files:**
- Modify: `sdk/agent-chat-react/src/agent-chat.tsx`
- Test: `sdk/agent-chat-react/tests/agent-chat.test.tsx` (extend)

The right column becomes a vertical stack: `BrowserPane` (when active)
on top, `WorkspacePanel` below, separated by a draggable divider. When
no browser is provisioned the right column collapses to just the
workspace — current behaviour, no visual regression.

- [ ] **Step 1: Write the failing test** — append to `tests/agent-chat.test.tsx`:

```typescript
it("renders only WorkspacePanel when no browser is provisioned", async () => {
  const adapter = makeMockAdapter({ browserState: null });
  render(<AgentChat adapter={adapter} sessionId="s" />);
  await waitFor(() => {
    expect(screen.getByTestId("workspace-panel")).toBeInTheDocument();
  });
  expect(screen.queryByTestId("browser-pane")).toBeNull();
});

it("stacks BrowserPane above WorkspacePanel when browser is live", async () => {
  const adapter = makeMockAdapter();
  adapter.openEventStream = () => mockEventStream([
    {
      type: "browser.provisioned",
      eventId: 1,
      data: { session_id: "s", browser_id: "b" },
    },
  ]);
  render(<AgentChat adapter={adapter} sessionId="s" />);
  await waitFor(() => {
    expect(screen.getByTestId("browser-pane")).toBeInTheDocument();
    expect(screen.getByTestId("workspace-panel")).toBeInTheDocument();
  });
  // BrowserPane should be visually above the workspace.
  const pane = screen.getByTestId("browser-pane");
  const ws = screen.getByTestId("workspace-panel");
  expect(pane.compareDocumentPosition(ws) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
});
```

- [ ] **Step 2: Run** — 2 FAIL.

- [ ] **Step 3: Modify `agent-chat.tsx`**

Replace the right-column `<WorkspacePanel ... />` with a stack:

```typescript
import { BrowserPane } from "./components/browser/browser-pane";

// Inside the component render:
const browserState = runtime.state.browser;

return (
  <AgentChatAdapterProvider
    value={{
      adapter,
      sessionId,
      onFileSelect: handleFileSelect,
    }}
  >
    <TooltipProvider>
      <section className="flex flex-1 min-h-0 overflow-hidden bg-background text-sm text-foreground">
        <div className="flex min-w-0 flex-1 flex-col overflow-hidden">
          <ChatThread
            sessionId={sessionId}
            messages={runtime.messages}
            isRunning={runtime.isRunning}
            isLoadingHistory={runtime.isLoadingHistory}
            onSend={(content, images) => void runtime.send(content, images)}
            onStop={() => void runtime.stop()}
            onRetry={runtime.retry}
            onFileSelect={handleFileSelect}
            disabled={effectiveDisabled}
            disabledReason={disabledReason}
            tokenUsage={runtime.tokenUsage}
            retryIndicator={runtime.retryIndicator}
          />
        </div>
        <div data-testid="right-stack" className="flex flex-col">
          {browserState !== null && (
            <div data-testid="browser-pane" className="flex-1 min-h-[40%] border-b border-line">
              <BrowserPane
                sessionId={sessionId ?? ""}
                state={browserState}
                adapter={adapter}
              />
            </div>
          )}
          <div data-testid="workspace-panel" className="flex-1 min-h-0">
            <WorkspacePanel
              adapter={adapter}
              sessionId={sessionId}
              selectedPath={workspacePath}
              onSelectedPathChange={setWorkspacePath}
              collapsed={workspaceCollapsed}
              onCollapsedChange={setWorkspaceCollapsed}
              refreshSignal={runtime.workspaceRefreshKey}
              disabled={effectiveDisabled}
            />
          </div>
        </div>
      </section>
    </TooltipProvider>
  </AgentChatAdapterProvider>
);
```

> Note: `runtime.state` will need a small type update to expose
> `browser: AgentChatBrowserState | null`. Already added in Task 11.

- [ ] **Step 4: Run** — both tests PASS.

- [ ] **Step 5: Commit**

```bash
git add sdk/agent-chat-react/src/agent-chat.tsx \
        sdk/agent-chat-react/tests/agent-chat.test.tsx
git commit -m "feat(sdk): stack BrowserPane above WorkspacePanel in right column"
```

---

## Task 15: SDK — `BrowserActivityGroup` for collapsed `browser_*` tool calls in the chat thread

**Files:**
- Create: `sdk/agent-chat-react/src/components/browser/browser-activity-group.tsx`
- Modify: `sdk/agent-chat-react/src/components/chat/chat-thread.tsx`
- Test: `sdk/agent-chat-react/tests/browser-activity-group.test.tsx`

When N consecutive `browser_*` tool calls arrive without an interleaving
LLM message or non-browser tool call, render a single collapsed group:
`⚡ browser (N actions — latest: <verb> <target>) ▾`. Click to expand
the per-action list with the same compact format. See spec §8.3.

- [ ] **Step 1: Write the failing test**

`tests/browser-activity-group.test.tsx`:

```typescript
import { describe, expect, it } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { BrowserActivityGroup } from "../src/components/browser/browser-activity-group";

const calls = [
  { id: "1", toolName: "browser_navigate", args: "{\"url\":\"https://app.com\"}", result: "{\"url\":\"https://app.com\"}", status: "complete" },
  { id: "2", toolName: "browser_click", args: "{\"ref\":\"@e3\"}", result: "{\"clicked\":true}", status: "complete" },
  { id: "3", toolName: "browser_type", args: "{\"ref\":\"@e4\",\"text\":\"x\"}", result: "{\"typed\":true}", status: "complete" },
];

describe("BrowserActivityGroup", () => {
  it("renders collapsed by default", () => {
    render(<BrowserActivityGroup calls={calls} />);
    expect(screen.getByText(/3 actions/)).toBeInTheDocument();
    expect(screen.queryByText(/navigate/)).toBeNull();
  });

  it("expands to show per-action list", () => {
    render(<BrowserActivityGroup calls={calls} />);
    fireEvent.click(screen.getByRole("button", { name: /3 actions/ }));
    expect(screen.getByText(/navigate/)).toBeInTheDocument();
    expect(screen.getByText(/click/)).toBeInTheDocument();
  });

  it("shows the latest action in the collapsed header", () => {
    render(<BrowserActivityGroup calls={calls} />);
    expect(screen.getByText(/latest: type/)).toBeInTheDocument();
  });

  it("flags errors with a marker", () => {
    const withError = [
      ...calls,
      { id: "4", toolName: "browser_click", args: "{}", result: "{\"error\":\"paused_by_user\"}", status: "error" },
    ];
    render(<BrowserActivityGroup calls={withError} />);
    fireEvent.click(screen.getByRole("button", { name: /4 actions/ }));
    expect(screen.getByTestId("activity-error-4")).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run** — 4 FAIL.

- [ ] **Step 3: Implement** the component:

```typescript
import { useState } from "react";
import { ChevronDown, ChevronRight, AlertCircle, Zap } from "lucide-react";
import type { AgentChatToolCallInfo } from "../../types";

interface Props { calls: AgentChatToolCallInfo[] }

function summarise(call: AgentChatToolCallInfo): string {
  const verb = call.toolName.replace(/^browser_/, "");
  const args = parseJson(call.args);
  if ("ref" in args) return `${verb} ${args.ref}`;
  if ("url" in args) return `${verb} ${args.url}`;
  if ("text" in args) return `${verb} "${String(args.text).slice(0, 24)}"`;
  return verb;
}

function parseJson(raw?: string): Record<string, unknown> {
  if (!raw) return {};
  try {
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch {
    return {};
  }
}

export function BrowserActivityGroup({ calls }: Props) {
  const [open, setOpen] = useState(false);
  const latest = calls[calls.length - 1];

  return (
    <div className="rounded border border-line bg-card text-xs">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-2 px-3 py-2"
      >
        {open ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
        <Zap className="h-3 w-3" aria-hidden="true" />
        <span>browser</span>
        <span className="text-muted-foreground">
          ({calls.length} actions — latest: {summarise(latest)})
        </span>
      </button>
      {open && (
        <ul className="border-t border-line">
          {calls.map((c) => {
            const result = parseJson(c.result);
            const err = result?.error;
            return (
              <li
                key={c.id}
                className="flex items-center gap-2 px-6 py-1 font-mono"
              >
                {err && (
                  <AlertCircle
                    data-testid={`activity-error-${c.id}`}
                    className="h-3 w-3 text-red-500"
                  />
                )}
                <span>{summarise(c)}</span>
                {err && (
                  <span className="ml-2 text-red-500">{err}</span>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
```

- [ ] **Step 4: Wire grouping into `ChatThread`**

In `src/components/chat/chat-thread.tsx`, when rendering the message list:

```typescript
import { BrowserActivityGroup } from "../browser/browser-activity-group";

function isBrowserCall(call: AgentChatToolCallInfo): boolean {
  return call.toolName.startsWith("browser_");
}

function groupBrowserCalls(messages: AgentChatMessage[]): RenderUnit[] {
  const out: RenderUnit[] = [];
  let buffer: AgentChatToolCallInfo[] = [];

  for (const m of messages) {
    if (m.role === "assistant" && m.toolCalls?.length) {
      const allBrowser = m.toolCalls.every(isBrowserCall);
      if (allBrowser) {
        buffer.push(...m.toolCalls);
        continue;
      }
    }
    if (buffer.length > 0) {
      out.push({ kind: "browser_group", calls: buffer });
      buffer = [];
    }
    out.push({ kind: "message", message: m });
  }
  if (buffer.length > 0) out.push({ kind: "browser_group", calls: buffer });
  return out;
}
```

Replace the existing message-mapping render with the grouped variant; for
`{ kind: "browser_group", calls }` render `<BrowserActivityGroup calls={calls} />`.

- [ ] **Step 5: Run** — all PASS.

- [ ] **Step 6: Commit**

```bash
git add sdk/agent-chat-react/src/components/browser/browser-activity-group.tsx \
        sdk/agent-chat-react/src/components/chat/chat-thread.tsx \
        sdk/agent-chat-react/tests/browser-activity-group.test.tsx
git commit -m "feat(sdk): collapse consecutive browser_* tool calls into one group"
```

---

## Task 16: Helm — API server egress to browser pods (both charts)

> **Both charts.** Apply to `helm/surogates/templates/` AND
> `surogate_ops/agent_chart/templates/` with two separate commits.

The Phase B `browser-networkpolicy.yaml` already allows ingress from the
api-server to browser pods (port 6080 + 10001). The reverse — api-server
egress to browser pods — needs to be permitted by the api-server's own
NetworkPolicy if one exists; if no api-server NetworkPolicy is in place
yet, this task is a no-op.

- [ ] **Step 1: Check whether the api-server has a NetworkPolicy**

```bash
ls /work/surogates/helm/surogates/templates/ | grep -i 'api.*network'
```

If no such file, skip to Step 4 (no-op task).

- [ ] **Step 2: If a policy exists, append a browser-pod egress rule**

Add to the api-server NetworkPolicy's `egress:` block:

```yaml
- to:
    - podSelector:
        matchLabels:
          app: surogates-browser
  ports:
    - protocol: TCP
      port: 443
    - protocol: TCP
      port: 10001
```

Use the literal `app: surogates-browser` selector because Phase B browser
pods are labeled that way. The Service exposes NoVNC on port `443`
targeting pod port `6080`, so egress should allow `443` and `10001`.

- [ ] **Step 3: Render and verify**

```bash
helm template /work/surogates/helm/surogates --show-only templates/api-networkpolicy.yaml
```

- [ ] **Step 4: Replicate to surogate-ops**

If a change was made in Step 2, copy the file to the surogate-ops chart.

- [ ] **Step 5: Commit (both repos, or skip if no-op)**

```bash
git -C /work/surogates add helm/surogates/templates/api-networkpolicy.yaml
git -C /work/surogates commit -m "chore(helm): allow api-server egress to browser pods"

git -C /work/surogate-ops add surogate_ops/agent_chart/templates/api-networkpolicy.yaml
git -C /work/surogate-ops commit -m "chore(helm): allow api-server egress to browser pods"
```

---

## Task 17: Bump SDK version + republish; bump frontend pin

**Files:**
- Modify: `sdk/agent-chat-react/package.json` (version bump)
- Modify: `surogate-ops/frontend/package.json` (pin bump)

The SDK additions in Tasks 11–15 are additive but extend the
`AgentChatAdapter` interface — host apps must implement the new methods.
Bump the minor version (e.g., `1.5.10` → `1.6.0`) and update the
surogate-ops frontend pin.

- [ ] **Step 1: Bump SDK version**

```bash
cd /work/surogates/sdk/agent-chat-react
pnpm version minor
```

- [ ] **Step 2: Build and publish (or build + workspace-link in dev)**

```bash
pnpm build
# In CI / release flow: pnpm publish (with the right registry config).
# In local dev: nothing to do — pnpm workspace links the package.
```

- [ ] **Step 3: Bump the frontend pin**

In `/work/surogate-ops/frontend/package.json`:

```diff
-    "@invergent/agent-chat-react": "^1.5.10",
+    "@invergent/agent-chat-react": "^1.6.0",
```

```bash
cd /work/surogate-ops/frontend
pnpm install
pnpm test
pnpm build
```

- [ ] **Step 4: Commit (both repos)**

```bash
git -C /work/surogates add sdk/agent-chat-react/package.json
git -C /work/surogates commit -m "chore(sdk): bump @invergent/agent-chat-react to 1.6.0"

git -C /work/surogate-ops add frontend/package.json frontend/pnpm-lock.yaml
git -C /work/surogate-ops commit -m "chore(frontend): bump @invergent/agent-chat-react to ^1.6.0"
```

---

## Task 18: Opt-in end-to-end test against a real cluster

**Files:**
- Create: `tests/integration/test_browser_e2e_phase_c.py`
- Modify: `pyproject.toml` (no new marker; reuse `browser_e2e_k8s` from Phase B)

- [ ] **Step 1: Write the test**

```python
"""Phase C end-to-end smoke against a real cluster.

Setup:
  kind create cluster --name surogates-test
  helm install surogates ./helm/surogates --set browser.backend=kubernetes
  ./images/build.sh latest browser
  kind load docker-image ghcr.io/invergent-ai/surogates-agent-browser:latest \
      --name surogates-test
  Run the API server + worker pods.

Then:
  pytest -m browser_e2e_k8s tests/integration/test_browser_e2e_phase_c.py -v
"""

from __future__ import annotations

import asyncio
import os
import pytest
import websockets
from httpx import AsyncClient


pytestmark = pytest.mark.browser_e2e_k8s

API_BASE = os.environ.get("BROWSER_E2E_API_BASE", "http://localhost:8000")
TOKEN = os.environ.get("BROWSER_E2E_TOKEN", "")


@pytest.fixture()
async def session_with_browser():
    if not TOKEN:
        pytest.skip("BROWSER_E2E_TOKEN is required")
    async with AsyncClient(base_url=API_BASE, timeout=30.0) as c:
        created = await c.post(
            "/v1/sessions",
            json={"model": os.environ.get("BROWSER_E2E_MODEL", "gpt-4.1-mini")},
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        assert created.status_code in {200, 201}, created.text
        session_id = created.json()["id"]
        sent = await c.post(
            f"/v1/sessions/{session_id}/messages",
            json={"content": "Open https://example.com in the browser, then stop."},
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        assert sent.status_code in {200, 202}, sent.text

        deadline = asyncio.get_event_loop().time() + 120
        last_event = 0
        while asyncio.get_event_loop().time() < deadline:
            events = await c.get(
                f"/v1/sessions/{session_id}/events/poll",
                params={"after": last_event},
                headers={"Authorization": f"Bearer {TOKEN}"},
            )
            assert events.status_code == 200, events.text
            for event in events.json().get("events", []):
                last_event = max(last_event, int(event["id"]))
                if event["type"] == "browser.provisioned":
                    return session_id
            await asyncio.sleep(1.0)
    raise AssertionError("browser.provisioned was not observed within 120s")


async def test_state_endpoint_returns_live_after_provision(session_with_browser) -> None:
    async with AsyncClient(base_url=API_BASE) as c:
        r = await c.get(
            f"/v1/sessions/{session_with_browser}/browser/state",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "live"


async def test_acquire_then_release_round_trip(session_with_browser) -> None:
    async with AsyncClient(base_url=API_BASE) as c:
        r1 = await c.post(
            f"/v1/sessions/{session_with_browser}/browser/control",
            json={"action": "acquire"},
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        assert r1.status_code == 200
        assert r1.json()["outcome"] == "granted"

        r2 = await c.get(
            f"/v1/sessions/{session_with_browser}/browser/state",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        assert r2.json()["status"] == "user-control"

        r3 = await c.post(
            f"/v1/sessions/{session_with_browser}/browser/control",
            json={"action": "release"},
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        assert r3.status_code == 200


async def test_live_view_html_is_served(session_with_browser) -> None:
    async with AsyncClient(base_url=API_BASE) as c:
        r = await c.get(
            f"/v1/sessions/{session_with_browser}/browser/live/vnc.html",
            params={"token": TOKEN},
        )
        assert r.status_code == 200
        assert b"<html" in r.content.lower() or b"<!doctype" in r.content.lower()


async def test_websocket_connects_and_pumps_one_frame(session_with_browser) -> None:
    ws_url = (
        API_BASE.replace("http", "ws", 1)
        + f"/v1/sessions/{session_with_browser}/browser/live/websockify"
        + f"?token={TOKEN}"
    )
    async with websockets.connect(ws_url, subprotocols=["binary"]) as ws:
        # NoVNC server hello message arrives within ~1s.
        first = await asyncio.wait_for(ws.recv(), timeout=5.0)
        assert isinstance(first, (bytes, bytearray))
        assert len(first) > 0
```

- [ ] **Step 2: Run the unit suite without the marker** (default skip)

```bash
pytest tests/ -q
```

Expected: green; phase-C e2e is skipped.

- [ ] **Step 3: Run the e2e (when a cluster is up)**

```bash
pytest -m browser_e2e_k8s tests/integration/test_browser_e2e_phase_c.py -v -s
```

Expected: 4 tests PASS.

- [ ] **Step 4: Commit**

```bash
git -C /work/surogates add tests/integration/test_browser_e2e_phase_c.py
git -C /work/surogates commit -m "test(browser): add Phase C e2e (state + control + live view + WS)"
```

---

## Final verification

After all 18 tasks:

```bash
# Backend full suite
pytest tests/ -q
```

Expected: green.

```bash
# SDK suite
cd /work/surogates/sdk/agent-chat-react
pnpm test
```

Expected: green.

```bash
# Frontend smoke
cd /work/surogate-ops/frontend
pnpm test
pnpm build
```

Expected: green.

Manual smoke (with a browser session running):

1. Open the chat UI; start an agent that uses a browser tool.
2. The right pane shows BrowserPane stacked above the workspace once
   `browser.provisioned` arrives.
3. Click "Take control" → button flips to "Return control"; status dot
   goes amber; the iframe accepts mouse/keyboard input.
4. Click "Return control" → button flips back; the agent resumes within
   ~1s; subsequent `browser_*` calls succeed.
5. Multiple `browser_*` calls in the thread collapse into one group with
   the latest action visible; clicking expands the list.

---

## What Phase C delivers

- **`BrowserResolver`** — registry-primary + K8s-fallback resolution,
  tenant-scoped. Single dependency the API server route handlers consume.
- **REST endpoints** — `GET /browser/state` and `POST /browser/control`
  with the spec's three-branch acquire semantics (granted / refreshed /
  conflict).
- **Live-view proxy** — HTTP catch-all for NoVNC static assets and a
  WebSocket pump for the framebuffer, both authenticated; the WS pump
  applies RFB ClientMessage gating (drop types 4/5/6 unless the
  connecting user holds control).
- **`?token=<jwt>` for live-view paths** — auth middleware special-cases
  the live-view prefix so iframes can carry session JWTs.
- **Wake-on-release** — releasing control fires
  `BROWSER_CONTROL_RETURNED` and enqueues a session wake so the harness
  resumes within seconds.
- **One-time pause notice** — the harness prepends a system message on
  the first iteration after the user takes control, then suppresses
  further injections until release; flag clears on release.
- **SDK browser pane** — `BrowserPane` (header + live view + control
  bar), stacked above `WorkspacePanel` in the right column. Reduces to
  the workspace-only layout when no browser is provisioned.
- **Activity grouping** — consecutive `browser_*` tool calls in the chat
  thread collapse into a single expandable group with the latest action
  surfaced in the header.
- **`browser.*` event types in the SDK** — folded into the chat reducer
  so the pane state stays in sync with the worker's view.
- **Helm:** API server egress to browser pods (when an api-server
  NetworkPolicy exists). NetworkPolicy ingress to browser pods from
  api-server was already added in Phase B.
- **SDK + frontend version bump** — `@invergent/agent-chat-react`
  minor bump and surogate-ops pin update.
- **Opt-in e2e** — state, control round-trip, live-view HTML, WebSocket
  framing all verified end-to-end against a real cluster.

Phase C explicitly does **not** ship: profile sync, opt-in recording.
Those are Phase D, against the same `KernelBrowserClient` and
`BrowserPool` interfaces, with no rework needed in this layer.
