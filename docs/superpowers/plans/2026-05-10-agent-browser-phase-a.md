# Agent Browser — Phase A: Backend Skeleton — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land the worker-side foundation for the agent browser — a Python `KernelBrowserClient` that drives kernel-images via REST, a `BrowserPool` + Redis `BrowserRegistry`/`BrowserControlStore` for cross-process state, a `ProcessBrowserBackend` that runs kernel-images via `docker run` for local development, and the discrete `browser_*` tools wired into the harness — always enabled (no feature flag; the `backend` choice is the only environment switch). End state: an agent running locally can navigate, get_state, click, type, screenshot a real Chromium running in a Docker container.

**Architecture:** New `surogates/browser/` package mirrors `surogates/sandbox/`. Tools dispatch as `ToolLocation.HARNESS` and call `BrowserPool.ensure()` then `KernelBrowserClient` over httpx against the pod's REST port. Cross-tool refs live in a per-session cache owned by `BrowserPool` so `browser_get_state` and later `browser_click {"ref": ...}` can run through separate client instances safely. Cross-process metadata (worker writes, API server reads in Phase C) lives in Redis hashes/keys. K8s backend, live-view UI, profile sync, and recording are deferred to Phases B/C/D.

**Tech Stack:** Python 3.12+, httpx (async HTTP), redis-py asyncio, pytest + pytest-asyncio, pydantic-settings, kernel-images Docker image (`ghcr.io/onkernel/chromium-headful:stable`).

**Spec:** [`docs/superpowers/specs/2026-05-10-agent-browser-design.md`](../specs/2026-05-10-agent-browser-design.md)

---

## Phase A TODO

- [x] Task 1: Add browser event types and config settings — completed
- [ ] Task 2: Define `BrowserBackend` protocol and value types — **in progress**
- [ ] Task 3: `KernelBrowserClient` skeleton — left to do
- [ ] Task 4: `KernelBrowserClient.navigate` — left to do
- [ ] Task 5: `KernelBrowserClient.get_state` with refs/cache — left to do
- [ ] Task 6: `get_state` filters — left to do
- [ ] Task 7: click/type client methods — left to do
- [ ] Task 8: key/scroll/drag/wait client methods — left to do
- [ ] Task 9: screenshot client method — left to do
- [ ] Task 10: `BrowserRegistry` — left to do
- [ ] Task 11: `BrowserControlStore` — left to do
- [ ] Task 12: `ProcessBrowserBackend` — left to do
- [ ] Task 13: `BrowserPool` — left to do
- [ ] Task 14: navigate/get_state/close tools — left to do
- [ ] Task 15: click/type/press_key/scroll/drag/wait tools — left to do
- [ ] Task 16: screenshot tool — left to do
- [ ] Task 17: router/runtime wiring — left to do
- [ ] Task 18: dispatch kwargs threading — left to do
- [ ] Task 19: worker bootstrap — left to do
- [ ] Task 20: `AgentHarness` signature update — left to do
- [ ] Task 21: opt-in e2e smoke — left to do

---

## File Structure

```
surogates/browser/
├── __init__.py              (NEW — empty package marker)
├── base.py                  (NEW — BrowserBackend protocol, BrowserSpec, BrowserStatus, errors)
├── client.py                (NEW — KernelBrowserClient: httpx wrapper around kernel-images REST)
├── registry.py              (NEW — BrowserRegistry: Redis hash for cross-process pod metadata)
├── control.py               (NEW — BrowserControlStore: Redis-backed user-control flag)
├── process.py               (NEW — ProcessBrowserBackend: docker run for local dev)
└── pool.py                  (NEW — BrowserPool: session_id → pod, lifecycle, registry writes)

surogates/tools/builtin/
└── browser.py               (REPLACE — was a stub; now hosts all browser_* discrete tools)

surogates/config.py          (MODIFY — add BrowserSettings)
surogates/session/events.py  (MODIFY — add BROWSER_PROVISIONED, BROWSER_DESTROYED)
surogates/governance/policy.py (MODIFY — extend URL arg map; no behavior change for existing tools)
surogates/tools/router.py    (MODIFY — add browser tools to TOOL_LOCATIONS as HARNESS)
surogates/tools/runtime.py   (MODIFY — register the browser module)
surogates/orchestrator/worker.py (MODIFY — always instantiate BrowserPool + Registry + Control)
surogates/harness/tool_exec.py (MODIFY — thread browser_pool / browser_control into execute_single_tool)

tests/test_browser_base.py        (NEW)
tests/test_browser_client.py      (NEW — bulk of the unit tests)
tests/test_browser_registry.py    (NEW)
tests/test_browser_control.py     (NEW)
tests/test_browser_process.py     (NEW)
tests/test_browser_pool.py        (NEW)
tests/test_browser_tools.py       (NEW)
tests/integration/test_browser_e2e.py (NEW — opt-in marker, requires Docker)
```

Files that change together (e.g., `client.py` + `test_browser_client.py`) live next to each other in the corresponding directories. Each module has one clear responsibility.

---

## Conventions used in every task

- Tests use `pytest` and `pytest-asyncio`. The repo's `pyproject.toml` already enables `asyncio_mode = "auto"` for the test directory, so `async def test_*` works without a `@pytest.mark.asyncio` decorator (verify by inspection of `tests/conftest.py` and existing tests like `tests/test_harness_api_client.py`). If absent on a given test you write, add the decorator explicitly.
- Use `httpx.MockTransport` for HTTP mocking, following the pattern in `tests/test_harness_api_client.py` (handler list of `(method, path, status, body)` tuples).
- Use a hand-rolled `FakeRedis` for Redis tests, following the pattern in `tests/test_rate_limit_guard.py`. Implement only the methods the code under test uses (`hset`, `hget`, `hdel`, `set`, `get`, `delete`, `expire`).
- Commit at the end of every task with the message shown. Stage only the files listed.
- Run the full test file, not just one test, to catch accidental regressions: `pytest tests/test_browser_X.py -v`.

---

## Task 1: Add browser event types and config settings

**Files:**
- Modify: `surogates/session/events.py`
- Modify: `surogates/config.py`
- Test: `tests/test_browser_base.py` (new file)

- [ ] **Step 1: Write the failing test**

Create `tests/test_browser_base.py`:

```python
"""Foundation tests: event types and config settings for the agent browser."""

from __future__ import annotations

import os

from surogates.session.events import EventType


def test_browser_event_types_exist() -> None:
    assert EventType.BROWSER_PROVISIONED.value == "browser.provisioned"
    assert EventType.BROWSER_DESTROYED.value == "browser.destroyed"


def test_browser_settings_defaults(monkeypatch) -> None:
    # Clear any inherited env so we test pristine defaults.
    for key in list(os.environ):
        if key.startswith("SUROGATES_BROWSER_"):
            monkeypatch.delenv(key, raising=False)

    from surogates.config import BrowserSettings

    s = BrowserSettings()
    # No `enabled` field — the browser is always on; backend choice is
    # the only environment switch.
    assert not hasattr(s, "enabled")
    assert s.backend == "process"
    assert s.image == "ghcr.io/onkernel/chromium-headful:stable"
    assert s.rest_port_base == 30000
    assert s.cdp_port_base == 31000
    assert s.live_view_port_base == 32000
    assert s.live_view_mode == "novnc"
    assert s.pod_ready_timeout == 60
    assert s.active_deadline_seconds == 3600
    assert s.cpu == "1"
    assert s.memory == "2Gi"
    assert s.cpu_limit == "2"
    assert s.memory_limit == "4Gi"


def test_browser_settings_env_override(monkeypatch) -> None:
    monkeypatch.setenv("SUROGATES_BROWSER_BACKEND", "kubernetes")
    monkeypatch.setenv("SUROGATES_BROWSER_REST_PORT_BASE", "40000")

    from surogates.config import BrowserSettings

    s = BrowserSettings()
    assert s.backend == "kubernetes"
    assert s.rest_port_base == 40000


def test_settings_includes_browser(monkeypatch) -> None:
    for key in list(os.environ):
        if key.startswith("SUROGATES_"):
            monkeypatch.delenv(key, raising=False)

    from surogates.config import Settings

    s = Settings()
    assert s.browser.backend == "process"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_browser_base.py -v
```

Expected: FAIL with `AttributeError: BROWSER_PROVISIONED` (event type missing) and/or `ImportError` for `BrowserSettings`.

- [ ] **Step 3: Add the new event types**

In `surogates/session/events.py`, after the existing `SAGA_*` block (around line 107) and before the closing of the `EventType` enum, add:

```python
    # Agent browser lifecycle (Phase A: provision/destroy only;
    # control + recording events arrive in Phases C/D).
    BROWSER_PROVISIONED = "browser.provisioned"
    BROWSER_DESTROYED = "browser.destroyed"
```

- [ ] **Step 4: Add `BrowserSettings` to `surogates/config.py`**

Insert a new class after the existing `SandboxSettings` class (around line 280, before `TransparencySettings`):

```python
class BrowserSettings(BaseSettings):
    """Agent browser configuration.

    The browser is implemented as a separate per-session resource (see
    spec §4). It is always enabled — there is no on/off switch. The
    ``backend`` choice follows the same dev/prod split as the sandbox:
    ``"process"`` runs kernel-images via ``docker run`` on the worker
    host (Phase A), ``"kubernetes"`` provisions per-session pods
    (Phase B). When ``backend == "process"``, the three ``*_port_base``
    settings allocate host ports per session (base + N).

    When the configured backend can't reach a running browser (e.g.,
    a worker without Docker), tool calls return ``browser_unavailable``
    and the agent learns to stop dispatching browser tools for that
    session.
    """

    model_config = {"env_prefix": "SUROGATES_BROWSER_"}

    backend: Literal["process", "kubernetes"] = "process"
    image: str = "ghcr.io/onkernel/chromium-headful:stable"

    # Process backend (Phase A) port allocation.
    rest_port_base: int = 30000        # docker -p {base + N}:10001
    cdp_port_base: int = 31000         # docker -p {base + N}:9222
    live_view_port_base: int = 32000   # docker -p {base + N}:6080 (NoVNC v1)
    live_view_mode: Literal["novnc", "webrtc"] = "novnc"

    # Kubernetes backend (Phase B). Defaults match SandboxSettings.
    k8s_namespace: str = "surogates"
    k8s_service_account: str = "surogates-browser"
    pod_ready_timeout: int = 60
    active_deadline_seconds: int = 3600

    # Resource requests / limits (Phase B).
    cpu: str = "1"
    memory: str = "2Gi"
    cpu_limit: str = "2"
    memory_limit: str = "4Gi"
```

Then add to the top-level `Settings` class (around line 489), before the `slack` field:

```python
    browser: BrowserSettings = Field(default_factory=BrowserSettings)
```

- [ ] **Step 5: Run test to verify it passes**

```bash
pytest tests/test_browser_base.py -v
```

Expected: 4 tests PASS.

- [ ] **Step 6: Commit**

```bash
git add tests/test_browser_base.py surogates/session/events.py surogates/config.py
git commit -m "feat(browser): add browser event types and config settings"
```

---

## Task 2: Define `BrowserBackend` protocol and value types

**Files:**
- Create: `surogates/browser/__init__.py`
- Create: `surogates/browser/base.py`
- Test: `tests/test_browser_base.py` (extend)

- [ ] **Step 1: Write the failing tests** — append to `tests/test_browser_base.py`:

```python
import json

import pytest


def test_browser_status_values() -> None:
    from surogates.browser.base import BrowserStatus

    assert BrowserStatus.RUNNING.value == "running"
    assert BrowserStatus.PENDING.value == "pending"
    assert BrowserStatus.FAILED.value == "failed"
    assert BrowserStatus.TERMINATED.value == "terminated"


def test_browser_spec_defaults() -> None:
    from surogates.browser.base import BrowserSpec

    spec = BrowserSpec()
    assert spec.image == "ghcr.io/onkernel/chromium-headful:stable"
    assert spec.cpu == "1"
    assert spec.memory == "2Gi"
    assert spec.cpu_limit == "2"
    assert spec.memory_limit == "4Gi"
    assert spec.pod_ready_timeout == 60
    assert spec.active_deadline_seconds == 3600
    assert spec.env == {}


def test_browser_spec_overrides() -> None:
    from surogates.browser.base import BrowserSpec

    spec = BrowserSpec(image="custom:1", cpu="500m", env={"FOO": "bar"})
    assert spec.image == "custom:1"
    assert spec.cpu == "500m"
    assert spec.env == {"FOO": "bar"}


def test_browser_unavailable_result_shape() -> None:
    from surogates.browser.base import browser_unavailable_result

    payload = json.loads(browser_unavailable_result("kubelet busy"))
    assert payload["error"] == "browser_unavailable"
    assert payload["reason"] == "kubelet busy"
    assert "guidance" in payload


def test_browser_unavailable_error_classifies() -> None:
    from surogates.browser.base import BrowserUnavailableError

    exc = BrowserUnavailableError("docker pull failed", classification="image")
    assert exc.reason == "docker pull failed"
    assert exc.classification == "image"


def test_browser_endpoint_helpers() -> None:
    from surogates.browser.base import BrowserEndpoint

    ep = BrowserEndpoint(
        rest_url="http://10.0.0.5:30000",
        cdp_url="ws://10.0.0.5:31000",
        live_view_url="ws://10.0.0.5:32000",
    )
    assert ep.rest_url.endswith(":30000")
    # All three components are required.
    with pytest.raises(TypeError):
        BrowserEndpoint(rest_url="http://x")  # type: ignore[call-arg]
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_browser_base.py -v
```

Expected: 6 new tests FAIL (`ImportError`).

- [ ] **Step 3: Create the package marker**

`surogates/browser/__init__.py`:

```python
"""Agent browser package — kernel-images-backed browser per session.

See ``docs/superpowers/specs/2026-05-10-agent-browser-design.md`` for
architecture. This package is the worker-side counterpart to the
``api/routes/browser.py`` HTTP layer that arrives in Phase C.
"""
```

- [ ] **Step 4: Create the protocol and value types**

`surogates/browser/base.py`:

```python
"""Browser backend protocol and value types.

Every browser backend (process, kubernetes) implements ``BrowserBackend``.
The protocol owns *lifecycle* (provision / status / destroy); the actual
browser-driving REST calls go through ``KernelBrowserClient`` against
the URL the backend returns from ``provision``.

Why a separate protocol from the lifecycle of ``Sandbox``?

The browser pod has three meaningful network ports (REST API, CDP,
live view) where the workspace sandbox only needs one (exec). Lumping
both under one protocol would force the sandbox to grow URL fields it
doesn't need or force the browser to fake a single ``execute`` channel
when its real surface is HTTP. They have parallel shapes but distinct
contracts; a separate protocol keeps each honest.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol


class BrowserUnavailableError(RuntimeError):
    """Raised when the browser subsystem is broken (image pull, node
    pressure, daemon down, etc.).

    Distinct from a tool-level failure (bad selector, navigation
    timeout). Surfaces to the LLM via ``browser_unavailable_result``
    so the model recognises the failure class and stops dispatching
    other ``browser_*`` tools that would all fail identically.
    """

    def __init__(self, reason: str, *, classification: str = "infra") -> None:
        super().__init__(reason)
        self.reason = reason
        self.classification = classification


def browser_unavailable_result(
    reason: str, *, tools_affected: list[str] | None = None,
) -> str:
    """JSON tool-result body returned when the browser is unavailable.

    Mirrors :func:`surogates.sandbox.base.sandbox_unavailable_result`
    so the LLM treats it the same way (give up; don't retry every
    browser tool).
    """
    payload: dict[str, object] = {
        "error": "browser_unavailable",
        "reason": reason,
        "guidance": (
            "The agent browser is unavailable -- every browser_* tool "
            "will fail with the same error until the underlying "
            "infrastructure is fixed. Do not retry browser tools. "
            "Use web_search / web_extract / web_crawl for read-only "
            "page access, or report the failure to the user."
        ),
    }
    if tools_affected:
        payload["tools_affected"] = tools_affected
    return json.dumps(payload)


class BrowserStatus(str, Enum):
    """Observable lifecycle states for a browser instance."""

    RUNNING = "running"
    PENDING = "pending"
    FAILED = "failed"
    TERMINATED = "terminated"


@dataclass(frozen=True, slots=True)
class BrowserEndpoint:
    """The three URLs a backend exposes after provisioning a browser.

    - ``rest_url``      kernel-images REST API base (e.g. http://host:30000)
    - ``cdp_url``       CDP WebSocket (used by future Playwright integrations)
    - ``live_view_url`` NoVNC (Phase A) / WebRTC (future). Service-internal;
                        Phase C wraps it in an authenticated proxy.
    """

    rest_url: str
    cdp_url: str
    live_view_url: str


@dataclass(slots=True)
class BrowserSpec:
    """Desired-state spec for provisioning a browser pod / container.

    Resource units mirror :class:`surogates.sandbox.base.SandboxSpec`:
    ``cpu`` / ``memory`` are K8s requests, ``cpu_limit`` / ``memory_limit``
    are limits. ``timeout`` is the per-tool-call upstream timeout (passed
    to httpx).
    """

    image: str = "ghcr.io/onkernel/chromium-headful:stable"
    cpu: str = "1"
    memory: str = "2Gi"
    cpu_limit: str = "2"
    memory_limit: str = "4Gi"
    pod_ready_timeout: int = 60
    active_deadline_seconds: int = 3600
    timeout: int = 60
    env: dict[str, str] = field(default_factory=dict)


class BrowserBackend(Protocol):
    """Backend-agnostic browser lifecycle protocol.

    Implementations: :class:`~surogates.browser.process.ProcessBrowserBackend`
    (Phase A, docker run) and :class:`~surogates.browser.kubernetes.K8sBrowserBackend`
    (Phase B, K8s pod).
    """

    async def provision(self, spec: BrowserSpec) -> tuple[str, BrowserEndpoint]:
        """Create a browser instance.

        Returns ``(browser_id, endpoint)``. The browser is ready to
        accept REST calls when this returns; the backend is responsible
        for waiting until kernel-images logs the DevTools listening line.
        """
        ...

    async def status(self, browser_id: str) -> BrowserStatus:
        """Return the current lifecycle state."""
        ...

    async def destroy(self, browser_id: str) -> None:
        """Tear down the instance and free its resources."""
        ...
```

- [ ] **Step 5: Run tests**

```bash
pytest tests/test_browser_base.py -v
```

Expected: all 10 tests PASS (4 from Task 1 + 6 new).

- [ ] **Step 6: Commit**

```bash
git add surogates/browser/__init__.py surogates/browser/base.py tests/test_browser_base.py
git commit -m "feat(browser): add BrowserBackend protocol and value types"
```

---

## Task 3: `KernelBrowserClient` skeleton (init / close / context manager)

**Files:**
- Create: `surogates/browser/client.py`
- Test: `tests/test_browser_client.py` (new file)

- [ ] **Step 1: Write the failing tests**

`tests/test_browser_client.py`:

```python
"""Tests for surogates.browser.client.KernelBrowserClient."""

from __future__ import annotations

import json

import httpx
import pytest

from surogates.browser.client import KernelBrowserClient


@pytest.fixture()
def mock_transport():
    """Build a list of ``(method, path, status, body)`` handlers."""
    handlers: list[tuple] = []

    class MockTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            for method, path, status, body in handlers:
                if request.method == method and request.url.path == path:
                    if isinstance(body, bytes):
                        return httpx.Response(status, content=body)
                    return httpx.Response(status, json=body)
            return httpx.Response(404, json={"error": "not found", "path": request.url.path})

    return handlers, MockTransport()


@pytest.fixture()
def client_with_transport(mock_transport):
    """Create a KernelBrowserClient using a mock transport."""
    handlers, transport = mock_transport
    client = KernelBrowserClient(rest_url="http://browser-test:10001")
    client._http = httpx.AsyncClient(
        base_url="http://browser-test:10001",
        transport=transport,
        timeout=5.0,
    )
    return client, handlers


class TestClientLifecycle:
    async def test_close_disposes_http(self) -> None:
        client = KernelBrowserClient(rest_url="http://x:10001")
        await client.close()
        assert client._closed is True

    async def test_context_manager_closes(self) -> None:
        async with KernelBrowserClient(rest_url="http://x:10001") as client:
            assert client._closed is False
        assert client._closed is True

    async def test_double_close_is_noop(self) -> None:
        client = KernelBrowserClient(rest_url="http://x:10001")
        await client.close()
        await client.close()
        assert client._closed is True

    async def test_rest_url_is_normalized(self) -> None:
        # Trailing slash stripped so endpoint paths concatenate cleanly.
        client = KernelBrowserClient(rest_url="http://x:10001/")
        assert client.rest_url == "http://x:10001"
        await client.close()
```

- [ ] **Step 2: Run tests** — `pytest tests/test_browser_client.py -v` → 4 FAIL with `ImportError`.

- [ ] **Step 3: Create the client**

`surogates/browser/client.py`:

```python
"""Async HTTP client for kernel-images REST API.

Wraps the kernel-images Go server's REST surface (see
``study/kernel-images/server/openapi.yaml``) behind a typed Python
facade. Methods correspond 1:1 to the discrete tool surface (navigate,
get_state, click, type, ...) defined in spec §5.

Caches the most recent DOM-derived snapshot in a caller-supplied
per-session dict so ``@e1``-style refs in subsequent calls
(``click_ref``, ``type_ref``) resolve to coordinates locally without
round-tripping. The cache is invalidated by every mutating action.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class KernelBrowserClient:
    """HTTP client for a single kernel-images container's REST API."""

    def __init__(
        self,
        rest_url: str,
        *,
        timeout: float = 30.0,
        snapshot_cache: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.rest_url = rest_url.rstrip("/")
        self._timeout = timeout
        self._http: httpx.AsyncClient = httpx.AsyncClient(
            base_url=self.rest_url,
            timeout=timeout,
        )
        self._closed = False
        # ref → (x, y, role, name) cache, populated by get_state.
        # Browser tools create a fresh client per call, so production
        # handlers pass the per-session dict owned by BrowserPool. Unit tests
        # can omit it and get a client-local cache.
        self._snapshot_cache = snapshot_cache if snapshot_cache is not None else {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def close(self) -> None:
        if self._closed:
            return
        await self._http.aclose()
        self._closed = True

    async def __aenter__(self) -> "KernelBrowserClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()
```

- [ ] **Step 4: Run tests** — `pytest tests/test_browser_client.py -v` → 4 PASS.

- [ ] **Step 5: Commit**

```bash
git add surogates/browser/client.py tests/test_browser_client.py
git commit -m "feat(browser): add KernelBrowserClient skeleton"
```

---

## Task 4: `KernelBrowserClient.navigate`

**Files:**
- Modify: `surogates/browser/client.py`
- Test: `tests/test_browser_client.py` (extend)

- [ ] **Step 1: Write the failing test** — append to `tests/test_browser_client.py`:

```python
class TestNavigate:
    async def test_navigate_returns_url_and_title(self, client_with_transport) -> None:
        client, handlers = client_with_transport
        handlers.append((
            "POST", "/playwright/execute", 200,
            {
                "success": True,
                "result": {"url": "https://example.com/", "title": "Example"},
            },
        ))
        result = await client.navigate("https://example.com")
        assert result["url"] == "https://example.com/"
        assert result["title"] == "Example"

    async def test_navigate_propagates_kernel_error(self, client_with_transport) -> None:
        client, handlers = client_with_transport
        handlers.append((
            "POST", "/playwright/execute", 200,
            {"success": False, "error": "ERR_NAME_NOT_RESOLVED"},
        ))
        with pytest.raises(RuntimeError, match="ERR_NAME_NOT_RESOLVED"):
            await client.navigate("https://nope.invalid")

    async def test_navigate_invalidates_snapshot_cache(self, client_with_transport) -> None:
        client, handlers = client_with_transport
        client._snapshot_cache["@e1"] = {"x": 10, "y": 10, "role": "button", "name": "x"}
        handlers.append((
            "POST", "/playwright/execute", 200,
            {"success": True, "result": {"url": "https://example.com/", "title": "Example"}},
        ))
        await client.navigate("https://example.com")
        assert client._snapshot_cache == {}
```

- [ ] **Step 2: Run** — 3 FAIL.

- [ ] **Step 3: Implement `navigate`** — append to `surogates/browser/client.py`:

```python
    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    async def navigate(self, url: str, *, wait_until: str = "load") -> dict[str, Any]:
        """Navigate the page to *url* and return the final URL + title.

        Implemented via ``/playwright/execute`` since kernel-images'
        ``/computer/*`` endpoints don't include a primitive for
        navigation. Cache invalidates because the DOM changed.
        """
        code = (
            "await page.goto({url!r}, {{waitUntil: {wait_until!r}}});\n"
            "return {{ url: page.url(), title: await page.title() }};"
        ).format(url=url, wait_until=wait_until)
        result = await self._playwright_execute(code)
        self._invalidate_snapshot_cache()
        return result

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _playwright_execute(
        self,
        code: str,
        *,
        timeout_sec: int = 60,
    ) -> Any:
        """POST /playwright/execute and unwrap the ``{success, result, error}`` envelope.

        Raises :class:`RuntimeError` when ``success`` is False so the
        caller can decide whether to surface as a tool-level error or
        as ``browser_unavailable``.
        """
        resp = await self._http.post(
            "/playwright/execute",
            json={"code": code, "timeout_sec": timeout_sec},
        )
        resp.raise_for_status()
        body = resp.json()
        if not body.get("success", False):
            raise RuntimeError(body.get("error") or "playwright execute failed")
        return body.get("result")

    def _invalidate_snapshot_cache(self) -> None:
        """Drop the cached DOM snapshot. Called after every mutating action."""
        self._snapshot_cache.clear()
```

- [ ] **Step 4: Run** — `pytest tests/test_browser_client.py -v` → all PASS (7 total).

- [ ] **Step 5: Commit**

```bash
git add surogates/browser/client.py tests/test_browser_client.py
git commit -m "feat(browser): add KernelBrowserClient.navigate"
```

---

## Task 5: `KernelBrowserClient.get_state` with refs and cache

**Files:**
- Modify: `surogates/browser/client.py`
- Test: `tests/test_browser_client.py` (extend)

- [ ] **Step 1: Write the failing test** — append to `tests/test_browser_client.py`:

```python
class TestGetState:
    async def test_get_state_returns_tree_with_refs(self, client_with_transport) -> None:
        client, handlers = client_with_transport
        # The Playwright snapshot script returns nodes with x/y/role/name fields.
        handlers.append((
            "POST", "/playwright/execute", 200,
            {
                "success": True,
                "result": {
                    "url": "https://example.com/",
                    "title": "Example",
                    "viewport": {"width": 1280, "height": 800},
                    "nodes": [
                        {"role": "link", "name": "Settings", "x": 1130, "y": 24,
                         "width": 80, "height": 32},
                        {"role": "button", "name": "New project", "x": 200, "y": 80,
                         "width": 120, "height": 36},
                    ],
                },
            },
        ))
        state = await client.get_state()
        assert state["url"] == "https://example.com/"
        assert state["viewport"] == {"width": 1280, "height": 800}
        # Refs are assigned in tree order, starting at @e1.
        assert state["tree"][0]["ref"] == "@e1"
        assert state["tree"][0]["role"] == "link"
        assert state["tree"][0]["name"] == "Settings"
        assert state["tree"][1]["ref"] == "@e2"

    async def test_get_state_populates_snapshot_cache(self, client_with_transport) -> None:
        client, handlers = client_with_transport
        handlers.append((
            "POST", "/playwright/execute", 200,
            {
                "success": True,
                "result": {
                    "url": "u", "title": "t", "viewport": {"width": 100, "height": 100},
                    "nodes": [{"role": "button", "name": "Go", "x": 10, "y": 20,
                               "width": 50, "height": 30}],
                },
            },
        ))
        await client.get_state()
        # Click target is the centre of the bounding box.
        cached = client._snapshot_cache["@e1"]
        assert cached["x"] == 10 + 50 // 2
        assert cached["y"] == 20 + 30 // 2
        assert cached["role"] == "button"
        assert cached["name"] == "Go"

    async def test_get_state_overwrites_old_cache(self, client_with_transport) -> None:
        client, handlers = client_with_transport
        client._snapshot_cache["@e9"] = {"x": 0, "y": 0, "role": "stale", "name": "stale"}
        handlers.append((
            "POST", "/playwright/execute", 200,
            {
                "success": True,
                "result": {
                    "url": "u", "title": "t", "viewport": {"width": 1, "height": 1},
                    "nodes": [{"role": "button", "name": "fresh", "x": 1, "y": 1,
                               "width": 0, "height": 0}],
                },
            },
        ))
        await client.get_state()
        assert "@e9" not in client._snapshot_cache
        assert "@e1" in client._snapshot_cache
```

- [ ] **Step 2: Run** — 3 FAIL.

- [ ] **Step 3: Implement `get_state`** — append to `surogates/browser/client.py`:

```python
    # ------------------------------------------------------------------
    # Perception
    # ------------------------------------------------------------------

    # The snapshot script runs inside the kernel-images browser context
    # via /playwright/execute. Do NOT use page.accessibility.snapshot()
    # for coordinates: Playwright's accessibility nodes are plain data,
    # not ElementHandles, so they cannot provide bounding boxes. Instead
    # scan visible DOM elements and compute an accessibility-inspired
    # role/name plus viewport-relative bbox.
    _SNAPSHOT_SCRIPT = """
function roleOf(el) {
  const explicit = el.getAttribute('role');
  if (explicit) return explicit;
  const tag = el.tagName.toLowerCase();
  const type = (el.getAttribute('type') || '').toLowerCase();
  if (tag === 'button') return 'button';
  if (tag === 'a' && el.hasAttribute('href')) return 'link';
  if (tag === 'textarea') return 'textbox';
  if (tag === 'select') return 'combobox';
  if (tag === 'input') {
    if (type === 'checkbox') return 'checkbox';
    if (type === 'radio') return 'radio';
    if (type === 'range') return 'slider';
    if (type === 'number') return 'spinbutton';
    if (type === 'search') return 'searchbox';
    return 'textbox';
  }
  if (/^h[1-6]$/.test(tag)) return 'heading';
  if (tag === 'img') return 'img';
  return 'generic';
}

function nameOf(el) {
  const direct = el.getAttribute('aria-label')
    || el.getAttribute('title')
    || el.getAttribute('alt')
    || el.getAttribute('placeholder')
    || el.value
    || el.innerText
    || el.textContent
    || '';
  return String(direct).replace(/\\s+/g, ' ').trim().slice(0, 240);
}

function depthOf(el) {
  let d = 0, cur = el;
  while (cur && cur.parentElement) { d++; cur = cur.parentElement; }
  return d;
}

const out = [];
for (const el of Array.from(document.querySelectorAll('*'))) {
  const style = window.getComputedStyle(el);
  if (style.visibility === 'hidden' || style.display === 'none') continue;
  const bbox = el.getBoundingClientRect();
  if (!bbox || bbox.width <= 0 || bbox.height <= 0) continue;
  out.push({
    role: roleOf(el),
    name: nameOf(el),
    x: Math.round(bbox.x),
    y: Math.round(bbox.y),
    width: Math.round(bbox.width),
    height: Math.round(bbox.height),
    depth: depthOf(el),
    children_count: el.children ? el.children.length : 0,
  });
}
return {
  url: page.url(),
  title: await page.title(),
  viewport: page.viewportSize() || {width: 0, height: 0},
  nodes: out,
};
"""

    async def get_state(self) -> dict[str, Any]:
        """Return the current page's DOM-derived tree with stable ``@e1`` refs.

        Refs are assigned in tree order before any filtering. Coordinates
        are stored as bbox centres in the shared per-session cache so
        ``click_ref`` can resolve refs across separate tool calls.
        """
        raw = await self._playwright_execute(self._SNAPSHOT_SCRIPT)
        nodes = raw.get("nodes", [])

        tree: list[dict[str, Any]] = []
        new_cache: dict[str, dict[str, Any]] = {}
        for idx, node in enumerate(nodes, start=1):
            ref = f"@e{idx}"
            bbox_x = int(node.get("x", 0))
            bbox_y = int(node.get("y", 0))
            bbox_w = int(node.get("width", 0))
            bbox_h = int(node.get("height", 0))
            cx = bbox_x + bbox_w // 2
            cy = bbox_y + bbox_h // 2
            entry = {
                "ref": ref,
                "role": node.get("role", ""),
                "name": node.get("name", ""),
                "x": cx,
                "y": cy,
            }
            tree.append(entry)
            new_cache[ref] = {
                "x": cx, "y": cy,
                "role": node.get("role", ""),
                "name": node.get("name", ""),
            }

        # Replace in-place (don't rebind): production clients receive the
        # per-session dict owned by BrowserPool, so later tool calls must see
        # the refreshed refs.
        self._snapshot_cache.clear()
        self._snapshot_cache.update(new_cache)

        return {
            "url": raw.get("url", ""),
            "title": raw.get("title", ""),
            "viewport": raw.get("viewport", {"width": 0, "height": 0}),
            "tree": tree,
        }
```

- [ ] **Step 4: Run** — all PASS.

- [ ] **Step 5: Commit**

```bash
git add surogates/browser/client.py tests/test_browser_client.py
git commit -m "feat(browser): add KernelBrowserClient.get_state with ref cache"
```

---

## Task 6: `get_state` filter parameters (`interactive_only`, `compact`, `max_depth`, `selector`)

**Files:**
- Modify: `surogates/browser/client.py`
- Test: `tests/test_browser_client.py` (extend)

- [ ] **Step 1: Write the failing tests** — append:

```python
INTERACTIVE_ROLES = {
    "button", "link", "textbox", "combobox", "checkbox", "radio",
    "menuitem", "tab", "switch", "searchbox", "slider", "spinbutton",
}


class TestGetStateFilters:
    @pytest.fixture()
    def deep_response(self) -> dict[str, Any]:
        return {
            "success": True,
            "result": {
                "url": "u", "title": "t", "viewport": {"width": 1, "height": 1},
                "nodes": [
                    {"role": "generic", "name": "", "x": 0, "y": 0, "width": 0, "height": 0},
                    {"role": "button", "name": "Go", "x": 10, "y": 10, "width": 1, "height": 1},
                    {"role": "paragraph", "name": "", "x": 0, "y": 20, "width": 0, "height": 0},
                    {"role": "link", "name": "Home", "x": 30, "y": 30, "width": 1, "height": 1},
                ],
            },
        }

    async def test_interactive_only_drops_structural_nodes(
        self, client_with_transport, deep_response
    ) -> None:
        client, handlers = client_with_transport
        handlers.append(("POST", "/playwright/execute", 200, deep_response))
        state = await client.get_state(interactive_only=True)
        roles = {n["role"] for n in state["tree"]}
        assert roles == {"button", "link"}
        # Refs remain stable: button is @e2 from full tree.
        assert state["tree"][0]["ref"] == "@e2"
        assert state["tree"][1]["ref"] == "@e4"

    async def test_filters_dont_corrupt_cache(
        self, client_with_transport, deep_response
    ) -> None:
        client, handlers = client_with_transport
        handlers.append(("POST", "/playwright/execute", 200, deep_response))
        await client.get_state(interactive_only=True)
        # All four refs are present in the cache regardless of filter.
        assert set(client._snapshot_cache.keys()) == {"@e1", "@e2", "@e3", "@e4"}

    async def test_max_depth_truncates(self, client_with_transport, deep_response) -> None:
        client, handlers = client_with_transport
        # Add a depth field to drive max_depth.
        deep_response = {
            "success": True,
            "result": {
                **deep_response["result"],
                "nodes": [
                    {**n, "depth": d}
                    for n, d in zip(deep_response["result"]["nodes"], [0, 1, 1, 3])
                ],
            },
        }
        handlers.append(("POST", "/playwright/execute", 200, deep_response))
        state = await client.get_state(max_depth=2)
        # Only nodes with depth <= 2 survive.
        assert len(state["tree"]) == 3
        assert all(n["ref"] != "@e4" for n in state["tree"])
```

Helper for sender of `INTERACTIVE_ROLES`: this set is the canonical list of
interactive ARIA roles `get_state(interactive_only=True)` keeps. Keep the
list inside `client.py`; the test imports it from there if needed.

- [ ] **Step 2: Run** — 3 FAIL.

- [ ] **Step 3: Implement filters** — replace `get_state` and add the constant:

```python
    # Canonical set of interactive ARIA roles. Used by get_state(interactive_only=True).
    # Borrowed from browser-agent DOM snapshot UX patterns.
    _INTERACTIVE_ROLES: frozenset[str] = frozenset({
        "button", "link", "textbox", "combobox", "checkbox", "radio",
        "menuitem", "tab", "switch", "searchbox", "slider", "spinbutton",
    })

    async def get_state(
        self,
        *,
        interactive_only: bool = False,
        compact: bool = False,
        max_depth: int | None = None,
        selector: str | None = None,
    ) -> dict[str, Any]:
        """Return the current page's DOM-derived tree with stable refs and filters.

        Refs are assigned in tree order *before* filtering, so ``@e2``
        always points to the same DOM element no matter which filters
        the caller used. The cache always contains the full set of refs.
        """
        # Scope the DOM scan to an optional CSS selector. Use JSON encoding
        # rather than f-string interpolation inside JavaScript.
        script = self._SNAPSHOT_SCRIPT
        if selector:
            import json as _json
            selector_json = _json.dumps(selector)
            script = script.replace(
                "for (const el of Array.from(document.querySelectorAll('*'))) {",
                (
                    f"const __root = document.querySelector({selector_json});\n"
                    "if (!__root) throw new Error('selector matched no element');\n"
                    "for (const el of Array.from(__root.querySelectorAll('*'))) {"
                ),
            )
        raw = await self._playwright_execute(script)
        nodes = raw.get("nodes", [])

        tree: list[dict[str, Any]] = []
        new_cache: dict[str, dict[str, Any]] = {}
        for idx, node in enumerate(nodes, start=1):
            ref = f"@e{idx}"
            bbox_x = int(node.get("x", 0))
            bbox_y = int(node.get("y", 0))
            bbox_w = int(node.get("width", 0))
            bbox_h = int(node.get("height", 0))
            cx = bbox_x + bbox_w // 2
            cy = bbox_y + bbox_h // 2
            entry = {
                "ref": ref,
                "role": node.get("role", ""),
                "name": node.get("name", ""),
                "x": cx,
                "y": cy,
            }
            depth = node.get("depth")
            if depth is not None:
                entry["depth"] = int(depth)
            new_cache[ref] = {
                "x": cx, "y": cy,
                "role": node.get("role", ""),
                "name": node.get("name", ""),
            }

            # Apply filters.
            if interactive_only and entry["role"] not in self._INTERACTIVE_ROLES:
                continue
            if compact and not entry["name"] and entry["role"] not in self._INTERACTIVE_ROLES:
                continue
            if max_depth is not None and depth is not None and int(depth) > max_depth:
                continue
            tree.append(entry)

        # Replace in-place so BrowserPool's per-session cache object remains shared.
        self._snapshot_cache.clear()
        self._snapshot_cache.update(new_cache)

        return {
            "url": raw.get("url", ""),
            "title": raw.get("title", ""),
            "viewport": raw.get("viewport", {"width": 0, "height": 0}),
            "tree": tree,
        }
```

- [ ] **Step 4: Run** — all PASS.

- [ ] **Step 5: Commit**

```bash
git add surogates/browser/client.py tests/test_browser_client.py
git commit -m "feat(browser): add DOM snapshot filters to get_state"
```

---

## Task 7: `KernelBrowserClient` click + type (ref + coords)

**Files:**
- Modify: `surogates/browser/client.py`
- Test: `tests/test_browser_client.py` (extend)

- [ ] **Step 1: Write the failing tests** — append:

```python
class TestClickType:
    async def test_click_at_coords(self, client_with_transport) -> None:
        client, handlers = client_with_transport
        handlers.append(("POST", "/computer/click_mouse", 200, {"ok": True}))
        await client.click_at(120, 240)
        # No exception; cache invalidated.
        assert client._snapshot_cache == {}

    async def test_click_ref_resolves_from_cache(self, client_with_transport) -> None:
        client, handlers = client_with_transport
        client._snapshot_cache["@e3"] = {"x": 50, "y": 60, "role": "button", "name": "Go"}

        captured: list[dict] = []

        class CapturingTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
                captured.append(json.loads(request.content))
                return httpx.Response(200, json={"ok": True})

        client._http = httpx.AsyncClient(
            base_url=client.rest_url,
            transport=CapturingTransport(),
        )
        await client.click_ref("@e3")
        assert captured[0] == {"x": 50, "y": 60, "click_type": "click"}

    async def test_click_ref_unknown_raises(self, client_with_transport) -> None:
        client, _ = client_with_transport
        with pytest.raises(KeyError, match="@e99"):
            await client.click_ref("@e99")

    async def test_type_text_invalidates_cache(self, client_with_transport) -> None:
        client, handlers = client_with_transport
        client._snapshot_cache["@e1"] = {"x": 1, "y": 1, "role": "textbox", "name": "Email"}
        handlers.append(("POST", "/computer/type", 200, {"ok": True}))
        await client.type_text("hello")
        assert client._snapshot_cache == {}

    async def test_type_into_ref_clicks_first(self, client_with_transport) -> None:
        client, handlers = client_with_transport
        client._snapshot_cache["@e2"] = {"x": 30, "y": 40, "role": "textbox", "name": "Email"}
        handlers.append(("POST", "/computer/click_mouse", 200, {"ok": True}))
        handlers.append(("POST", "/computer/type", 200, {"ok": True}))
        await client.type_into_ref("@e2", "test@example.com")
        # Cache cleared after the click invalidation, plus the second cache invalidation from type
        # so type_into_ref's @e2 lookup must happen *before* clicking. This is asserted by the call
        # path completing without KeyError.
        assert client._snapshot_cache == {}
```

- [ ] **Step 2: Run** — 5 FAIL.

- [ ] **Step 3: Implement click + type** — append to `surogates/browser/client.py`:

```python
    # ------------------------------------------------------------------
    # Click / type
    # ------------------------------------------------------------------

    async def click_at(
        self,
        x: int,
        y: int,
        *,
        button: str = "left",
        click_type: str = "click",
        num_clicks: int = 1,
    ) -> None:
        """Click at absolute viewport coordinates."""
        body: dict[str, Any] = {"x": x, "y": y, "click_type": click_type}
        if button != "left":
            body["button"] = button
        if num_clicks != 1:
            body["num_clicks"] = num_clicks
        resp = await self._http.post("/computer/click_mouse", json=body)
        resp.raise_for_status()
        self._invalidate_snapshot_cache()

    async def click_ref(self, ref: str, **kwargs: Any) -> None:
        """Resolve *ref* against the cached snapshot and click its centre."""
        coords = self._resolve_ref(ref)
        await self.click_at(coords["x"], coords["y"], **kwargs)

    async def type_text(self, text: str, *, delay_ms: int = 0) -> None:
        """Type *text* at the current focus."""
        body: dict[str, Any] = {"text": text, "smooth": False}
        if delay_ms:
            body["delay"] = delay_ms
        resp = await self._http.post("/computer/type", json=body)
        resp.raise_for_status()
        self._invalidate_snapshot_cache()

    async def type_into_ref(self, ref: str, text: str, **kwargs: Any) -> None:
        """Resolve *ref*, click it to focus, then type *text*."""
        # Resolve BEFORE clicking — click invalidates the cache.
        coords = self._resolve_ref(ref)
        await self.click_at(coords["x"], coords["y"])
        await self.type_text(text, **kwargs)

    def _resolve_ref(self, ref: str) -> dict[str, Any]:
        entry = self._snapshot_cache.get(ref)
        if entry is None:
            raise KeyError(
                f"Unknown ref {ref!r}; call browser_get_state to refresh the cache",
            )
        return entry
```

- [ ] **Step 4: Run** — all PASS.

- [ ] **Step 5: Commit**

```bash
git add surogates/browser/client.py tests/test_browser_client.py
git commit -m "feat(browser): add click_at / click_ref / type_text / type_into_ref"
```

---

## Task 8: `KernelBrowserClient` press_key, scroll, drag, wait

**Files:**
- Modify: `surogates/browser/client.py`
- Test: `tests/test_browser_client.py` (extend)

- [ ] **Step 1: Write the failing tests** — append:

```python
class TestSmallActions:
    async def test_press_key_single(self, client_with_transport) -> None:
        client, handlers = client_with_transport
        handlers.append(("POST", "/computer/press_key", 200, {"ok": True}))
        await client.press_key("Enter")
        assert client._snapshot_cache == {}

    async def test_press_key_chord(self, client_with_transport) -> None:
        client, handlers = client_with_transport
        captured: list[dict] = []

        class CapturingTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
                captured.append(json.loads(request.content))
                return httpx.Response(200, json={"ok": True})

        client._http = httpx.AsyncClient(base_url=client.rest_url, transport=CapturingTransport())
        await client.press_key("Ctrl+l")
        assert captured[0]["keys"] == ["Ctrl+l"]

    async def test_scroll_at(self, client_with_transport) -> None:
        client, handlers = client_with_transport
        handlers.append(("POST", "/computer/scroll", 200, {"ok": True}))
        await client.scroll_at(640, 400, delta_y=300)
        assert client._snapshot_cache == {}

    async def test_drag(self, client_with_transport) -> None:
        client, handlers = client_with_transport
        handlers.append(("POST", "/computer/drag_mouse", 200, {"ok": True}))
        await client.drag([(10, 10), (200, 200)])
        assert client._snapshot_cache == {}

    async def test_wait_sleeps(self, client_with_transport) -> None:
        client, _ = client_with_transport
        import time
        t0 = time.perf_counter()
        await client.wait(50)  # 50 ms
        elapsed = (time.perf_counter() - t0) * 1000
        assert 40 <= elapsed < 500   # generous upper bound for slow CI
        # wait does NOT invalidate the cache.
        # (cache wasn't populated above, so just check it didn't gain anything)
        assert client._snapshot_cache == {}
```

- [ ] **Step 2: Run** — 5 FAIL.

- [ ] **Step 3: Implement** — append:

```python
    # ------------------------------------------------------------------
    # Keys / scroll / drag / wait
    # ------------------------------------------------------------------

    async def press_key(self, *keys: str, duration_ms: int = 0) -> None:
        body: dict[str, Any] = {"keys": list(keys)}
        if duration_ms:
            body["duration"] = duration_ms
        resp = await self._http.post("/computer/press_key", json=body)
        resp.raise_for_status()
        self._invalidate_snapshot_cache()

    async def scroll_at(self, x: int, y: int, *, delta_x: int = 0, delta_y: int = 0) -> None:
        body = {"x": x, "y": y, "delta_x": delta_x, "delta_y": delta_y}
        resp = await self._http.post("/computer/scroll", json=body)
        resp.raise_for_status()
        self._invalidate_snapshot_cache()

    async def drag(
        self,
        path: list[tuple[int, int]],
        *,
        button: str = "left",
    ) -> None:
        if len(path) < 2:
            raise ValueError("drag path must contain at least two points")
        body: dict[str, Any] = {
            "path": [list(p) for p in path],
            "smooth": False,
        }
        if button != "left":
            body["button"] = button
        resp = await self._http.post("/computer/drag_mouse", json=body)
        resp.raise_for_status()
        self._invalidate_snapshot_cache()

    async def wait(self, ms: int) -> None:
        """Sleep for *ms* milliseconds. Does NOT touch the cache."""
        import asyncio as _asyncio
        await _asyncio.sleep(max(0, ms) / 1000.0)
```

- [ ] **Step 4: Run** — all PASS.

- [ ] **Step 5: Commit**

```bash
git add surogates/browser/client.py tests/test_browser_client.py
git commit -m "feat(browser): add press_key / scroll / drag / wait"
```

---

## Task 9: `KernelBrowserClient.screenshot` (with `annotate`)

**Files:**
- Modify: `surogates/browser/client.py`
- Test: `tests/test_browser_client.py` (extend)

- [ ] **Step 1: Write the failing tests** — append:

```python
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


class TestScreenshot:
    async def test_screenshot_returns_png_bytes(self, client_with_transport) -> None:
        client, handlers = client_with_transport
        handlers.append((
            "POST", "/computer/screenshot", 200,
            PNG_MAGIC + b"fakepngbody",
        ))
        result = await client.screenshot()
        assert result["png_bytes"].startswith(PNG_MAGIC)
        assert "annotations" not in result  # No annotate -> no annotations.

    async def test_screenshot_with_annotate_runs_overlay_then_clears(
        self, client_with_transport
    ) -> None:
        client, _ = client_with_transport
        # Pre-seed snapshot cache so annotate doesn't have to call get_state first.
        client._snapshot_cache["@e1"] = {"x": 100, "y": 50, "role": "button", "name": "Go"}
        client._snapshot_cache["@e2"] = {"x": 200, "y": 50, "role": "link", "name": "Help"}

        seen_paths: list[str] = []

        class TracingTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
                seen_paths.append(request.url.path)
                if request.url.path == "/computer/screenshot":
                    return httpx.Response(200, content=PNG_MAGIC + b"img")
                if request.url.path == "/playwright/execute":
                    return httpx.Response(200, json={"success": True, "result": True})
                return httpx.Response(404)

        client._http = httpx.AsyncClient(base_url=client.rest_url, transport=TracingTransport())
        result = await client.screenshot(annotate=True)

        # Two playwright/execute calls (overlay inject + remove) and one screenshot.
        assert seen_paths.count("/playwright/execute") == 2
        assert seen_paths.count("/computer/screenshot") == 1
        # Annotations correlate @eN with numbered overlays.
        assert result["annotations"] == [
            {"ref": "@e1", "label": 1, "role": "button", "name": "Go"},
            {"ref": "@e2", "label": 2, "role": "link", "name": "Help"},
        ]
```

- [ ] **Step 2: Run** — 2 FAIL.

- [ ] **Step 3: Implement** — append:

```python
    # ------------------------------------------------------------------
    # Screenshot
    # ------------------------------------------------------------------

    async def screenshot(
        self,
        *,
        region: dict[str, int] | None = None,
        annotate: bool = False,
    ) -> dict[str, Any]:
        """Capture a PNG of the viewport (or *region*).

        When *annotate* is True, draws numbered overlays on every cached
        ref BEFORE capturing and removes them after. Returns:

        - ``png_bytes``  the raw PNG bytes (caller stores as artifact).
        - ``annotations``  present only when annotate=True. List of
          ``{ref, label, role, name}`` correlating each numbered overlay
          with its ref.
        """
        annotations: list[dict[str, Any]] | None = None
        if annotate:
            if not self._snapshot_cache:
                # Caller forgot to refresh; do an interactive snapshot now.
                await self.get_state(interactive_only=True)
            annotations = self._build_annotations()
            await self._inject_overlay(annotations)

        body = {} if region is None else {"region": region}
        resp = await self._http.post("/computer/screenshot", json=body)
        resp.raise_for_status()
        png_bytes = resp.content

        if annotate:
            await self._remove_overlay()

        result: dict[str, Any] = {"png_bytes": png_bytes}
        if annotations is not None:
            result["annotations"] = annotations
        return result

    def _build_annotations(self) -> list[dict[str, Any]]:
        """Build the annotation list from the current snapshot cache."""
        out: list[dict[str, Any]] = []
        for label, (ref, entry) in enumerate(
            sorted(self._snapshot_cache.items(), key=lambda kv: int(kv[0][2:])),
            start=1,
        ):
            out.append({
                "ref": ref,
                "label": label,
                "role": entry.get("role", ""),
                "name": entry.get("name", ""),
            })
        return out

    async def _inject_overlay(self, annotations: list[dict[str, Any]]) -> None:
        """Inject a fixed-position canvas drawing numbered labels next to each ref."""
        coords_payload = [
            {"label": a["label"], **self._snapshot_cache[a["ref"]]} for a in annotations
        ]
        # Stage the data on window so the script body stays small.
        code = (
            f"window.__surogates_overlays = {coords_payload!r};\n"
            "const c = document.createElement('canvas');\n"
            "c.id = 'surogates-overlay'; c.style.cssText = "
            "'position:fixed;inset:0;pointer-events:none;z-index:2147483647';\n"
            "c.width = window.innerWidth; c.height = window.innerHeight;\n"
            "document.documentElement.appendChild(c);\n"
            "const g = c.getContext('2d');\n"
            "g.font = 'bold 14px sans-serif';\n"
            "for (const o of window.__surogates_overlays) {\n"
            "  g.fillStyle = 'rgba(255, 215, 0, 0.85)';\n"
            "  g.fillRect(o.x - 12, o.y - 10, 24, 20);\n"
            "  g.fillStyle = 'black';\n"
            "  g.textAlign = 'center'; g.textBaseline = 'middle';\n"
            "  g.fillText(String(o.label), o.x, o.y);\n"
            "}\n"
            "return true;\n"
        )
        await self._playwright_execute(f"await page.evaluate(async () => {{ {code} }});")

    async def _remove_overlay(self) -> None:
        await self._playwright_execute(
            "await page.evaluate(() => { "
            "const c = document.getElementById('surogates-overlay'); "
            "if (c) c.remove(); "
            "});"
        )
```

- [ ] **Step 4: Run** — all PASS (test counts test that "execute" is called twice — overlay inject + remove).

- [ ] **Step 5: Commit**

```bash
git add surogates/browser/client.py tests/test_browser_client.py
git commit -m "feat(browser): add screenshot with optional annotated overlays"
```

---

## Task 10: `BrowserRegistry` (Redis hash)

**Files:**
- Create: `surogates/browser/registry.py`
- Test: `tests/test_browser_registry.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_browser_registry.py`:

```python
"""Tests for surogates.browser.registry.BrowserRegistry."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from surogates.browser.registry import BrowserEntry, BrowserRegistry


class FakeRedis:
    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, bytes]] = {}

    async def hset(self, name: str, key: str, value: str) -> int:
        self.hashes.setdefault(name, {})
        existed = key in self.hashes[name]
        self.hashes[name][key] = value.encode() if isinstance(value, str) else value
        return 0 if existed else 1

    async def hget(self, name: str, key: str):
        return self.hashes.get(name, {}).get(key)

    async def hdel(self, name: str, key: str) -> int:
        if name in self.hashes and key in self.hashes[name]:
            del self.hashes[name][key]
            return 1
        return 0

    async def hkeys(self, name: str):
        return list(self.hashes.get(name, {}).keys())


class TestBrowserRegistry:
    async def test_set_and_get(self) -> None:
        reg = BrowserRegistry(FakeRedis())  # type: ignore[arg-type]
        entry = BrowserEntry(
            session_id="sess-1",
            org_id="org-1",
            user_id="user-1",
            rest_url="http://10.0.0.5:30000",
            cdp_url="ws://10.0.0.5:31000",
            live_view_url="ws://10.0.0.5:32000",
            provisioned_at=datetime(2026, 5, 10, 0, 0, tzinfo=timezone.utc),
        )
        await reg.set(entry)
        out = await reg.get("sess-1")
        assert out is not None
        assert out.session_id == "sess-1"
        assert out.rest_url == "http://10.0.0.5:30000"
        assert out.org_id == "org-1"

    async def test_get_missing(self) -> None:
        reg = BrowserRegistry(FakeRedis())  # type: ignore[arg-type]
        assert await reg.get("nope") is None

    async def test_delete_idempotent(self) -> None:
        reg = BrowserRegistry(FakeRedis())  # type: ignore[arg-type]
        await reg.delete("nope")  # No raise.
        entry = BrowserEntry(
            session_id="sess-1", org_id="o", user_id="u",
            rest_url="r", cdp_url="c", live_view_url="l",
            provisioned_at=datetime.now(timezone.utc),
        )
        await reg.set(entry)
        await reg.delete("sess-1")
        assert await reg.get("sess-1") is None

    async def test_persists_as_json(self) -> None:
        fake = FakeRedis()
        reg = BrowserRegistry(fake)  # type: ignore[arg-type]
        entry = BrowserEntry(
            session_id="sess-1", org_id="o", user_id="u",
            rest_url="r", cdp_url="c", live_view_url="l",
            provisioned_at=datetime(2026, 5, 10, tzinfo=timezone.utc),
        )
        await reg.set(entry)
        raw = fake.hashes["surogates:browser:registry"]["sess-1"]
        decoded = json.loads(raw.decode())
        assert decoded["session_id"] == "sess-1"
        assert decoded["provisioned_at"] == "2026-05-10T00:00:00+00:00"
```

- [ ] **Step 2: Run** — 4 FAIL.

- [ ] **Step 3: Implement**

`surogates/browser/registry.py`:

```python
"""Cross-process browser pod registry (Redis hash).

The worker writes pod metadata (REST URL, CDP URL, live-view URL) keyed
by session_id when a browser is provisioned. The API server reads the
same hash in Phase C to resolve where to proxy live-view traffic and
state queries — the worker's in-memory ``BrowserPool`` is not visible
to other pods.

The hash key is ``surogates:browser:registry``. Each session entry is
JSON-serialised so it survives Redis restarts and stays readable from
the redis-cli.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from redis.asyncio import Redis

logger = logging.getLogger(__name__)

REGISTRY_HASH_KEY = "surogates:browser:registry"


@dataclass(slots=True)
class BrowserEntry:
    """One row in the browser registry."""

    session_id: str
    org_id: str
    user_id: str
    rest_url: str
    cdp_url: str
    live_view_url: str
    provisioned_at: datetime

    def to_json(self) -> str:
        d = asdict(self)
        d["provisioned_at"] = self.provisioned_at.isoformat()
        return json.dumps(d)

    @classmethod
    def from_json(cls, raw: str | bytes) -> "BrowserEntry":
        if isinstance(raw, bytes):
            raw = raw.decode()
        d: dict[str, Any] = json.loads(raw)
        return cls(
            session_id=d["session_id"],
            org_id=d["org_id"],
            user_id=d["user_id"],
            rest_url=d["rest_url"],
            cdp_url=d["cdp_url"],
            live_view_url=d["live_view_url"],
            provisioned_at=datetime.fromisoformat(d["provisioned_at"]),
        )


class BrowserRegistry:
    """Async wrapper around the Redis hash that holds browser metadata."""

    def __init__(self, redis: "Redis") -> None:
        self._redis = redis

    async def set(self, entry: BrowserEntry) -> None:
        await self._redis.hset(REGISTRY_HASH_KEY, entry.session_id, entry.to_json())

    async def get(self, session_id: str) -> BrowserEntry | None:
        raw = await self._redis.hget(REGISTRY_HASH_KEY, session_id)
        if raw is None:
            return None
        return BrowserEntry.from_json(raw)

    async def delete(self, session_id: str) -> None:
        # Idempotent — no exception on missing key.
        await self._redis.hdel(REGISTRY_HASH_KEY, session_id)

    async def list_session_ids(self) -> list[str]:
        keys = await self._redis.hkeys(REGISTRY_HASH_KEY)
        return [k.decode() if isinstance(k, bytes) else k for k in keys]
```

- [ ] **Step 4: Run** — all PASS.

- [ ] **Step 5: Commit**

```bash
git add surogates/browser/registry.py tests/test_browser_registry.py
git commit -m "feat(browser): add BrowserRegistry for cross-process pod metadata"
```

---

## Task 11: `BrowserControlStore` (Redis-backed user-control flag)

**Files:**
- Create: `surogates/browser/control.py`
- Test: `tests/test_browser_control.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_browser_control.py`:

```python
"""Tests for surogates.browser.control.BrowserControlStore."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from surogates.browser.control import (
    AcquireOutcome,
    BrowserControlStore,
    ControlEntry,
)


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    async def get(self, key: str):
        return self.values.get(key)

    async def set(
        self,
        key: str,
        value: str | bytes,
        *,
        nx: bool = False,
        ex: int | None = None,
    ) -> bool:
        if nx and key in self.values:
            return False
        self.values[key] = value.encode() if isinstance(value, str) else value
        return True

    async def delete(self, *keys: str) -> int:
        n = 0
        for k in keys:
            if k in self.values:
                del self.values[k]
                n += 1
        return n


class TestAcquire:
    async def test_acquire_when_unheld(self) -> None:
        store = BrowserControlStore(FakeRedis())  # type: ignore[arg-type]
        outcome, entry = await store.acquire("sess-1", "user-A")
        assert outcome == AcquireOutcome.GRANTED
        assert entry.owner_user_id == "user-A"

    async def test_acquire_same_user_refreshes(self) -> None:
        store = BrowserControlStore(FakeRedis())  # type: ignore[arg-type]
        await store.acquire("sess-1", "user-A")
        outcome, entry = await store.acquire("sess-1", "user-A")
        assert outcome == AcquireOutcome.REFRESHED
        assert entry.owner_user_id == "user-A"

    async def test_acquire_different_user_conflicts(self) -> None:
        store = BrowserControlStore(FakeRedis())  # type: ignore[arg-type]
        await store.acquire("sess-1", "user-A")
        outcome, entry = await store.acquire("sess-1", "user-B")
        assert outcome == AcquireOutcome.CONFLICT
        # On conflict, returned entry is the existing holder.
        assert entry.owner_user_id == "user-A"


class TestRelease:
    async def test_release_owner(self) -> None:
        store = BrowserControlStore(FakeRedis())  # type: ignore[arg-type]
        await store.acquire("sess-1", "user-A")
        ok = await store.release("sess-1", "user-A")
        assert ok is True
        assert await store.get("sess-1") is None

    async def test_release_non_owner_rejected(self) -> None:
        store = BrowserControlStore(FakeRedis())  # type: ignore[arg-type]
        await store.acquire("sess-1", "user-A")
        ok = await store.release("sess-1", "user-B")
        assert ok is False
        # Still held by A.
        entry = await store.get("sess-1")
        assert entry is not None
        assert entry.owner_user_id == "user-A"

    async def test_release_unheld_is_noop(self) -> None:
        store = BrowserControlStore(FakeRedis())  # type: ignore[arg-type]
        ok = await store.release("sess-9", "user-X")
        assert ok is False


class TestGet:
    async def test_get_missing(self) -> None:
        store = BrowserControlStore(FakeRedis())  # type: ignore[arg-type]
        assert await store.get("nope") is None

    async def test_held_by_returns_user(self) -> None:
        store = BrowserControlStore(FakeRedis())  # type: ignore[arg-type]
        assert await store.held_by("sess-1") is None
        await store.acquire("sess-1", "user-A")
        assert await store.held_by("sess-1") == "user-A"
```

- [ ] **Step 2: Run** — 8 FAIL.

- [ ] **Step 3: Implement**

`surogates/browser/control.py`:

```python
"""Cross-process user-control flag for the browser live view.

When a user clicks "Take control" in the SPA, the API server records
ownership here. Every ``browser_*`` tool checks this store before
acting and short-circuits with ``paused_by_user`` while a user holds
control.

This is shared runtime state, NOT session.config. The harness reads
session.config once per wake into an in-memory snapshot, so a flag
flip mid-wake would be invisible. Storing the flag in Redis with a
per-call read lets the worker honor user takeover within the same
wake.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from redis.asyncio import Redis


_KEY_PREFIX = "surogates:browser:control:"


def _key(session_id: str) -> str:
    return f"{_KEY_PREFIX}{session_id}"


@dataclass(slots=True)
class ControlEntry:
    owner_user_id: str
    acquired_at: datetime

    def to_json(self) -> str:
        return json.dumps({
            "owner_user_id": self.owner_user_id,
            "acquired_at": self.acquired_at.isoformat(),
        })

    @classmethod
    def from_json(cls, raw: str | bytes) -> "ControlEntry":
        if isinstance(raw, bytes):
            raw = raw.decode()
        d = json.loads(raw)
        return cls(
            owner_user_id=d["owner_user_id"],
            acquired_at=datetime.fromisoformat(d["acquired_at"]),
        )


class AcquireOutcome(str, Enum):
    GRANTED = "granted"
    REFRESHED = "refreshed"
    CONFLICT = "conflict"


class BrowserControlStore:
    def __init__(self, redis: "Redis", *, ttl_seconds: int = 60) -> None:
        self._redis = redis
        self._ttl_seconds = ttl_seconds

    async def acquire(
        self,
        session_id: str,
        user_id: str,
    ) -> tuple[AcquireOutcome, ControlEntry]:
        """Try to grant control of *session_id* to *user_id*.

        Three outcomes (see spec §7.3):
        - **GRANTED**: was unheld; *user_id* now owns it.
        - **REFRESHED**: same user already held it; ``acquired_at`` updated.
        - **CONFLICT**: different user holds it; returned entry is the holder.
        """
        entry = ControlEntry(
            owner_user_id=user_id,
            acquired_at=datetime.now(timezone.utc),
        )
        key = _key(session_id)
        # Atomic first acquisition. Without NX, two users can race through
        # get()+set() and both believe they hold control. The TTL prevents a
        # crashed browser-control client from pausing the agent indefinitely.
        acquired = await self._redis.set(
            key,
            entry.to_json(),
            nx=True,
            ex=self._ttl_seconds,
        )
        if acquired:
            return AcquireOutcome.GRANTED, entry

        existing = await self.get(session_id)
        if existing is None:
            # Key disappeared between SET NX and GET (e.g. manual release).
            # Retry once recursively; this path is rare and bounded.
            return await self.acquire(session_id, user_id)
        if existing.owner_user_id != user_id:
            return AcquireOutcome.CONFLICT, existing

        await self._redis.set(key, entry.to_json(), ex=self._ttl_seconds)
        return AcquireOutcome.REFRESHED, entry

    async def release(self, session_id: str, user_id: str) -> bool:
        """Release control held by *user_id*. Returns True iff a release happened."""
        entry = await self.get(session_id)
        if entry is None or entry.owner_user_id != user_id:
            return False
        await self._redis.delete(_key(session_id))
        return True

    async def get(self, session_id: str) -> ControlEntry | None:
        raw = await self._redis.get(_key(session_id))
        if raw is None:
            return None
        return ControlEntry.from_json(raw)

    async def held_by(self, session_id: str) -> str | None:
        entry = await self.get(session_id)
        return entry.owner_user_id if entry else None
```

- [ ] **Step 4: Run** — all PASS.

- [ ] **Step 5: Commit**

```bash
git add surogates/browser/control.py tests/test_browser_control.py
git commit -m "feat(browser): add BrowserControlStore with acquire conflict semantics"
```

---

## Task 12: `ProcessBrowserBackend` — provision + status + destroy

**Files:**
- Create: `surogates/browser/process.py`
- Test: `tests/test_browser_process.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_browser_process.py`:

```python
"""Tests for surogates.browser.process.ProcessBrowserBackend.

Uses a mock subprocess driver since CI doesn't run Docker. The actual
docker-driven path is exercised in tests/integration/test_browser_e2e.py
behind the ``--browser-e2e`` marker.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from surogates.browser.base import BrowserSpec, BrowserStatus
from surogates.browser.process import ProcessBrowserBackend


class FakeDocker:
    """Pretends to run docker. Records every command, fakes container ids."""

    def __init__(self, ready_after_calls: int = 1) -> None:
        self.calls: list[list[str]] = []
        self.ready_after_calls = ready_after_calls
        self._spec_json_polls = 0
        self._containers: dict[str, dict[str, Any]] = {}

    async def run(self, args: list[str]) -> tuple[int, bytes, bytes]:
        self.calls.append(args)
        # `docker run -d ...`
        if args[:2] == ["run", "-d"]:
            cid = f"cid-{len(self._containers)+1}"
            self._containers[cid] = {"running": True}
            return 0, cid.encode() + b"\n", b""
        # `docker inspect --format ...`
        if args[0] == "inspect":
            cid = args[-1]
            running = self._containers.get(cid, {}).get("running", False)
            return 0, (b"running" if running else b"exited") + b"\n", b""
        # `docker stop` / `docker rm`
        if args[0] in {"stop", "rm"}:
            cid = args[-1]
            if cid in self._containers:
                self._containers[cid]["running"] = False
                if args[0] == "rm":
                    del self._containers[cid]
            return 0, b"", b""
        return 0, b"", b""


@pytest.fixture()
def fake_spec_json_transport():
    """Mock the kernel-images REST so /spec.json returns 200 immediately."""

    class T(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            if request.url.path == "/spec.json":
                return httpx.Response(200, json={"ready": True})
            return httpx.Response(404)

    return T()


class TestProvision:
    async def test_provision_runs_docker_and_returns_endpoint(
        self, fake_spec_json_transport
    ) -> None:
        docker = FakeDocker()
        backend = ProcessBrowserBackend(
            image="kernel-test:1",
            rest_port_base=30000,
            cdp_port_base=31000,
            live_view_port_base=32000,
            docker=docker,
            httpx_transport=fake_spec_json_transport,
        )
        spec = BrowserSpec(image="kernel-test:1", pod_ready_timeout=5)
        bid, endpoint = await backend.provision(spec)
        assert bid == "cid-1"
        # First container takes the base ports.
        assert endpoint.rest_url == "http://127.0.0.1:30000"
        assert endpoint.cdp_url == "ws://127.0.0.1:31000"
        assert endpoint.live_view_url == "ws://127.0.0.1:32000"
        # docker run was invoked.
        run_call = docker.calls[0]
        assert run_call[0] == "run"
        assert "-d" in run_call
        # Port mappings include the three target ports.
        joined = " ".join(run_call)
        assert "30000:10001" in joined
        assert "31000:9222" in joined
        assert "32000:6080" in joined
        assert run_call[-1] == "kernel-test:1"

    async def test_provision_increments_port_for_second_browser(
        self, fake_spec_json_transport
    ) -> None:
        docker = FakeDocker()
        backend = ProcessBrowserBackend(
            image="i", rest_port_base=30000, cdp_port_base=31000,
            live_view_port_base=32000,
            docker=docker, httpx_transport=fake_spec_json_transport,
        )
        b1, ep1 = await backend.provision(BrowserSpec())
        b2, ep2 = await backend.provision(BrowserSpec())
        assert ep1.rest_url.endswith(":30000")
        assert ep2.rest_url.endswith(":30001")


class TestStatus:
    async def test_status_running(self, fake_spec_json_transport) -> None:
        docker = FakeDocker()
        backend = ProcessBrowserBackend(
            image="i", rest_port_base=30000, cdp_port_base=31000,
            live_view_port_base=32000,
            docker=docker, httpx_transport=fake_spec_json_transport,
        )
        bid, _ = await backend.provision(BrowserSpec())
        assert await backend.status(bid) == BrowserStatus.RUNNING

    async def test_status_terminated_after_destroy(self, fake_spec_json_transport) -> None:
        docker = FakeDocker()
        backend = ProcessBrowserBackend(
            image="i", rest_port_base=30000, cdp_port_base=31000,
            live_view_port_base=32000,
            docker=docker, httpx_transport=fake_spec_json_transport,
        )
        bid, _ = await backend.provision(BrowserSpec())
        await backend.destroy(bid)
        assert await backend.status(bid) == BrowserStatus.TERMINATED


class TestDestroy:
    async def test_destroy_runs_stop_and_rm(self, fake_spec_json_transport) -> None:
        docker = FakeDocker()
        backend = ProcessBrowserBackend(
            image="i", rest_port_base=30000, cdp_port_base=31000,
            live_view_port_base=32000,
            docker=docker, httpx_transport=fake_spec_json_transport,
        )
        bid, _ = await backend.provision(BrowserSpec())
        await backend.destroy(bid)
        verbs = [c[0] for c in docker.calls]
        assert "stop" in verbs
        assert "rm" in verbs

    async def test_destroy_unknown_is_noop(self, fake_spec_json_transport) -> None:
        docker = FakeDocker()
        backend = ProcessBrowserBackend(
            image="i", rest_port_base=30000, cdp_port_base=31000,
            live_view_port_base=32000,
            docker=docker, httpx_transport=fake_spec_json_transport,
        )
        # Does not raise.
        await backend.destroy("never-provisioned")
```

- [ ] **Step 2: Run** — 6 FAIL.

- [ ] **Step 3: Implement**

`surogates/browser/process.py`:

```python
"""Local-development browser backend: kernel-images via ``docker run``.

The K8s backend in Phase B replaces this for production. The two share
the :class:`~surogates.browser.base.BrowserBackend` protocol so the
worker only swaps a backend instance.

Port allocation: each provisioned container takes one slot from each
of three configurable port pools (REST, CDP, live-view). Pools are
zero-indexed offsets from the bases in ``BrowserSettings``. There is
no recycling in v1 — the pool grows monotonically. Adequate for dev.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from surogates.browser.base import (
    BrowserBackend,
    BrowserEndpoint,
    BrowserSpec,
    BrowserStatus,
    BrowserUnavailableError,
)

logger = logging.getLogger(__name__)


class _DockerDriver(Protocol):
    """The shape ProcessBrowserBackend uses to talk to docker.

    Lets tests substitute a fake driver. The default driver shells out
    to ``docker``; a fake captures ``args`` instead.
    """

    async def run(self, args: list[str]) -> tuple[int, bytes, bytes]: ...


class _RealDocker:
    async def run(self, args: list[str]) -> tuple[int, bytes, bytes]:
        proc = await asyncio.create_subprocess_exec(
            "docker", *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        return proc.returncode or 0, out, err


@dataclass
class _Entry:
    container_id: str
    endpoint: BrowserEndpoint
    rest_port: int
    cdp_port: int
    live_view_port: int


class ProcessBrowserBackend:
    """Runs kernel-images locally as Docker containers."""

    def __init__(
        self,
        *,
        image: str,
        rest_port_base: int,
        cdp_port_base: int,
        live_view_port_base: int,
        docker: _DockerDriver | None = None,
        httpx_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._image = image
        self._rest_port_base = rest_port_base
        self._cdp_port_base = cdp_port_base
        self._live_view_port_base = live_view_port_base
        self._docker = docker or _RealDocker()
        self._transport = httpx_transport
        self._entries: dict[str, _Entry] = {}
        self._next_offset = 0
        self._lock = asyncio.Lock()

    async def provision(self, spec: BrowserSpec) -> tuple[str, BrowserEndpoint]:
        async with self._lock:
            offset = self._next_offset
            self._next_offset += 1

        rest_port = self._rest_port_base + offset
        cdp_port = self._cdp_port_base + offset
        live_view_port = self._live_view_port_base + offset
        image = spec.image or self._image

        args = [
            "run", "-d", "--rm",
            "-p", f"{rest_port}:10001",
            "-p", f"{cdp_port}:9222",
            "-p", f"{live_view_port}:6080",
            "--shm-size", "2g",
        ]
        for k, v in spec.env.items():
            args.extend(["-e", f"{k}={v}"])
        args.append(image)

        rc, stdout, stderr = await self._docker.run(args)
        if rc != 0:
            raise BrowserUnavailableError(
                f"docker run failed (exit {rc}): {stderr.decode(errors='replace')}",
                classification="docker",
            )
        container_id = stdout.decode().strip().split("\n")[0]

        endpoint = BrowserEndpoint(
            rest_url=f"http://127.0.0.1:{rest_port}",
            cdp_url=f"ws://127.0.0.1:{cdp_port}",
            live_view_url=f"ws://127.0.0.1:{live_view_port}",
        )

        try:
            await self._wait_ready(endpoint, spec.pod_ready_timeout)
        except Exception:
            # Readiness failure after docker run would otherwise leak the
            # container and its allocated ports.
            await self._docker.run(["stop", container_id])
            await self._docker.run(["rm", container_id])
            raise

        self._entries[container_id] = _Entry(
            container_id=container_id,
            endpoint=endpoint,
            rest_port=rest_port,
            cdp_port=cdp_port,
            live_view_port=live_view_port,
        )
        logger.info("Provisioned browser container %s on REST :%d", container_id, rest_port)
        return container_id, endpoint

    async def status(self, browser_id: str) -> BrowserStatus:
        if browser_id not in self._entries:
            return BrowserStatus.TERMINATED
        rc, stdout, _ = await self._docker.run([
            "inspect", "--format", "{{.State.Status}}", browser_id,
        ])
        if rc != 0:
            return BrowserStatus.FAILED
        state = stdout.decode().strip()
        if state == "running":
            return BrowserStatus.RUNNING
        if state in {"created", "restarting"}:
            return BrowserStatus.PENDING
        if state in {"exited", "dead", "removing"}:
            return BrowserStatus.TERMINATED
        return BrowserStatus.FAILED

    async def destroy(self, browser_id: str) -> None:
        if browser_id not in self._entries:
            return
        await self._docker.run(["stop", browser_id])
        await self._docker.run(["rm", browser_id])
        del self._entries[browser_id]
        logger.info("Destroyed browser container %s", browser_id)

    async def _wait_ready(self, endpoint: BrowserEndpoint, timeout: int) -> None:
        """Poll /spec.json until the container responds (or timeout)."""
        deadline = asyncio.get_running_loop().time() + timeout
        last_err: Exception | None = None
        async with httpx.AsyncClient(
            base_url=endpoint.rest_url,
            transport=self._transport,
            timeout=2.0,
        ) as http:
            while asyncio.get_running_loop().time() < deadline:
                try:
                    resp = await http.get("/spec.json")
                    if resp.status_code == 200:
                        return
                except Exception as exc:  # noqa: BLE001 — exhaustive retry
                    last_err = exc
                await asyncio.sleep(0.5)
        raise BrowserUnavailableError(
            f"Browser did not become ready within {timeout}s "
            f"({type(last_err).__name__ if last_err else 'no_response'})",
            classification="readiness",
        )
```

- [ ] **Step 4: Add a readiness-failure cleanup test** — append to `tests/test_browser_process.py`:

```python
class NeverReadyTransport(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"ready": False})


async def test_provision_cleans_up_container_when_readiness_times_out() -> None:
    docker = FakeDocker()
    backend = ProcessBrowserBackend(
        image="i", rest_port_base=30000, cdp_port_base=31000,
        live_view_port_base=32000,
        docker=docker, httpx_transport=NeverReadyTransport(),
    )
    with pytest.raises(Exception):
        await backend.provision(BrowserSpec(pod_ready_timeout=0))

    verbs = [c[0] for c in docker.calls]
    assert "stop" in verbs
    assert "rm" in verbs
```

- [ ] **Step 5: Run** — all PASS.

- [ ] **Step 6: Commit**

```bash
git add surogates/browser/process.py tests/test_browser_process.py
git commit -m "feat(browser): add ProcessBrowserBackend (docker run for dev)"
```

---

## Task 13: `BrowserPool` — ensure / destroy / event emission

**Files:**
- Create: `surogates/browser/pool.py`
- Test: `tests/test_browser_pool.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_browser_pool.py`:

```python
"""Tests for surogates.browser.pool.BrowserPool."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from surogates.browser.base import (
    BrowserEndpoint,
    BrowserSpec,
    BrowserStatus,
    BrowserUnavailableError,
)
from surogates.browser.pool import BrowserPool, EnsureResult
from surogates.browser.registry import BrowserEntry, BrowserRegistry


class FakeBackend:
    def __init__(self) -> None:
        self.provisions = 0
        self.destroys: list[str] = []
        self.status_overrides: dict[str, BrowserStatus] = {}
        self.fail_provision: BrowserUnavailableError | None = None

    async def provision(self, spec: BrowserSpec) -> tuple[str, BrowserEndpoint]:
        if self.fail_provision is not None:
            raise self.fail_provision
        self.provisions += 1
        bid = f"b{self.provisions}"
        return bid, BrowserEndpoint(
            rest_url=f"http://x:{30000 + self.provisions}",
            cdp_url=f"ws://x:{31000 + self.provisions}",
            live_view_url=f"ws://x:{32000 + self.provisions}",
        )

    async def status(self, browser_id: str) -> BrowserStatus:
        return self.status_overrides.get(browser_id, BrowserStatus.RUNNING)

    async def destroy(self, browser_id: str) -> None:
        self.destroys.append(browser_id)


class FakeRegistry:
    def __init__(self) -> None:
        self.entries: dict[str, BrowserEntry] = {}

    async def set(self, entry: BrowserEntry) -> None:
        self.entries[entry.session_id] = entry

    async def get(self, session_id: str) -> BrowserEntry | None:
        return self.entries.get(session_id)

    async def delete(self, session_id: str) -> None:
        self.entries.pop(session_id, None)


class TestEnsure:
    async def test_first_call_provisions(self) -> None:
        backend = FakeBackend()
        registry = FakeRegistry()
        pool = BrowserPool(backend=backend, registry=registry)  # type: ignore[arg-type]

        result = await pool.ensure(
            session_id="sess-1", org_id="o", user_id="u", spec=BrowserSpec(),
        )
        assert isinstance(result, EnsureResult)
        assert result.newly_provisioned is True
        assert result.endpoint.rest_url == "http://x:30001"
        assert backend.provisions == 1
        # Registry is updated.
        assert "sess-1" in registry.entries

    async def test_second_call_reuses(self) -> None:
        backend = FakeBackend()
        registry = FakeRegistry()
        pool = BrowserPool(backend=backend, registry=registry)  # type: ignore[arg-type]

        await pool.ensure("sess-1", "o", "u", BrowserSpec())
        result = await pool.ensure("sess-1", "o", "u", BrowserSpec())
        assert result.newly_provisioned is False
        assert backend.provisions == 1

    async def test_stale_status_reprovisions(self) -> None:
        backend = FakeBackend()
        registry = FakeRegistry()
        pool = BrowserPool(backend=backend, registry=registry)  # type: ignore[arg-type]

        await pool.ensure("sess-1", "o", "u", BrowserSpec())
        # Mark the first browser as failed.
        backend.status_overrides["b1"] = BrowserStatus.FAILED
        result = await pool.ensure("sess-1", "o", "u", BrowserSpec())
        assert result.newly_provisioned is True
        assert backend.provisions == 2
        assert backend.destroys == ["b1"]

    async def test_provision_failure_propagates(self) -> None:
        backend = FakeBackend()
        backend.fail_provision = BrowserUnavailableError("docker pull failed")
        pool = BrowserPool(backend=backend, registry=FakeRegistry())  # type: ignore[arg-type]
        with pytest.raises(BrowserUnavailableError):
            await pool.ensure("sess-1", "o", "u", BrowserSpec())


class TestDestroy:
    async def test_destroy_for_session(self) -> None:
        backend = FakeBackend()
        registry = FakeRegistry()
        pool = BrowserPool(backend=backend, registry=registry)  # type: ignore[arg-type]
        await pool.ensure("sess-1", "o", "u", BrowserSpec())

        await pool.destroy_for_session("sess-1")
        assert backend.destroys == ["b1"]
        assert "sess-1" not in registry.entries

    async def test_destroy_for_unknown_session_is_noop(self) -> None:
        pool = BrowserPool(backend=FakeBackend(), registry=FakeRegistry())  # type: ignore[arg-type]
        await pool.destroy_for_session("nope")  # no raise

    async def test_destroy_all(self) -> None:
        backend = FakeBackend()
        pool = BrowserPool(backend=backend, registry=FakeRegistry())  # type: ignore[arg-type]
        await pool.ensure("sess-1", "o", "u", BrowserSpec())
        await pool.ensure("sess-2", "o", "u", BrowserSpec())
        await pool.destroy_all()
        assert sorted(backend.destroys) == ["b1", "b2"]


class TestEvents:
    async def test_ensure_emits_browser_provisioned_via_callback(self) -> None:
        events: list[tuple[str, dict]] = []

        async def emitter(session_id: str, event_type: str, data: dict) -> None:
            events.append((event_type, data))

        backend = FakeBackend()
        pool = BrowserPool(
            backend=backend,
            registry=FakeRegistry(),  # type: ignore[arg-type]
            event_emitter=emitter,
        )
        await pool.ensure("sess-1", "o", "u", BrowserSpec())
        # Re-ensure: no second event.
        await pool.ensure("sess-1", "o", "u", BrowserSpec())

        types = [t for t, _ in events]
        assert types == ["browser.provisioned"]
        assert events[0][1]["session_id"] == "sess-1"
        assert events[0][1]["browser_id"] == "b1"

    async def test_destroy_emits_browser_destroyed(self) -> None:
        events: list[tuple[str, dict]] = []

        async def emitter(session_id: str, event_type: str, data: dict) -> None:
            events.append((event_type, data))

        backend = FakeBackend()
        pool = BrowserPool(
            backend=backend,
            registry=FakeRegistry(),  # type: ignore[arg-type]
            event_emitter=emitter,
        )
        await pool.ensure("sess-1", "o", "u", BrowserSpec())
        await pool.destroy_for_session("sess-1")

        types = [t for t, _ in events]
        assert "browser.destroyed" in types
```

- [ ] **Step 2: Run** — 8 FAIL.

- [ ] **Step 3: Implement**

`surogates/browser/pool.py`:

```python
"""Session-scoped browser pool.

Mirrors :class:`surogates.sandbox.pool.SandboxPool` for browser pods.
Owns the worker-local ``session_id → browser_id`` map, mirrors
metadata into the cross-process :class:`BrowserRegistry`, and emits
``browser.provisioned`` / ``browser.destroyed`` events through an
injected callback so the pool stays decoupled from
:class:`SessionStore`.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from surogates.browser.base import (
    BrowserBackend,
    BrowserEndpoint,
    BrowserSpec,
    BrowserStatus,
)
from surogates.browser.registry import BrowserEntry, BrowserRegistry
from surogates.session.events import EventType

logger = logging.getLogger(__name__)


# Async event emitter: (session_id, event_type_value, data) -> None.
EventEmitter = Callable[[str, str, dict[str, Any]], Awaitable[None]]


@dataclass(slots=True)
class EnsureResult:
    browser_id: str
    endpoint: BrowserEndpoint
    newly_provisioned: bool
    snapshot_cache: dict[str, dict[str, Any]]


@dataclass(slots=True)
class _Slot:
    browser_id: str
    endpoint: BrowserEndpoint
    snapshot_cache: dict[str, dict[str, Any]]


class BrowserPool:
    def __init__(
        self,
        *,
        backend: BrowserBackend,
        registry: BrowserRegistry,
        event_emitter: EventEmitter | None = None,
    ) -> None:
        self._backend = backend
        self._registry = registry
        self._emit = event_emitter
        self._mapping: dict[str, _Slot] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._global_lock = asyncio.Lock()

    async def ensure(
        self,
        session_id: str,
        org_id: str,
        user_id: str,
        spec: BrowserSpec,
    ) -> EnsureResult:
        lock = await self._session_lock(session_id)
        async with lock:
            slot = self._mapping.get(session_id)
            if slot is not None:
                status = await self._backend.status(slot.browser_id)
                if status == BrowserStatus.RUNNING:
                    return EnsureResult(
                        browser_id=slot.browser_id,
                        endpoint=slot.endpoint,
                        newly_provisioned=False,
                        snapshot_cache=slot.snapshot_cache,
                    )
                logger.warning(
                    "Browser %s for session %s is %s; reprovisioning",
                    slot.browser_id, session_id, status.value,
                )
                await self._backend.destroy(slot.browser_id)
                self._mapping.pop(session_id, None)
                await self._registry.delete(session_id)

            browser_id, endpoint = await self._backend.provision(spec)
            slot = _Slot(
                browser_id=browser_id,
                endpoint=endpoint,
                snapshot_cache={},
            )
            self._mapping[session_id] = slot
            await self._registry.set(BrowserEntry(
                session_id=session_id,
                org_id=org_id,
                user_id=user_id,
                rest_url=endpoint.rest_url,
                cdp_url=endpoint.cdp_url,
                live_view_url=endpoint.live_view_url,
                provisioned_at=datetime.now(timezone.utc),
            ))
            if self._emit is not None:
                await self._emit(session_id, EventType.BROWSER_PROVISIONED.value, {
                    "session_id": session_id,
                    "browser_id": browser_id,
                })
            return EnsureResult(
                browser_id=browser_id,
                endpoint=endpoint,
                newly_provisioned=True,
                snapshot_cache=slot.snapshot_cache,
            )

    async def destroy_for_session(self, session_id: str) -> None:
        lock = await self._session_lock(session_id)
        async with lock:
            slot = self._mapping.pop(session_id, None)
            if slot is None:
                return
            await self._backend.destroy(slot.browser_id)
            await self._registry.delete(session_id)
            if self._emit is not None:
                await self._emit(session_id, EventType.BROWSER_DESTROYED.value, {
                    "session_id": session_id,
                    "browser_id": slot.browser_id,
                })
        async with self._global_lock:
            self._locks.pop(session_id, None)

    async def destroy_all(self) -> None:
        async with self._global_lock:
            session_ids = list(self._mapping.keys())
        for sid in session_ids:
            try:
                await self.destroy_for_session(sid)
            except Exception:
                logger.exception("Error destroying browser for session %s", sid)

    def get_slot(self, session_id: str) -> _Slot | None:
        slot = self._mapping.get(session_id)
        return slot

    async def _session_lock(self, session_id: str) -> asyncio.Lock:
        async with self._global_lock:
            lock = self._locks.get(session_id)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[session_id] = lock
            return lock
```

- [ ] **Step 4: Run** — all PASS.

- [ ] **Step 5: Commit**

```bash
git add surogates/browser/pool.py tests/test_browser_pool.py
git commit -m "feat(browser): add BrowserPool with ensure/destroy lifecycle and event emission"
```

---

## Task 14: Browser tools — `browser_navigate`, `browser_get_state`, `browser_close` + control short-circuit

**Files:**
- Replace: `surogates/tools/builtin/browser.py` (was a stub)
- Test: `tests/test_browser_tools.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_browser_tools.py`:

```python
"""Tests for surogates.tools.builtin.browser handlers.

Handlers are tested via direct invocation (no router) using mock
BrowserPool / KernelBrowserClient / BrowserControlStore.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest

from surogates.browser.base import BrowserEndpoint, BrowserSpec
from surogates.browser.control import AcquireOutcome, ControlEntry
from surogates.browser.pool import EnsureResult


class FakePool:
    def __init__(self) -> None:
        self.ensures: list[tuple[str, str, str]] = []
        self.destroyed: list[str] = []
        self._fixed_endpoint = BrowserEndpoint(
            rest_url="http://browser:30000",
            cdp_url="ws://browser:31000",
            live_view_url="ws://browser:32000",
        )
        self.snapshot_cache: dict[str, dict[str, Any]] = {}

    async def ensure(self, session_id: str, org_id: str, user_id: str, spec: BrowserSpec) -> EnsureResult:
        self.ensures.append((session_id, org_id, user_id))
        return EnsureResult(
            browser_id="b1",
            endpoint=self._fixed_endpoint,
            newly_provisioned=True,
            snapshot_cache=self.snapshot_cache,
        )

    async def destroy_for_session(self, session_id: str) -> None:
        self.destroyed.append(session_id)


class FakeControlStore:
    def __init__(self, holder: str | None = None) -> None:
        self._holder = holder

    async def get(self, session_id: str) -> ControlEntry | None:
        if self._holder is None:
            return None
        return ControlEntry(
            owner_user_id=self._holder,
            acquired_at=datetime.now(timezone.utc),
        )


class FakeClient:
    def __init__(self) -> None:
        self.navigated_to: str | None = None
        self.closed = False

    async def navigate(self, url: str, **kwargs: Any) -> dict[str, Any]:
        self.navigated_to = url
        return {"url": url, "title": "Test Page"}

    async def get_state(self, **kwargs: Any) -> dict[str, Any]:
        return {"url": "http://example.com/", "title": "Test", "viewport": {"width": 1, "height": 1}, "tree": []}

    async def close(self) -> None:
        self.closed = True

    async def __aenter__(self) -> "FakeClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()


@pytest.fixture()
def tenant():
    from types import SimpleNamespace
    return SimpleNamespace(org_id=UUID("00000000-0000-0000-0000-000000000001"),
                           user_id=UUID("00000000-0000-0000-0000-000000000002"))


class TestNavigateHandler:
    async def test_navigates_via_pool(self, tenant) -> None:
        from surogates.tools.builtin.browser import _browser_navigate_handler

        pool = FakePool()
        client = FakeClient()
        control = FakeControlStore()
        sid = uuid4()

        result = await _browser_navigate_handler(
            {"url": "https://example.com"},
            tenant=tenant,
            session_id=sid,
            browser_pool=pool,
            browser_control=control,
            _client_factory=lambda endpoint: client,
        )
        body = json.loads(result)
        assert body["url"] == "https://example.com"
        assert body["title"] == "Test Page"
        assert pool.ensures == [(str(sid), str(tenant.org_id), str(tenant.user_id))]
        assert client.navigated_to == "https://example.com"

    async def test_short_circuits_when_user_in_control(self, tenant) -> None:
        from surogates.tools.builtin.browser import _browser_navigate_handler

        pool = FakePool()
        # Pre-acquired by some user.
        control = FakeControlStore(holder="other-user")
        result = await _browser_navigate_handler(
            {"url": "https://example.com"},
            tenant=tenant,
            session_id=uuid4(),
            browser_pool=pool,
            browser_control=control,
            _client_factory=lambda endpoint: FakeClient(),
        )
        body = json.loads(result)
        assert body["error"] == "paused_by_user"
        # Pool was NOT ensured.
        assert pool.ensures == []

    async def test_returns_unavailable_when_pool_missing(self, tenant) -> None:
        from surogates.tools.builtin.browser import _browser_navigate_handler

        result = await _browser_navigate_handler(
            {"url": "https://example.com"},
            tenant=tenant,
            session_id=uuid4(),
            browser_pool=None,
            browser_control=None,
        )
        body = json.loads(result)
        assert body["error"] == "browser_unavailable"


class TestGetStateHandler:
    async def test_returns_tree(self, tenant) -> None:
        from surogates.tools.builtin.browser import _browser_get_state_handler
        result = await _browser_get_state_handler(
            {"interactive_only": True},
            tenant=tenant, session_id=uuid4(),
            browser_pool=FakePool(),
            browser_control=FakeControlStore(),
            _client_factory=lambda endpoint: FakeClient(),
        )
        body = json.loads(result)
        assert "tree" in body
        assert body["url"] == "http://example.com/"


class TestCloseHandler:
    async def test_destroys_session_browser(self, tenant) -> None:
        from surogates.tools.builtin.browser import _browser_close_handler
        pool = FakePool()
        sid = uuid4()
        result = await _browser_close_handler(
            {},
            tenant=tenant, session_id=sid,
            browser_pool=pool, browser_control=FakeControlStore(),
        )
        body = json.loads(result)
        assert body["closed"] is True
        assert pool.destroyed == [str(sid)]
```

- [ ] **Step 2: Run** — fails (file doesn't have these handlers yet).

- [ ] **Step 3: Replace `surogates/tools/builtin/browser.py`** with the discrete tools (Task 14 covers navigate / get_state / close):

```python
"""Builtin agent-browser tools.

Replaces the prior placeholder. Each tool is harness-local — handlers
read the per-session ``BrowserPool`` and ``BrowserControlStore`` from
kwargs, short-circuit when a user has taken control, then talk to the
pod via :class:`KernelBrowserClient`.

See ``docs/superpowers/specs/2026-05-10-agent-browser-design.md`` §5.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable
from uuid import UUID

from surogates.browser.base import (
    BrowserEndpoint,
    BrowserSpec,
    BrowserUnavailableError,
    browser_unavailable_result,
)
from surogates.browser.client import KernelBrowserClient
from surogates.browser.control import BrowserControlStore
from surogates.browser.pool import BrowserPool
from surogates.tools.registry import ToolRegistry, ToolSchema

logger = logging.getLogger(__name__)


def _paused_by_user_result() -> str:
    return json.dumps({
        "error": "paused_by_user",
        "guidance": (
            "The user has taken control of the browser. Wait for them "
            "to finish before continuing — every browser_* tool will "
            "return this error until they release control."
        ),
    })


async def _resolve_session_browser(
    *,
    arguments: dict[str, Any],
    tenant: Any,
    session_id: UUID | str | None,
    browser_pool: BrowserPool | None,
    browser_control: BrowserControlStore | None,
    spec: BrowserSpec | None = None,
) -> tuple[str, BrowserEndpoint, dict[str, dict[str, Any]]] | str:
    """Common pre-flight: control short-circuit + pool.ensure().

    Returns either a ``(browser_id, endpoint, snapshot_cache)`` tuple ready
    for use, or a JSON error string the handler should return as-is. The
    cache is the per-session dict owned by BrowserPool.
    """
    if browser_pool is None or session_id is None:
        return browser_unavailable_result("browser pool not configured")

    sid_str = str(session_id)
    org_id = str(getattr(tenant, "org_id", "")) if tenant else ""
    user_id = str(getattr(tenant, "user_id", "")) if tenant else ""

    if browser_control is not None:
        held = await browser_control.get(sid_str)
        if held is not None:
            return _paused_by_user_result()

    try:
        result = await browser_pool.ensure(
            session_id=sid_str,
            org_id=org_id,
            user_id=user_id,
            spec=spec or BrowserSpec(),
        )
    except BrowserUnavailableError as exc:
        return browser_unavailable_result(exc.reason)
    return result.browser_id, result.endpoint, result.snapshot_cache


def _default_client_factory(
    endpoint: BrowserEndpoint,
    snapshot_cache: dict[str, dict[str, Any]],
) -> KernelBrowserClient:
    return KernelBrowserClient(
        rest_url=endpoint.rest_url,
        snapshot_cache=snapshot_cache,
    )


def _make_client(
    factory: Callable[..., Any],
    endpoint: BrowserEndpoint,
    snapshot_cache: dict[str, dict[str, Any]],
) -> Any:
    """Create a client while preserving one-arg test factories."""
    try:
        return factory(endpoint, snapshot_cache)
    except TypeError:
        return factory(endpoint)


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------

NAVIGATE_DESCRIPTION = (
    "Navigate the agent's browser to a URL. Provisions a fresh browser on "
    "the first browser_* call in the session. Returns the final URL after "
    "redirects and the page title."
)

NAVIGATE_SCHEMA = {
    "type": "object",
    "properties": {
        "url": {"type": "string", "description": "URL to navigate to."},
        "wait_until": {
            "type": "string",
            "enum": ["load", "domcontentloaded", "networkidle"],
            "default": "load",
        },
    },
    "required": ["url"],
    "additionalProperties": False,
}


async def _browser_navigate_handler(
    arguments: dict[str, Any],
    *,
    tenant: Any = None,
    session_id: UUID | str | None = None,
    browser_pool: BrowserPool | None = None,
    browser_control: BrowserControlStore | None = None,
    _client_factory: Callable[..., Any] = _default_client_factory,
    **_: Any,
) -> str:
    pre = await _resolve_session_browser(
        arguments=arguments,
        tenant=tenant,
        session_id=session_id,
        browser_pool=browser_pool,
        browser_control=browser_control,
    )
    if isinstance(pre, str):  # error JSON
        return pre
    _bid, endpoint, snapshot_cache = pre

    client = _make_client(_client_factory, endpoint, snapshot_cache)
    try:
        async with client:
            out = await client.navigate(
                arguments["url"],
                wait_until=arguments.get("wait_until", "load"),
            )
        return json.dumps({"url": out["url"], "title": out["title"]})
    except RuntimeError as exc:
        return json.dumps({"error": "navigate_failed", "detail": str(exc)})


GET_STATE_DESCRIPTION = (
    "Return the page's accessibility tree with @e1-style refs you can use in "
    "click/type/scroll. Optional filters: interactive_only (only buttons / "
    "links / inputs), compact (drop empty structural nodes), max_depth, "
    "selector (CSS scope)."
)

GET_STATE_SCHEMA = {
    "type": "object",
    "properties": {
        "interactive_only": {"type": "boolean", "default": False},
        "compact": {"type": "boolean", "default": False},
        "max_depth": {"type": "integer", "minimum": 0},
        "selector": {"type": "string"},
    },
    "additionalProperties": False,
}


async def _browser_get_state_handler(
    arguments: dict[str, Any],
    *,
    tenant: Any = None,
    session_id: UUID | str | None = None,
    browser_pool: BrowserPool | None = None,
    browser_control: BrowserControlStore | None = None,
    _client_factory: Callable[..., Any] = _default_client_factory,
    **_: Any,
) -> str:
    pre = await _resolve_session_browser(
        arguments=arguments, tenant=tenant, session_id=session_id,
        browser_pool=browser_pool, browser_control=browser_control,
    )
    if isinstance(pre, str):
        return pre
    _bid, endpoint, snapshot_cache = pre

    client = _make_client(_client_factory, endpoint, snapshot_cache)
    async with client:
        state = await client.get_state(
            interactive_only=arguments.get("interactive_only", False),
            compact=arguments.get("compact", False),
            max_depth=arguments.get("max_depth"),
            selector=arguments.get("selector"),
        )
    return json.dumps(state)


CLOSE_DESCRIPTION = (
    "Explicitly close the agent's browser. Use when you are done with the "
    "browser for this session; otherwise it tears down on session end. "
    "Returns immediately even if no browser was provisioned."
)

CLOSE_SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}


async def _browser_close_handler(
    arguments: dict[str, Any],
    *,
    tenant: Any = None,
    session_id: UUID | str | None = None,
    browser_pool: BrowserPool | None = None,
    browser_control: BrowserControlStore | None = None,
    **_: Any,
) -> str:
    if browser_pool is None or session_id is None:
        return json.dumps({"closed": False, "reason": "no_browser_pool"})

    sid = str(session_id)

    # Even close obeys the user-control short-circuit (spec §4.3).
    if browser_control is not None:
        held = await browser_control.get(sid)
        if held is not None:
            return _paused_by_user_result()

    await browser_pool.destroy_for_session(sid)
    return json.dumps({"closed": True})


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register(registry: ToolRegistry) -> None:
    registry.register(
        name="browser_navigate",
        schema=ToolSchema(
            name="browser_navigate",
            description=NAVIGATE_DESCRIPTION,
            parameters=NAVIGATE_SCHEMA,
        ),
        handler=_browser_navigate_handler,
        toolset="browser",
    )
    registry.register(
        name="browser_get_state",
        schema=ToolSchema(
            name="browser_get_state",
            description=GET_STATE_DESCRIPTION,
            parameters=GET_STATE_SCHEMA,
        ),
        handler=_browser_get_state_handler,
        toolset="browser",
    )
    registry.register(
        name="browser_close",
        schema=ToolSchema(
            name="browser_close",
            description=CLOSE_DESCRIPTION,
            parameters=CLOSE_SCHEMA,
        ),
        handler=_browser_close_handler,
        toolset="browser",
    )
```

- [ ] **Step 4: Run** — `pytest tests/test_browser_tools.py -v` → all PASS for the 5 tests in this task.

- [ ] **Step 5: Commit**

```bash
git add surogates/tools/builtin/browser.py tests/test_browser_tools.py
git commit -m "feat(browser): add navigate / get_state / close tools with control short-circuit"
```

---

## Task 15: Browser tools — `browser_click`, `browser_type`, `browser_press_key`, `browser_scroll`, `browser_drag`, `browser_wait`

**Files:**
- Modify: `surogates/tools/builtin/browser.py`
- Modify: `tests/test_browser_tools.py`

- [ ] **Step 1: Write the failing tests** — append to `tests/test_browser_tools.py`:

```python
class FakeClickClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    async def click_at(self, x: int, y: int, **kw: Any) -> None:
        self.calls.append(("click_at", (x, y), kw))

    async def click_ref(self, ref: str, **kw: Any) -> None:
        self.calls.append(("click_ref", (ref,), kw))

    async def type_text(self, text: str, **kw: Any) -> None:
        self.calls.append(("type_text", (text,), kw))

    async def type_into_ref(self, ref: str, text: str, **kw: Any) -> None:
        self.calls.append(("type_into_ref", (ref, text), kw))

    async def press_key(self, *keys: str, **kw: Any) -> None:
        self.calls.append(("press_key", keys, kw))

    async def scroll_at(self, x: int, y: int, **kw: Any) -> None:
        self.calls.append(("scroll_at", (x, y), kw))

    async def drag(self, path: list[tuple[int, int]], **kw: Any) -> None:
        self.calls.append(("drag", (tuple(path),), kw))

    async def wait(self, ms: int) -> None:
        self.calls.append(("wait", (ms,), {}))

    async def close(self) -> None: ...
    async def __aenter__(self) -> "FakeClickClient": return self
    async def __aexit__(self, *exc: Any) -> None: ...


class TestClickHandler:
    async def test_click_with_ref(self, tenant) -> None:
        from surogates.tools.builtin.browser import _browser_click_handler
        c = FakeClickClient()
        await _browser_click_handler(
            {"ref": "@e3"},
            tenant=tenant, session_id=uuid4(),
            browser_pool=FakePool(),
            browser_control=FakeControlStore(),
            _client_factory=lambda _: c,
        )
        assert c.calls[0][0] == "click_ref"
        assert c.calls[0][1] == ("@e3",)

    async def test_click_with_coords(self, tenant) -> None:
        from surogates.tools.builtin.browser import _browser_click_handler
        c = FakeClickClient()
        await _browser_click_handler(
            {"x": 100, "y": 200},
            tenant=tenant, session_id=uuid4(),
            browser_pool=FakePool(),
            browser_control=FakeControlStore(),
            _client_factory=lambda _: c,
        )
        assert c.calls[0][0] == "click_at"
        assert c.calls[0][1] == (100, 200)

    async def test_click_requires_ref_or_coords(self, tenant) -> None:
        from surogates.tools.builtin.browser import _browser_click_handler
        result = await _browser_click_handler(
            {},
            tenant=tenant, session_id=uuid4(),
            browser_pool=FakePool(),
            browser_control=FakeControlStore(),
            _client_factory=lambda _: FakeClickClient(),
        )
        body = json.loads(result)
        assert body["error"] == "invalid_arguments"


class TestTypeHandler:
    async def test_type_into_ref(self, tenant) -> None:
        from surogates.tools.builtin.browser import _browser_type_handler
        c = FakeClickClient()
        await _browser_type_handler(
            {"ref": "@e2", "text": "hello"},
            tenant=tenant, session_id=uuid4(),
            browser_pool=FakePool(),
            browser_control=FakeControlStore(),
            _client_factory=lambda _: c,
        )
        assert c.calls[0][0] == "type_into_ref"
        assert c.calls[0][1] == ("@e2", "hello")

    async def test_type_at_focus(self, tenant) -> None:
        from surogates.tools.builtin.browser import _browser_type_handler
        c = FakeClickClient()
        await _browser_type_handler(
            {"text": "fallback"},
            tenant=tenant, session_id=uuid4(),
            browser_pool=FakePool(),
            browser_control=FakeControlStore(),
            _client_factory=lambda _: c,
        )
        assert c.calls[0][0] == "type_text"


class TestPressKey:
    async def test_press_single(self, tenant) -> None:
        from surogates.tools.builtin.browser import _browser_press_key_handler
        c = FakeClickClient()
        await _browser_press_key_handler(
            {"keys": ["Enter"]},
            tenant=tenant, session_id=uuid4(),
            browser_pool=FakePool(),
            browser_control=FakeControlStore(),
            _client_factory=lambda _: c,
        )
        assert c.calls[0][0] == "press_key"
        assert c.calls[0][1] == ("Enter",)


class TestScrollDragWait:
    async def test_scroll(self, tenant) -> None:
        from surogates.tools.builtin.browser import _browser_scroll_handler
        c = FakeClickClient()
        await _browser_scroll_handler(
            {"x": 100, "y": 200, "delta_y": 300},
            tenant=tenant, session_id=uuid4(),
            browser_pool=FakePool(),
            browser_control=FakeControlStore(),
            _client_factory=lambda _: c,
        )
        assert c.calls[0][0] == "scroll_at"
        assert c.calls[0][2]["delta_y"] == 300

    async def test_drag(self, tenant) -> None:
        from surogates.tools.builtin.browser import _browser_drag_handler
        c = FakeClickClient()
        await _browser_drag_handler(
            {"path": [[10, 10], [200, 200]]},
            tenant=tenant, session_id=uuid4(),
            browser_pool=FakePool(),
            browser_control=FakeControlStore(),
            _client_factory=lambda _: c,
        )
        assert c.calls[0][0] == "drag"
        assert c.calls[0][1] == (((10, 10), (200, 200)),)

    async def test_wait_caps_at_30s(self, tenant) -> None:
        from surogates.tools.builtin.browser import _browser_wait_handler
        c = FakeClickClient()
        await _browser_wait_handler(
            {"ms": 999_999},  # absurd value
            tenant=tenant, session_id=uuid4(),
            browser_pool=FakePool(),
            browser_control=FakeControlStore(),
            _client_factory=lambda _: c,
        )
        assert c.calls[0][0] == "wait"
        # Capped at 30_000.
        assert c.calls[0][1] == (30_000,)
```

- [ ] **Step 2: Run** — 9 FAIL.

- [ ] **Step 3: Add the handlers** — append to `surogates/tools/builtin/browser.py`:

```python
# ---------------------------------------------------------------------------
# Click / type / keys / scroll / drag / wait
# ---------------------------------------------------------------------------

CLICK_DESCRIPTION = (
    "Click a page element. Provide either a ref from browser_get_state "
    "(`ref: '@e3'`) or absolute coordinates (`x`, `y`). Refs are preferred."
)
CLICK_SCHEMA = {
    "type": "object",
    "properties": {
        "ref": {"type": "string", "pattern": "^@e[0-9]+$"},
        "x": {"type": "integer"},
        "y": {"type": "integer"},
        "button": {"type": "string", "enum": ["left", "right", "middle"], "default": "left"},
        "click_type": {"type": "string", "enum": ["click", "down", "up"], "default": "click"},
        "num_clicks": {"type": "integer", "minimum": 1, "default": 1},
    },
    "additionalProperties": False,
}


async def _browser_click_handler(
    arguments: dict[str, Any],
    *,
    tenant: Any = None,
    session_id: UUID | str | None = None,
    browser_pool: BrowserPool | None = None,
    browser_control: BrowserControlStore | None = None,
    _client_factory: Callable[..., Any] = _default_client_factory,
    **_: Any,
) -> str:
    pre = await _resolve_session_browser(
        arguments=arguments, tenant=tenant, session_id=session_id,
        browser_pool=browser_pool, browser_control=browser_control,
    )
    if isinstance(pre, str):
        return pre
    _bid, endpoint, snapshot_cache = pre

    has_ref = "ref" in arguments
    has_coords = "x" in arguments and "y" in arguments
    if not has_ref and not has_coords:
        return json.dumps({"error": "invalid_arguments",
                           "detail": "click requires `ref` or `x`+`y`"})

    common = {
        "button": arguments.get("button", "left"),
        "click_type": arguments.get("click_type", "click"),
        "num_clicks": arguments.get("num_clicks", 1),
    }
    client = _make_client(_client_factory, endpoint, snapshot_cache)
    async with client:
        try:
            if has_ref:
                await client.click_ref(arguments["ref"], **common)
            else:
                await client.click_at(arguments["x"], arguments["y"], **common)
        except KeyError as exc:
            return json.dumps({"error": "unknown_ref", "detail": str(exc)})
    return json.dumps({"clicked": True})


TYPE_DESCRIPTION = (
    "Type text. Provide `ref` to focus a specific input first; otherwise "
    "types at the current focus. Cache invalidates after this call — call "
    "browser_get_state again before clicking new refs."
)
TYPE_SCHEMA = {
    "type": "object",
    "properties": {
        "text": {"type": "string"},
        "ref": {"type": "string", "pattern": "^@e[0-9]+$"},
        "delay_ms": {"type": "integer", "minimum": 0, "default": 0},
    },
    "required": ["text"],
    "additionalProperties": False,
}


async def _browser_type_handler(
    arguments: dict[str, Any],
    *,
    tenant: Any = None,
    session_id: UUID | str | None = None,
    browser_pool: BrowserPool | None = None,
    browser_control: BrowserControlStore | None = None,
    _client_factory: Callable[..., Any] = _default_client_factory,
    **_: Any,
) -> str:
    pre = await _resolve_session_browser(
        arguments=arguments, tenant=tenant, session_id=session_id,
        browser_pool=browser_pool, browser_control=browser_control,
    )
    if isinstance(pre, str):
        return pre
    _bid, endpoint, snapshot_cache = pre

    client = _make_client(_client_factory, endpoint, snapshot_cache)
    async with client:
        if "ref" in arguments:
            await client.type_into_ref(
                arguments["ref"], arguments["text"],
                delay_ms=arguments.get("delay_ms", 0),
            )
        else:
            await client.type_text(arguments["text"], delay_ms=arguments.get("delay_ms", 0))
    return json.dumps({"typed": True})


PRESS_KEY_DESCRIPTION = (
    "Press one or more keyboard keys. Each entry is an X11 keysym name "
    "(e.g. 'Return', 'Tab', 'Escape', 'F5') or a chord ('Ctrl+l', "
    "'Ctrl+Shift+Tab')."
)
PRESS_KEY_SCHEMA = {
    "type": "object",
    "properties": {
        "keys": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        "duration_ms": {"type": "integer", "minimum": 0, "default": 0},
    },
    "required": ["keys"],
    "additionalProperties": False,
}


async def _browser_press_key_handler(
    arguments: dict[str, Any],
    *,
    tenant: Any = None,
    session_id: UUID | str | None = None,
    browser_pool: BrowserPool | None = None,
    browser_control: BrowserControlStore | None = None,
    _client_factory: Callable[..., Any] = _default_client_factory,
    **_: Any,
) -> str:
    pre = await _resolve_session_browser(
        arguments=arguments, tenant=tenant, session_id=session_id,
        browser_pool=browser_pool, browser_control=browser_control,
    )
    if isinstance(pre, str):
        return pre
    _bid, endpoint, snapshot_cache = pre

    client = _make_client(_client_factory, endpoint, snapshot_cache)
    async with client:
        await client.press_key(*arguments["keys"],
                               duration_ms=arguments.get("duration_ms", 0))
    return json.dumps({"pressed": arguments["keys"]})


SCROLL_DESCRIPTION = (
    "Scroll the mouse wheel at coordinates by a delta in logical ticks "
    "(positive `delta_y` = scroll down)."
)
SCROLL_SCHEMA = {
    "type": "object",
    "properties": {
        "x": {"type": "integer"}, "y": {"type": "integer"},
        "delta_x": {"type": "integer", "default": 0},
        "delta_y": {"type": "integer", "default": 0},
    },
    "required": ["x", "y"],
    "additionalProperties": False,
}


async def _browser_scroll_handler(
    arguments: dict[str, Any],
    *,
    tenant: Any = None,
    session_id: UUID | str | None = None,
    browser_pool: BrowserPool | None = None,
    browser_control: BrowserControlStore | None = None,
    _client_factory: Callable[..., Any] = _default_client_factory,
    **_: Any,
) -> str:
    pre = await _resolve_session_browser(
        arguments=arguments, tenant=tenant, session_id=session_id,
        browser_pool=browser_pool, browser_control=browser_control,
    )
    if isinstance(pre, str):
        return pre
    _bid, endpoint, snapshot_cache = pre

    client = _make_client(_client_factory, endpoint, snapshot_cache)
    async with client:
        await client.scroll_at(
            arguments["x"], arguments["y"],
            delta_x=arguments.get("delta_x", 0),
            delta_y=arguments.get("delta_y", 0),
        )
    return json.dumps({"scrolled": True})


DRAG_DESCRIPTION = (
    "Drag the mouse along a path of [x, y] points. The path must contain "
    "at least two points. Useful for selecting text or sliders."
)
DRAG_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {
            "type": "array",
            "minItems": 2,
            "items": {
                "type": "array",
                "minItems": 2, "maxItems": 2,
                "items": {"type": "integer"},
            },
        },
        "button": {"type": "string", "enum": ["left", "middle", "right"], "default": "left"},
    },
    "required": ["path"],
    "additionalProperties": False,
}


async def _browser_drag_handler(
    arguments: dict[str, Any],
    *,
    tenant: Any = None,
    session_id: UUID | str | None = None,
    browser_pool: BrowserPool | None = None,
    browser_control: BrowserControlStore | None = None,
    _client_factory: Callable[..., Any] = _default_client_factory,
    **_: Any,
) -> str:
    pre = await _resolve_session_browser(
        arguments=arguments, tenant=tenant, session_id=session_id,
        browser_pool=browser_pool, browser_control=browser_control,
    )
    if isinstance(pre, str):
        return pre
    _bid, endpoint, snapshot_cache = pre

    path = [(int(p[0]), int(p[1])) for p in arguments["path"]]
    client = _make_client(_client_factory, endpoint, snapshot_cache)
    async with client:
        await client.drag(path, button=arguments.get("button", "left"))
    return json.dumps({"dragged": True, "points": len(path)})


WAIT_DESCRIPTION = (
    "Pause for N milliseconds — for animations or async loads. Capped at "
    "30,000 ms; use multiple calls if you need longer."
)
WAIT_SCHEMA = {
    "type": "object",
    "properties": {"ms": {"type": "integer", "minimum": 0, "maximum": 30_000}},
    "required": ["ms"],
    "additionalProperties": False,
}

_MAX_WAIT_MS = 30_000


async def _browser_wait_handler(
    arguments: dict[str, Any],
    *,
    tenant: Any = None,
    session_id: UUID | str | None = None,
    browser_pool: BrowserPool | None = None,
    browser_control: BrowserControlStore | None = None,
    _client_factory: Callable[..., Any] = _default_client_factory,
    **_: Any,
) -> str:
    pre = await _resolve_session_browser(
        arguments=arguments, tenant=tenant, session_id=session_id,
        browser_pool=browser_pool, browser_control=browser_control,
    )
    if isinstance(pre, str):
        return pre
    _bid, endpoint, snapshot_cache = pre

    ms = min(int(arguments.get("ms", 0)), _MAX_WAIT_MS)
    client = _make_client(_client_factory, endpoint, snapshot_cache)
    async with client:
        await client.wait(ms)
    return json.dumps({"waited_ms": ms})
```

- [ ] **Step 4: Wire into `register()`** — extend the `register` function at the bottom of `surogates/tools/builtin/browser.py`:

```python
    registry.register(
        name="browser_click",
        schema=ToolSchema(name="browser_click", description=CLICK_DESCRIPTION,
                          parameters=CLICK_SCHEMA),
        handler=_browser_click_handler, toolset="browser",
    )
    registry.register(
        name="browser_type",
        schema=ToolSchema(name="browser_type", description=TYPE_DESCRIPTION,
                          parameters=TYPE_SCHEMA),
        handler=_browser_type_handler, toolset="browser",
    )
    registry.register(
        name="browser_press_key",
        schema=ToolSchema(name="browser_press_key", description=PRESS_KEY_DESCRIPTION,
                          parameters=PRESS_KEY_SCHEMA),
        handler=_browser_press_key_handler, toolset="browser",
    )
    registry.register(
        name="browser_scroll",
        schema=ToolSchema(name="browser_scroll", description=SCROLL_DESCRIPTION,
                          parameters=SCROLL_SCHEMA),
        handler=_browser_scroll_handler, toolset="browser",
    )
    registry.register(
        name="browser_drag",
        schema=ToolSchema(name="browser_drag", description=DRAG_DESCRIPTION,
                          parameters=DRAG_SCHEMA),
        handler=_browser_drag_handler, toolset="browser",
    )
    registry.register(
        name="browser_wait",
        schema=ToolSchema(name="browser_wait", description=WAIT_DESCRIPTION,
                          parameters=WAIT_SCHEMA),
        handler=_browser_wait_handler, toolset="browser",
    )
```

- [ ] **Step 5: Run** — `pytest tests/test_browser_tools.py -v` → all PASS.

- [ ] **Step 6: Commit**

```bash
git add surogates/tools/builtin/browser.py tests/test_browser_tools.py
git commit -m "feat(browser): add click/type/press_key/scroll/drag/wait tools"
```

---

## Task 16: Browser tool — `browser_screenshot` (bounded base64 + annotate)

**Files:**
- Modify: `surogates/tools/builtin/browser.py`
- Modify: `tests/test_browser_tools.py`

- [ ] **Step 1: Write the failing test** — append to `tests/test_browser_tools.py`:

```python
class FakeScreenshotClient:
    def __init__(self) -> None:
        self.captured: list[dict[str, Any]] = []

    async def screenshot(self, *, region: dict | None = None, annotate: bool = False) -> dict:
        self.captured.append({"region": region, "annotate": annotate})
        if annotate:
            return {
                "png_bytes": b"\x89PNG\r\n\x1a\nimg",
                "annotations": [
                    {"ref": "@e1", "label": 1, "role": "button", "name": "Go"},
                ],
            }
        return {"png_bytes": b"\x89PNG\r\n\x1a\nimg"}

    async def close(self) -> None: ...
    async def __aenter__(self) -> "FakeScreenshotClient": return self
    async def __aexit__(self, *exc: Any) -> None: ...


class TestScreenshotHandler:
    async def test_annotate_returns_annotations(self, tenant) -> None:
        from surogates.tools.builtin.browser import _browser_screenshot_handler
        result = await _browser_screenshot_handler(
            {"annotate": True},
            tenant=tenant, session_id=uuid4(),
            browser_pool=FakePool(),
            browser_control=FakeControlStore(),
            _client_factory=lambda _: FakeScreenshotClient(),
        )
        body = json.loads(result)
        assert "base64" in body
        assert body["mime_type"] == "image/png"
        assert body["annotations"] == [
            {"ref": "@e1", "label": 1, "role": "button", "name": "Go"},
        ]

    async def test_returns_base64_png(self, tenant) -> None:
        from surogates.tools.builtin.browser import _browser_screenshot_handler
        result = await _browser_screenshot_handler(
            {},
            tenant=tenant, session_id=uuid4(),
            browser_pool=FakePool(),
            browser_control=FakeControlStore(),
            _client_factory=lambda _: FakeScreenshotClient(),
        )
        body = json.loads(result)
        assert "base64" in body
        assert body["mime_type"] == "image/png"
```

- [ ] **Step 2: Run** — 2 FAIL.

- [ ] **Step 3: Implement** — append to `surogates/tools/builtin/browser.py`:

```python
import base64

# ---------------------------------------------------------------------------
# Screenshot
# ---------------------------------------------------------------------------

SCREENSHOT_DESCRIPTION = (
    "Capture a PNG screenshot of the page as bounded base64. "
    "Pass annotate=true to overlay numbered labels on interactive elements; "
    "each label correlates with the @eN ref in the response 'annotations'."
)
SCREENSHOT_SCHEMA = {
    "type": "object",
    "properties": {
        "annotate": {"type": "boolean", "default": False},
        "region": {
            "type": "object",
            "properties": {
                "x": {"type": "integer"}, "y": {"type": "integer"},
                "width": {"type": "integer"}, "height": {"type": "integer"},
            },
            "required": ["x", "y", "width", "height"],
            "additionalProperties": False,
        },
    },
    "additionalProperties": False,
}

# Hard cap on base64 output to avoid blowing up tool results. Phase A does not
# use ArtifactStore because the current artifact API stores typed JSON
# artifacts (`name`, `kind`, `spec`), not arbitrary binary PNG payloads.
_MAX_BASE64_BYTES = 256 * 1024


async def _browser_screenshot_handler(
    arguments: dict[str, Any],
    *,
    tenant: Any = None,
    session_id: UUID | str | None = None,
    browser_pool: BrowserPool | None = None,
    browser_control: BrowserControlStore | None = None,
    _client_factory: Callable[..., Any] = _default_client_factory,
    **_: Any,
) -> str:
    pre = await _resolve_session_browser(
        arguments=arguments, tenant=tenant, session_id=session_id,
        browser_pool=browser_pool, browser_control=browser_control,
    )
    if isinstance(pre, str):
        return pre
    _bid, endpoint, snapshot_cache = pre

    annotate = bool(arguments.get("annotate", False))
    region = arguments.get("region")

    client = _make_client(_client_factory, endpoint, snapshot_cache)
    async with client:
        out = await client.screenshot(region=region, annotate=annotate)

    png_bytes = out["png_bytes"]
    annotations = out.get("annotations")

    if len(png_bytes) > _MAX_BASE64_BYTES:
        return json.dumps({
            "error": "screenshot_too_large_for_base64",
            "bytes": len(png_bytes),
            "guidance": (
                "Screenshot binary artifacts are deferred to a later phase; "
                "capture a smaller region or retry without annotation."
            ),
        })
    body = {
        "base64": base64.b64encode(png_bytes).decode(),
        "mime_type": "image/png",
        "bytes": len(png_bytes),
    }

    if annotations is not None:
        body["annotations"] = annotations
    return json.dumps(body)
```

- [ ] **Step 4: Wire into `register()`** — add to the bottom of the function:

```python
    registry.register(
        name="browser_screenshot",
        schema=ToolSchema(name="browser_screenshot", description=SCREENSHOT_DESCRIPTION,
                          parameters=SCREENSHOT_SCHEMA),
        handler=_browser_screenshot_handler, toolset="browser",
    )
```

- [ ] **Step 5: Run** — `pytest tests/test_browser_tools.py -v` → all PASS.

- [ ] **Step 6: Commit**

```bash
git add surogates/tools/builtin/browser.py tests/test_browser_tools.py
git commit -m "feat(browser): add screenshot tool with bounded base64 and annotate"
```

---

## Task 17: Wire browser tools into `ToolRouter` and `ToolRuntime`

**Files:**
- Modify: `surogates/tools/router.py`
- Modify: `surogates/tools/runtime.py`
- Modify: `surogates/governance/policy.py`
- Test: `tests/test_browser_tools.py` (add registration tests)

- [ ] **Step 1: Write the failing test** — append to `tests/test_browser_tools.py`:

```python
class TestToolWiring:
    def test_router_locates_browser_tools_in_harness(self) -> None:
        from surogates.tools.router import TOOL_LOCATIONS, ToolLocation
        for tool in [
            "browser_navigate", "browser_get_state", "browser_screenshot",
            "browser_click", "browser_type", "browser_press_key",
            "browser_scroll", "browser_drag", "browser_wait", "browser_close",
        ]:
            assert TOOL_LOCATIONS[tool] == ToolLocation.HARNESS, tool

    def test_runtime_registers_browser_tools(self) -> None:
        from surogates.tools.registry import ToolRegistry
        from surogates.tools.runtime import ToolRuntime

        registry = ToolRegistry()
        runtime = ToolRuntime(registry)
        runtime.register_builtins()

        for tool in [
            "browser_navigate", "browser_get_state", "browser_screenshot",
            "browser_click", "browser_type", "browser_press_key",
            "browser_scroll", "browser_drag", "browser_wait", "browser_close",
        ]:
            assert registry.has(tool), tool

    def test_governance_url_arg_includes_browser_navigate(self) -> None:
        from surogates.governance.policy import _URL_ARGUMENT_MAP
        assert "url" in _URL_ARGUMENT_MAP["browser_navigate"]
```

- [ ] **Step 2: Run** — first two tests FAIL; third passes (already there).

- [ ] **Step 3: Modify `surogates/tools/router.py`** — extend the
`TOOL_LOCATIONS` dict (around line 40) with the browser tools:

```python
    # Agent browser (Phase A) — all harness-local; speaks directly to the
    # browser pod's REST API via KernelBrowserClient. The browser pod is a
    # separate resource from the workspace sandbox; see spec §4.3.
    "browser_navigate": ToolLocation.HARNESS,
    "browser_get_state": ToolLocation.HARNESS,
    "browser_screenshot": ToolLocation.HARNESS,
    "browser_click": ToolLocation.HARNESS,
    "browser_type": ToolLocation.HARNESS,
    "browser_press_key": ToolLocation.HARNESS,
    "browser_scroll": ToolLocation.HARNESS,
    "browser_drag": ToolLocation.HARNESS,
    "browser_wait": ToolLocation.HARNESS,
    "browser_close": ToolLocation.HARNESS,
```

- [ ] **Step 4: Modify `surogates/tools/runtime.py`** — register the browser
module. In `register_builtins`, add `browser` to the import block and the
modules list, replacing the comment block that currently says the stub is
disabled:

```python
        from surogates.tools.builtin import (
            artifact,
            browser,           # <-- add this line, drop the "browser is a stub" note
            clarify,
            coordinator,
            cron,
            ...
```

And in the `modules = [...]` list, add `browser,` adjacent to `web_search`
(or any sensible position):

```python
        modules = [
            memory,
            skills,
            skill_manager,
            vision,
            web_search,
            browser,           # <-- add
            ...
```

Also delete the now-stale paragraph in the docstring that calls browser a stub.

- [ ] **Step 5: `surogates/governance/policy.py`** is already correct — the
existing `_URL_ARGUMENT_MAP` entry for `browser_navigate` is what we want.
No change needed.

- [ ] **Step 6: Run** — `pytest tests/test_browser_tools.py -v` → all PASS.

- [ ] **Step 7: Commit**

```bash
git add surogates/tools/router.py surogates/tools/runtime.py tests/test_browser_tools.py
git commit -m "feat(browser): wire browser tools into router and runtime"
```

---

## Task 18: Thread `browser_pool` and `browser_control` through `execute_single_tool`

**Files:**
- Modify: `surogates/harness/tool_exec.py`
- Modify: `surogates/harness/loop.py` (only if it forwards kwargs explicitly)
- Modify: `surogates/tools/router.py` (only if it forwards kwargs to harness handlers)

- [ ] **Step 1: Locate the kwarg surface**

Run:

```bash
grep -n "sandbox_pool" surogates/harness/tool_exec.py surogates/harness/loop.py surogates/harness/streaming_executor.py surogates/tools/router.py
```

For every site that accepts or forwards `sandbox_pool: SandboxPool | None = None`,
add the parallel kwargs:

```python
browser_pool: "BrowserPool | None" = None,
browser_control: "BrowserControlStore | None" = None,
```

(Use forward-reference quoted strings under `if TYPE_CHECKING` to avoid
circular imports — follow the existing pattern for `SandboxPool`.)

- [ ] **Step 2: At the dispatch site inside `execute_single_tool`**

When the registry's `dispatch` is called (or the router's `execute`),
forward the new kwargs:

```python
result = await tools.dispatch(
    tool_name,
    tool_args,
    tenant=tenant,
    session_id=session.id,
    sandbox_pool=sandbox_pool,
    browser_pool=browser_pool,
    browser_control=browser_control,
    api_client=api_client,
    # ... any others already there
)
```

- [ ] **Step 3: Add an integration-style test**

`tests/test_browser_tools.py` already covers handler-level wiring with
direct invocation. Add one router-level test:

```python
class TestRouterDispatch:
    async def test_router_dispatches_browser_navigate(self, tenant, monkeypatch) -> None:
        import json
        from uuid import uuid4

        from surogates.governance.policy import GovernanceGate, PolicyDecision
        from surogates.tools.registry import ToolRegistry
        from surogates.tools.runtime import ToolRuntime
        from surogates.tools.router import ToolRouter

        registry = ToolRegistry()
        ToolRuntime(registry).register_builtins()

        class AllowAll(GovernanceGate):
            def __init__(self) -> None: ...
            def check(self, *args: Any, **kwargs: Any) -> PolicyDecision:
                return PolicyDecision(allowed=True, reason="test")

        # Sandbox pool not used by harness tools, but the router needs *something*.
        sandbox_pool = None  # the path is HARNESS, sandbox_pool isn't dereferenced

        router = ToolRouter(
            registry=registry, sandbox_pool=sandbox_pool, governance=AllowAll(),
        )

        # Patch the navigate handler's pool/control via kwargs.
        result = await router.execute(
            name="browser_navigate",
            arguments={"url": "https://example.com"},
            tenant=tenant,
            session_id=uuid4(),
            browser_pool=FakePool(),
            browser_control=FakeControlStore(),
            _client_factory=lambda _: FakeClient(),
        )
        body = json.loads(result)
        assert body["url"] == "https://example.com"
```

This test **will fail until step 2** is done — it asserts the new kwargs
flow through `router.execute` to `registry.dispatch` to the handler.

- [ ] **Step 4: Update `ToolRouter.execute`** — `surogates/tools/router.py`,
the `execute` method (around line 133). Add the new kwargs and forward
them to `registry.dispatch`:

```python
    async def execute(
        self,
        *,
        name: str,
        arguments: str | dict[str, Any],
        tenant: Any,
        session_id: UUID,
        workspace_path: str | None = None,
        # New ↓
        browser_pool: "BrowserPool | None" = None,
        browser_control: "BrowserControlStore | None" = None,
        **extra_kwargs: Any,
    ) -> str:
        ...
        match location:
            case ToolLocation.HARNESS:
                return await self.registry.dispatch(
                    name,
                    arguments,
                    tenant=tenant,
                    session_id=session_id,
                    browser_pool=browser_pool,
                    browser_control=browser_control,
                    **extra_kwargs,
                )
            case ToolLocation.SANDBOX:
                ...
```

Add the matching `if TYPE_CHECKING` import block at the top:

```python
if TYPE_CHECKING:
    from surogates.browser.control import BrowserControlStore
    from surogates.browser.pool import BrowserPool
```

- [ ] **Step 5: Run** — `pytest tests/test_browser_tools.py -v` → all PASS,
including the new router test.

- [ ] **Step 6: Commit**

```bash
git add surogates/harness/tool_exec.py surogates/harness/loop.py \
        surogates/harness/streaming_executor.py surogates/tools/router.py \
        tests/test_browser_tools.py
git commit -m "feat(browser): thread browser_pool / browser_control through tool dispatch"
```

---

## Task 19: Worker bootstrap — always instantiate pool/registry/control

**Files:**
- Modify: `surogates/orchestrator/worker.py`

- [ ] **Step 1: Identify bootstrap location**

Open `surogates/orchestrator/worker.py` and locate the section where
`SandboxPool` is created (the same file, near the top of the worker
function, immediately after imports + settings load).

- [ ] **Step 2: Add browser bootstrap right after sandbox bootstrap**

The browser pool is always instantiated. The `backend` setting is the
only environment switch. Phase A only ships `process`; the `kubernetes`
branch raises until Phase B fills it in.

```python
    # Sandbox pool (existing) ---------------------------------------------
    if settings.sandbox.backend == "kubernetes":
        sandbox_backend = K8sSandbox(...)
    else:
        sandbox_backend = ProcessSandbox()
    sandbox_pool = SandboxPool(sandbox_backend)

    # Browser pool (Phase A) ----------------------------------------------
    from surogates.browser.control import BrowserControlStore
    from surogates.browser.pool import BrowserPool
    from surogates.browser.process import ProcessBrowserBackend
    from surogates.browser.registry import BrowserRegistry

    if settings.browser.backend == "kubernetes":
        raise RuntimeError(
            "browser.backend=kubernetes is reserved for Phase B; "
            "set browser.backend=process for Phase A.",
        )
    browser_backend = ProcessBrowserBackend(
        image=settings.browser.image,
        rest_port_base=settings.browser.rest_port_base,
        cdp_port_base=settings.browser.cdp_port_base,
        live_view_port_base=settings.browser.live_view_port_base,
    )
    browser_registry = BrowserRegistry(redis_client)
    browser_control = BrowserControlStore(redis_client)

    async def _emit_browser_event(
        session_id: str, event_type: str, data: dict,
    ) -> None:
        from surogates.session.events import EventType
        try:
            from uuid import UUID
            await session_store.emit_event(
                UUID(session_id),
                EventType(event_type),
                data,
            )
        except Exception:  # noqa: BLE001 — best effort
            logger.exception("Failed to emit browser event %s", event_type)

    browser_pool = BrowserPool(
        backend=browser_backend,
        registry=browser_registry,
        event_emitter=_emit_browser_event,
    )
    logger.info("Agent browser ready (backend=%s)", settings.browser.backend)
```

- [ ] **Step 3: Pass to `AgentHarness`**

Find the `AgentHarness(...)` instantiation in the same file and add the
two kwargs:

```python
    harness = AgentHarness(
        ...
        sandbox_pool=sandbox_pool,
        browser_pool=browser_pool,
        browser_control=browser_control,
        ...
    )
```

(The `AgentHarness` and downstream `tool_exec` were updated in Task 18.)

- [ ] **Step 4: Add cleanup**

Find the existing `await sandbox_pool.destroy_all()` shutdown block and add:

```python
        await browser_pool.destroy_all()
```

- [ ] **Step 5: Verify by running the broader test suite**

```bash
pytest tests/ -k "not e2e" -q
```

Expected: all existing tests still pass; new browser tests still pass.

- [ ] **Step 6: Commit**

```bash
git add surogates/orchestrator/worker.py
git commit -m "feat(browser): wire BrowserPool / BrowserRegistry / BrowserControlStore into worker bootstrap"
```

---

## Task 20: AgentHarness signature update

**Files:**
- Modify: `surogates/harness/loop.py`

- [ ] **Step 1: Inspect `AgentHarness.__init__`** to find where `sandbox_pool` is accepted and stored.

```bash
grep -n "sandbox_pool" surogates/harness/loop.py
```

- [ ] **Step 2: Add browser kwargs**

In `AgentHarness.__init__`:

```python
    def __init__(
        self,
        *,
        ...
        sandbox_pool: "SandboxPool | None" = None,
        browser_pool: "BrowserPool | None" = None,
        browser_control: "BrowserControlStore | None" = None,
        ...
    ) -> None:
        ...
        self._sandbox_pool = sandbox_pool
        self._browser_pool = browser_pool
        self._browser_control = browser_control
```

Add the matching `if TYPE_CHECKING` block:

```python
if TYPE_CHECKING:
    from surogates.browser.control import BrowserControlStore
    from surogates.browser.pool import BrowserPool
```

- [ ] **Step 3: Forward to `tool_exec.execute_tool_calls`**

Find the call site inside `wake()` (or wherever the harness invokes
`tool_exec.execute_tool_calls`) and add:

```python
        await execute_tool_calls(
            tool_calls,
            ...
            sandbox_pool=self._sandbox_pool,
            browser_pool=self._browser_pool,
            browser_control=self._browser_control,
            ...
        )
```

- [ ] **Step 4: Run the harness tests**

```bash
pytest tests/test_harness_resilience.py tests/test_streaming_executor.py -q
```

Expected: still PASS — kwargs are optional with `None` default, so
existing instantiations keep working.

- [ ] **Step 5: Commit**

```bash
git add surogates/harness/loop.py
git commit -m "feat(browser): accept browser_pool / browser_control on AgentHarness"
```

---

## Task 21: End-to-end smoke (opt-in, requires Docker + kernel-images image)

**Files:**
- Create: `tests/integration/test_browser_e2e.py`

- [ ] **Step 1: Create the opt-in marker test**

`tests/integration/test_browser_e2e.py`:

```python
"""End-to-end browser smoke test.

Requires:
- Docker running on the host.
- The kernel-images headful image already pulled
  (ghcr.io/onkernel/chromium-headful:stable).

Skipped by default. Run explicitly:

    pytest -m browser_e2e tests/integration/test_browser_e2e.py -v

Marker and default deselection wiring for ``browser_e2e`` lives in
``pyproject.toml``.
"""

from __future__ import annotations

import os

import pytest

from surogates.browser.base import BrowserSpec
from surogates.browser.client import KernelBrowserClient
from surogates.browser.process import ProcessBrowserBackend


pytestmark = pytest.mark.browser_e2e

E2E_IMAGE = os.environ.get(
    "BROWSER_E2E_IMAGE", "ghcr.io/onkernel/chromium-headful:stable",
)


@pytest.fixture()
async def backend():
    b = ProcessBrowserBackend(
        image=E2E_IMAGE,
        rest_port_base=39000,
        cdp_port_base=39100,
        live_view_port_base=39200,
    )
    yield b


@pytest.fixture()
async def browser(backend):
    bid, endpoint = await backend.provision(BrowserSpec(image=E2E_IMAGE, pod_ready_timeout=60))
    try:
        yield bid, endpoint
    finally:
        await backend.destroy(bid)


async def test_navigate_and_get_state(browser) -> None:
    _bid, endpoint = browser
    async with KernelBrowserClient(rest_url=endpoint.rest_url) as c:
        out = await c.navigate("https://example.com")
        assert "Example" in out["title"]

        state = await c.get_state(interactive_only=True)
        # example.com has at least one link ("More information…").
        assert any(n["role"] == "link" for n in state["tree"])


async def test_screenshot_returns_png(browser) -> None:
    _bid, endpoint = browser
    async with KernelBrowserClient(rest_url=endpoint.rest_url) as c:
        await c.navigate("https://example.com")
        result = await c.screenshot()
        assert result["png_bytes"].startswith(b"\x89PNG")
        assert len(result["png_bytes"]) > 1000   # non-trivial image
```

- [ ] **Step 2: Add the marker to `pyproject.toml`**

Inside `[tool.pytest.ini_options]`, add an explicit default marker filter and
append to the `markers` list. A marker declaration alone does **not** skip or
deselect tests; `addopts` is what keeps Docker-dependent tests out of the
default suite.

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
addopts = "-m 'not browser_e2e'"
markers = [
    "browser_e2e: end-to-end agent browser tests requiring Docker + kernel-images image (opt-in)",
]
```

- [ ] **Step 3: Verify the marker is registered**

```bash
pytest --markers | grep browser_e2e
```

Expected: prints the marker description; no warning about unknown markers.

- [ ] **Step 4: Run the suite without the marker (default skip)**

```bash
pytest tests/ -q
```

Expected: all earlier tests PASS; `tests/integration/test_browser_e2e.py`
is collected but **deselected** by the `addopts = "-m 'not browser_e2e'"`
filter.

- [ ] **Step 5: Run the e2e test explicitly (optional, when Docker + image are available)**

```bash
docker pull ghcr.io/onkernel/chromium-headful:stable
pytest -m browser_e2e tests/integration/test_browser_e2e.py -v -s
```

Expected: 2 tests PASS against the real container.

- [ ] **Step 6: Commit**

```bash
git add tests/integration/test_browser_e2e.py pyproject.toml
git commit -m "test(browser): add opt-in e2e smoke test against real kernel-images container"
```

---

## Final verification

After all 21 tasks:

```bash
pytest tests/ -q
```

Expected: full suite green; new browser tests counted alongside existing.

Optional, when Docker + the image are available locally:

```bash
docker pull ghcr.io/onkernel/chromium-headful:stable
pytest -m browser_e2e -v
```

---

## What Phase A delivers

- **Backend skeleton** — `BrowserBackend` protocol, `BrowserSpec`,
  `BrowserStatus`, `BrowserUnavailableError`, `browser_unavailable_result`.
- **`KernelBrowserClient`** — full coverage of the discrete Phase A
  endpoints with httpx; shared per-session `@e1`-style ref cache;
  DOM-derived snapshot filters; annotated screenshots with overlay
  inject/remove.
- **`BrowserRegistry`** — Redis hash for cross-process pod metadata
  (consumed by API server in Phase C).
- **`BrowserControlStore`** — Redis-backed user-control flag with
  acquire-conflict semantics.
- **`ProcessBrowserBackend`** — runs kernel-images via `docker run` for
  local dev.
- **`BrowserPool`** — session-scoped lifecycle, registry mirroring,
  event emission.
- **10 discrete tools** — `browser_navigate`, `browser_get_state`,
  `browser_screenshot`, `browser_click`, `browser_type`,
  `browser_press_key`, `browser_scroll`, `browser_drag`,
  `browser_wait`, `browser_close` — wired into router and runtime.
- **Always-on tool registration** — browser tools are always present in
  the LLM's tool list; backend choice (`process` vs `kubernetes`) is
  the only environment switch. When the backend can't reach a running
  browser, calls return ``browser_unavailable`` with standard guidance.
- **Worker bootstrap integration** — pool/registry/control instantiated
  unconditionally on startup; cleaned up on shutdown.
- **Opt-in e2e smoke test** — verifies the whole stack against a real
  Chromium when Docker is available.

Phase A explicitly does **not** ship: K8s backend, API server endpoints
for `/browser/state` / `/browser/live` / `/browser/control`, SPA UI,
profile sync, recording. Those land in Phases B/C/D against the same
`BrowserBackend` / `BrowserPool` / `KernelBrowserClient` interfaces, with
no rework needed in this layer.
