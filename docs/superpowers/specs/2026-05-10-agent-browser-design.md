# Agent Browser via Kernel-Images — Design

Date: 2026-05-10
Status: Approved (brainstorming)

## 1. Context

The current `browser_navigate` tool in `surogates/tools/builtin/browser.py` is a
stub: it advertises a sandbox-routed schema but its handler returns a
`sandbox_required` error. Surogates has no working browser capability.

Hermes ships a 3500-line `browser_tool.py` with multiple cloud and local
backends (Browserbase, Browser Use, Camofox, agent-browser CLI). Reusing it
would lock us into provider-specific code paths and leaves us without a
sandboxed live-view experience.

`./study/kernel-images` (https://kernel.sh) is a sandboxed Chromium image
exposing:

- **CDP** on port `9222` (Playwright/Puppeteer-compatible).
- **REST API** on port `10001` (computer actions, recording, file ops, process
  exec, Playwright code execution, structured "capture session" events).
- **Live view** internally on NoVNC `:6080` or WebRTC `:8080`; Unikraft maps
  those to external `:443`, while our K8s Service performs the equivalent
  internal port mapping.

Kernel-images is itself a sandbox image — one container per browser session.
That maps cleanly onto the Surogates "one sandbox per session" model and lets
us build a full agentic browser without writing a Chromium harness from
scratch.

## 2. Goals

1. **Full agentic browsing** — discrete tools for navigate, click, type,
   scroll, drag, key press, screenshot, get-state. Comparable to Anthropic
   Computer Use / OpenAI Operator action surfaces.
2. **One browser per agent session** — provisioned lazily on first
   `browser_*` tool call, destroyed when the session ends.
3. **Live view in the web chat UI** — user can watch the agent drive the
   browser and take over (e.g. for CAPTCHAs, MFA, login).
4. **Per-tenant profile persistence** — cookies and login state survive
   across sessions for the same user.
5. **K8s-native** — runs in our existing per-session pod model with no new
   public ingress surface.
6. **Architecture parity** — browser tools follow the same governance,
   tenancy, and event-emission patterns as existing tools.

## 3. Non-goals

- **Multi-browser-per-session.** One browser per session. Multi-tab via the
  same browser is fine; multiple Chromium instances per session is not.
- **Cloud browser providers.** No Browserbase, Browser Use, Camofox in v1.
  Self-hosted kernel-images only.
- **Always-on recording.** Recording is opt-in via a tool/flag, not the
  default.
- **Unikraft unikernel deployment.** Vendor-bound to Unikraft Cloud; we run
  the Docker headful image in our own K8s.
- **Replacing the existing workspace sandbox.** Browser pod is a separate
  resource; the workspace sandbox keeps its current role.

## 4. Architecture

### 4.1 Components

A new worker-local `BrowserPool` (sibling to `SandboxPool`) owns provisioning
and teardown. Because the API server also needs to resolve browser pods for
state and live-view proxying, pod metadata is mirrored into a shared
`BrowserRegistry` keyed by `session_id` (Redis hash in v1, reconstructable
from K8s labels as a fallback). Lazy provisioning happens on the first
`browser_*` tool call; teardown happens on explicit close, session completion,
interrupt/reset cleanup, or pod deadline expiry.

Browser tools run **harness-local** (in the worker process) and call the
browser pod's REST API over the cluster network via a thin Python client. API
routes never talk to the worker's in-memory pool; they read `BrowserRegistry`
and proxy to the registered Service address.

```
┌─────────────────────────────────────────────────────────────────┐
│ Worker (harness)                                                │
│  ┌──────────────┐   ┌──────────────┐   ┌──────────────────────┐ │
│  │ ToolRouter   │──▶│ browser_*    │──▶│ KernelBrowserClient  │ │
│  │ (HARNESS)    │   │ handlers     │   │ (httpx)              │ │
│  └──────────────┘   └──────────────┘   └──────┬───────────────┘ │
│                                               │                 │
│  ┌──────────────────────────────────────┐     │                 │
│  │ BrowserPool (session_id → pod)       │◀────┘ ensure() before │
│  │ - ensure(session_id, spec)            │       first call     │
│  │ - destroy_for_session(session_id)     │                      │
│  │ - writes BrowserRegistry              │                      │
│  │ - profile sync on provision/teardown  │                      │
│  └──────────────────────────────────────┘                      │
└─────────────────────────────────────────────────────────────────┘
                  │ K8s API (create/delete pod)
                  ▼
┌─────────────────────────────────────────────────────────────────┐
│ K8s Namespace: surogates                                        │
│                                                                 │
│  Pod: browser-{id[:12]}       (one per active session)         │
│  ├─ container: kernel-images-headful                            │
│  │    Chromium + Xorg + Mutter + supervisor                     │
│  │    REST API on :10001                                        │
│  │    CDP on :9222                                              │
│  │    NoVNC on :6080  (v1)                                      │
│  │    WebRTC on :8080 (future, ENABLE_WEBRTC=true)              │
│  │    /home/kernel/profile/  ← user-data-dir                    │
│  │                                                              │
│  Service: browser-{id[:12]}.surogates.svc                       │
│    - port 443 → targetPort 6080 for NoVNC                       │
│    - port 10001 → targetPort 10001 for REST                     │
│  NetworkPolicy: ingress only from worker + api-server           │
└─────────────────────────────────────────────────────────────────┘
                  ▲
                  │ wss://...   (live view)
                  │
┌─────────────────────────────────────────────────────────────────┐
│ API Server                                                       │
│  BrowserRegistry (Redis): session_id → service URL/status       │
│  BrowserControlStore (Redis): session_id → user-control flag    │
│  GET /v1/sessions/{id}/browser/state ← registry + control       │
│  HTTP/WS /v1/sessions/{id}/browser/live/* ← NoVNC proxy         │
│  POST /v1/sessions/{id}/browser/control ← acquire/release      │
└─────────────────────────────────────────────────────────────────┘
                  ▲
                  │ wss + SSE
                  │
              [Web Chat UI]
```

The user's browser only ever talks to the API server. The browser pod stays on
the cluster network, reachable from worker and api-server only. Browser pods
are labeled with `surogates/session-id`, `surogates/org-id`, and
`surogates/user-id` so API fallback lookup and orphan cleanup do not depend
solely on worker memory.

### 4.2 New module layout

```
surogates/browser/
├── __init__.py
├── client.py        # KernelBrowserClient — httpx wrapper around kernel-images REST API
├── pool.py          # BrowserPool — session_id → pod_url + lifecycle
├── registry.py      # BrowserRegistry — shared Redis/K8s metadata for API proxy lookup
├── control.py       # BrowserControlStore — shared user-control flag + helpers
├── kubernetes.py    # K8sBrowserBackend — provisions browser pods
├── process.py       # ProcessBrowserBackend — runs kernel-images via docker for dev
├── profile.py       # ProfileSync — sync tenant profile to/from S3 via /fs/upload_zstd
└── base.py          # BrowserBackend protocol, BrowserSpec, BrowserStatus
```

```
surogates/tools/builtin/browser.py
```

Replaced (not augmented) with the discrete tool registrations. The existing
stub goes away.

```
surogates/api/routes/browser.py
```

New API routes for browser state, live-view WS proxy, and control acquisition.

### 4.3 Tool dispatch

`ToolLocation.HARNESS` for every browser tool. The router does not go through
`SandboxPool`. Each handler:

1. Checks `BrowserControlStore`; if user control is active, returns
   `{"error": "paused_by_user", "guidance": "..."}` without touching the pod.
2. Calls `BrowserPool.ensure(session_id, spec)` — provisions on first call and
   writes/refreshes the shared `BrowserRegistry` entry.
3. Builds a `KernelBrowserClient` bound to the pod REST URL.
4. Issues the corresponding REST call.
5. Returns the result as a JSON string (the standard tool-result shape).

The control-store short-circuit applies to **every** `browser_*` tool,
including `browser_close`. The agent must not yank the browser while the
user is mid-task; tearing down the pod is itself an action the user might
want to refuse, so the same `paused_by_user` response is returned. If the
agent needs to close the browser, the user has to release control first.

This keeps the existing `SandboxPool` logic untouched. The browser pod is a
separate resource with a separate lifecycle.

## 5. Tool surface

Discrete tools, one per kernel-images REST endpoint we expose. Each is
governance-checked like any other tool.

| Tool | kernel-images endpoint | Purpose |
|---|---|---|
| `browser_navigate` | `/playwright/execute` (`page.goto`) | Navigate to URL; returns final URL + page title |
| `browser_get_state` | `/playwright/execute` (a11y snapshot) | Default perception: structured a11y tree with `@e1`-style refs, plus URL and title |
| `browser_screenshot` | `/computer/screenshot` | PNG of viewport (or region); returns artifact metadata or base64 fallback |
| `browser_click` | `/computer/click_mouse` | Click by coords or by `@ref` (resolved via cached a11y snapshot) |
| `browser_type` | `/computer/type` | Type text; supports `@ref` to focus first |
| `browser_press_key` | `/computer/press_key` | Press named key(s) — Tab, Enter, ctrl+l, etc. |
| `browser_scroll` | `/computer/scroll` | Scroll at coords by delta |
| `browser_drag` | `/computer/drag_mouse` | Drag along a path |
| `browser_wait` | (in-process sleep) | Wait N ms — for animations / async loads |
| `browser_record_start` | `/recording/start` | Begin mp4 capture |
| `browser_record_stop` | `/recording/stop` + `/recording/download` | Stop mp4 capture; downloads MP4 and uploads artifact to session bucket |
| `browser_close` | (BrowserPool.destroy_for_session) | Explicit close before session end |

Tools land in `surogates/tools/builtin/browser.py` (one file, single
responsibility — registrations + handlers thin enough to fit). Each handler
delegates to `KernelBrowserClient`. The `@ref → coords` resolution lives in
the client since it caches the last a11y snapshot per browser pod.

Binary outputs are never returned as raw bytes in tool results:

- `browser_screenshot` stores the PNG through the existing artifact/session
  storage path and returns `{ "artifact_id", "mime_type": "image/png",
  "width", "height" }`. In local/dev mode without storage, it may return
  bounded base64 with an explicit byte limit.
- `browser_record_stop` calls `/recording/stop`, polls
  `/recording/download?id=...` until the MP4 is ready (handling `202
  Retry-After`), uploads the file as a session artifact, then optionally calls
  `/recording/delete`.

### 5.1 Perception model

`browser_get_state` is the default perception channel. It returns:

```json
{
  "url": "https://app.com/dashboard",
  "title": "Dashboard",
  "viewport": { "width": 1280, "height": 800 },
  "tree": [
    { "ref": "@e1", "role": "link", "name": "Settings", "x": 1130, "y": 24 },
    { "ref": "@e2", "role": "button", "name": "New project", "x": 200, "y": 80 },
    ...
  ]
}
```

The client caches the most recent snapshot per session. `browser_click @e2`
resolves to the cached coords; the cache invalidates on any action that may
mutate the DOM (click, type, navigate, key press) — the LLM is expected to
call `browser_get_state` again after such actions. `browser_screenshot` is
available when the tree isn't enough (visual layouts, CAPTCHAs).

## 6. Persistence — per-tenant profile

Profile lives at `tenant-{org_id}/users/{user_id}/browser-profile/` in Garage.

- **On provision (after pod ready):**
  1. Worker downloads the profile from S3 to a temp dir.
  2. Worker tar-zstd-compresses it and POSTs to the pod's `/fs/upload_zstd`
     with `dest_path=/home/kernel/profile`.
  3. Worker calls `/chromium/flags` PATCH with
     `--user-data-dir=/home/kernel/profile`. Kernel-images restarts Chromium
     and waits for the DevTools listening line.

- **On teardown (session end or explicit close):**
  1. Worker calls `/fs/download_dir_zstd?path=/home/kernel/profile` to fetch
     the profile.
  2. Worker uploads it back to S3 under the tenant profile path.
  3. Pod is deleted.

A new user has no profile yet; provisioning skips the upload step. This is
fine — Chromium creates a fresh user-data-dir on first launch.

`ProfileSync` is a separate module so the I/O is testable without K8s.
Concurrent sessions for the same user are an edge case in v1: last writer
wins. (Acceptable — multi-session-per-user-with-shared-profile is uncommon
and adds locking complexity. Document it; revisit if a real complaint
appears.)

## 7. Live view — HTTP/WebSocket proxy

The API server acts as an authenticated HTTP/WebSocket proxy between the SPA
and the browser pod's live-view Service port. In v1 the upstream is NoVNC:
the Kubernetes Service exposes `:443` but routes to container `targetPort:
6080`. WebRTC remains future work and would route service `:443` to container
`targetPort: 8080` with the extra ICE/TURN configuration it requires.

### 7.1 API endpoints

```
GET  /v1/sessions/{id}/browser/state
  → 200 { status: "live"|"provisioning"|"closed", url, title, control_owner }
  → 404 if no browser provisioned

HTTP/WS /v1/sessions/{id}/browser/live/{path:path}
  ← HTTP request or WS upgrade with session JWT
  → API server validates JWT
  → API server resolves browser pod via BrowserRegistry
  → API server proxies upstream to browser-{id[:12]}.surogates.svc:443
    (Service targetPort 6080 for NoVNC v1)
  → HTTP assets stream normally; websocket traffic uses a bidirectional pump
  → Honor BrowserControlStore flag for websocket input frames

POST /v1/sessions/{id}/browser/control
  body: { action: "acquire" | "release" }
  → 200 — emits BROWSER_CONTROL_GRANTED / BROWSER_CONTROL_RETURNED
```

### 7.2 Read-only enforcement

Kernel-images supports container-level read-only via `ENABLE_READONLY_VIEW`
but lacks a runtime toggle. We start the container in interactive mode and
gate input at the proxy:

- Outbound (pod → user) frames are always forwarded.
- Inbound (user → pod) frames carrying input events are dropped unless
  `BrowserControlStore` says the user owns browser control.

For NoVNC, "input frames" are RFB ClientMessage types `KeyEvent (4)`,
`PointerEvent (5)`, and `ClientCutText (6)` — the last is clipboard paste,
which is also user-originating input and must be dropped when control is
not granted. Other ClientMessage types (`SetPixelFormat`, `SetEncodings`,
`FramebufferUpdateRequest`) are forwarded so the read-only client can still
negotiate framebuffer updates. For WebRTC, input goes over a separate data
channel that we gate at the data-channel multiplexer. We start with NoVNC
for v1 (simpler proxy) and add WebRTC in a follow-up.

### 7.3 Control flow

User-control acquisition:

1. SPA POSTs `/v1/sessions/{id}/browser/control { action: "acquire" }`.
2. API server validates the session owner, then resolves the current
   `BrowserControlStore[session_id]` entry:
   - **Unheld:** set `{ owner_user_id, acquired_at }`, emit
     `BROWSER_CONTROL_GRANTED`, return `200`.
   - **Held by the same user (different tab/device):** treat as idempotent
     refresh — update `acquired_at`, do **not** re-emit
     `BROWSER_CONTROL_GRANTED`, return `200`. Both tabs receive input.
   - **Held by a different user with session access:** return `409 Conflict`
     with `{ "holder_user_id", "acquired_at" }` so the SPA can surface
     "another user is currently controlling this browser." No event emitted.
     Sessions are usually single-user, but we don't want a silent steal if
     two users share access (e.g., admin shadowing).
3. On grant, the WS proxy starts forwarding input frames.
4. Worker's next `browser_*` tool call short-circuits with
   `{"error": "paused_by_user", "guidance": "..."}`.
5. A one-time system-injected message ("The user has taken control of the
   browser. Wait for them to finish before continuing.") is prepended to the
   next LLM iteration so the model doesn't keep retrying.

User-control release:

1. SPA POSTs `{ action: "release" }`.
2. API server clears the `BrowserControlStore` entry, emits
   `BROWSER_CONTROL_RETURNED`, queues a wake on the session via the
   orchestrator's Redis queue.
3. Worker resumes naturally on next iteration; browser tools succeed again.

The worker is **not suspended** — it just gets `paused_by_user` from any
browser tool while the user has control. The control check must read shared
runtime state on every browser-tool call, not `session.config`, because the
harness loop holds an in-memory session snapshot during a wake.

## 8. UI design — web chat SPA

### 8.1 Layout

The right pane stacks browser-on-top, workspace-below when a browser is
active. Resizable divider. Either side collapses with a chevron.

```
┌─────────────────────────────────────┐
│ ⚡ Browser  ●  https://app.com/login │  ← header: status dot + URL
├─────────────────────────────────────┤
│                                     │
│         [ live view iframe ]        │
│                                     │
│  (NoVNC over WS proxied              │
│   by API server)                    │
│                                     │
├─────────────────────────────────────┤
│ [ Take control ]  ⏺ rec  ⋯ menu     │  ← controls bar
├═════════════════════════════════════┤
│ 📁 surogate-agent-2cf550cd…  ▾      │  ← workspace section
│ files...                            │     (resizable / collapsible)
└─────────────────────────────────────┘
```

When no browser is provisioned, the right pane is the workspace alone — the
current UI, unchanged.

### 8.2 States

| State | Trigger | Visual |
|---|---|---|
| `not provisioned` | no browser pod yet | section hidden; right pane = workspace only |
| `provisioning` | first `browser_*` tool call | section appears with skeleton + "Starting browser…" + spinner |
| `live` | pod ready, WS connected | live view rendering; status dot green; URL in header |
| `user-control` | user clicked "Take control" | input forwarded; status dot amber + "You have control"; button reads "Return control" |
| `recording` | `/recording/start` succeeded | red dot + "REC 00:23" timer in controls bar |
| `closed` | session ended or browser destroyed | section flashes "Browser closed" then hides |

### 8.3 Thread rendering — collapsed activity group

- Consecutive `browser_*` tool calls collapse under one group header:
  `⚡ browser (N actions — latest: <verb> <target>) ▾`.
- Group breaks when an LLM message or non-browser tool call interleaves
  (preserves causality).
- Expanding shows per-action list: `verb @ref "label"` or `verb "value"`.
- Clicking the group header (or any action) focuses the right-pane browser
  section (scroll into view + flash border).
- Errors (`paused_by_user`, navigation timeouts, element-not-found) render
  with a red marker on the affected line.

### 8.4 Inline thread markers

Browser lifecycle events emit one-line inline markers in the thread (subtle,
not full cards):

| Event | Marker |
|---|---|
| `BROWSER_PROVISIONED` | `⚡ browser ready` |
| `BROWSER_CONTROL_GRANTED` | `⚠ user took control` (warning style) |
| `BROWSER_CONTROL_RETURNED` | `⚡ control returned to agent` |
| `BROWSER_RECORDING_STARTED` | `⏺ recording` |
| `BROWSER_RECORDING_STOPPED` | `⏹ recording saved` (links to artifact) |
| `BROWSER_DESTROYED` | `⚡ browser closed` |

### 8.5 SSE consumption

The SPA already subscribes to `/v1/sessions/{id}/events`. New event types are
added to the existing stream; the right-pane reducer listens for browser.*
events and updates state. No new SSE endpoint; the live view runs over its
own dedicated proxy at `/v1/sessions/{id}/browser/live/{path}` (HTTP for
NoVNC's static assets, WS upgrade for the framebuffer stream).

## 9. Events

New types added to `surogates/session/events.py`:

```python
class EventType(str, Enum):
    ...
    BROWSER_PROVISIONED = "browser.provisioned"
    BROWSER_DESTROYED = "browser.destroyed"
    BROWSER_CONTROL_GRANTED = "browser.control_granted"
    BROWSER_CONTROL_RETURNED = "browser.control_returned"
    BROWSER_RECORDING_STARTED = "browser.recording_started"
    BROWSER_RECORDING_STOPPED = "browser.recording_stopped"
```

`tool.call` and `tool.result` events for individual `browser_*` calls reuse
the existing event types. No special-casing in the event log; the SPA does
the grouping client-side based on consecutive tool names.

## 10. Security

| Concern | Mitigation |
|---|---|
| Browser pod scope | NetworkPolicy: ingress from worker + api-server only; egress unrestricted (browser needs internet) |
| Live-view auth | API server validates session JWT before opening WS; user can only view their own session's browser |
| Input gating | API server proxy drops input frames unless `BrowserControlStore` grants control to the connected user |
| Browser metadata integrity | API server reads BrowserRegistry by session id and verifies `org_id`/`user_id` before proxying; K8s fallback lookup uses pod labels |
| Profile secrets | Cookies/auth tokens stay in tenant Garage bucket; never in worker memory beyond the rsync window; never logged |
| Profile cross-tenant leak | Profile path is keyed by `org_id` + `user_id`; ProfileSync validates tenant scope before upload/download |
| Sandbox escape via browser | Browser pod has no DB/Redis credentials, no other tenant access; if compromised, blast radius is the user's profile + the live session |
| Recording PII | Recordings stored in session bucket (per-session isolation); user can opt out by not enabling recording |
| Resource exhaustion | Per-pod `activeDeadlineSeconds=3600` (matches existing sandbox); CPU/memory limits in pod spec |
| Governance | Every `browser_*` tool call goes through `GovernanceGate.check()`; tenants can deny specific tools via policy |

## 11. Resource sizing

Per `kernel-images/AGENTS.md`: headful image needs significant RAM for Xorg
+ Chromium + WebRTC. Initial pod spec:

```python
BrowserSpec(
    image="ghcr.io/onkernel/chromium-headful:stable",
    cpu="1",
    memory="2Gi",
    cpu_limit="2",
    memory_limit="4Gi",
    timeout=300,
    active_deadline_seconds=3600,
)
```

At 5000 enterprise users with 5–10% concurrently using browsers, expected
peak: ~250–500 browser pods. At 4 GiB limit each: ~1–2 TiB RAM cluster-wide
peak. Capacity planning lives outside this spec.

## 12. Configuration

```yaml
# config.dev.yaml — ProcessBrowserBackend (docker run)
browser:
  enabled: true
  backend: "process"
  image: "ghcr.io/onkernel/chromium-headful:stable"
  rest_port_base: 30000       # docker -p {base + N}:10001
  cdp_port_base: 31000        # docker -p {base + N}:9222
  live_view_port_base: 32000  # docker -p {base + N}:6080 (NoVNC v1)
  live_view_mode: "novnc"

# config.prod.yaml — K8sBrowserBackend
browser:
  enabled: true
  backend: "kubernetes"
  namespace: "surogates"
  service_account: "surogates-browser"
  image: "ghcr.io/onkernel/chromium-headful:stable"
  live_view_mode: "novnc"
  service_port: 443
  live_view_target_port: 6080
  rest_target_port: 10001
  pod_ready_timeout: 60
  active_deadline_seconds: 3600
  cpu: "1"
  memory: "2Gi"
  cpu_limit: "2"
  memory_limit: "4Gi"
```

When `browser.enabled` is false, browser tools are unregistered and don't
appear in the LLM's tool list at all.

## 13. Testing

Verifiable without a real K8s cluster or Chromium:

- `KernelBrowserClient` — unit tests against an httpx mock for every endpoint
  (request shape, response parsing, error handling).
- `BrowserPool` — provisioning logic, ensure-or-reprovision, destroy on
  session end, and registry writes/removals. Mock backend.
- `BrowserRegistry` — Redis read/write/delete, stale-entry handling, and K8s
  label fallback lookup.
- `BrowserControlStore` — acquire/release ownership checks, per-call tool
  short-circuit behavior, and wake enqueue on release.
- `ProfileSync` — upload/download round-trip with `LocalBackend` + a mock
  kernel-images `/fs/*` server.
- `K8sBrowserBackend` — pod manifest builder, status mapping (the same
  pattern as existing `tests/test_kubernetes_sandbox.py`).
- API server `/browser/control` control-store mutation, event emission.
- API server `/browser/state` reads BrowserRegistry and validates tenant scope.
- WS proxy frame gating — synthetic NoVNC frames in/out, assert input frames
  dropped unless control flag is set.
- `paused_by_user` short-circuit in browser tool handlers.
- `browser_screenshot` stores PNG artifact metadata or returns bounded base64
  in local/dev fallback.
- `browser_record_stop` calls stop, handles `202 Retry-After` from
  `/recording/download`, uploads MP4 artifact, and optionally deletes the
  remote recorder output.
- Frontend: storybook entries for the right-pane states, the take-control
  toggle, and the activity group collapse/expand.

Verifiable only end-to-end (deferred to manual + CI integration):

- Real Chromium responding to `browser_get_state` with a non-trivial a11y
  snapshot.
- NoVNC live view rendering in the SPA.
- Profile rsync round-trip preserving cookies across sessions.

## 14. Rollout

The work is large enough to break into independently shippable phases. Each
phase ends with a working subset behind a feature flag.

1. **Phase A — backend skeleton (COMPLETE).** `BrowserBackend` protocol +
   `ProcessBrowserBackend` (docker run) + `BrowserPool` + `BrowserRegistry`
   + `BrowserControlStore` + `KernelBrowserClient` + the discrete tools wired
   through `ToolRouter`. End state: agent can navigate, screenshot, click,
   type via dev backend; no live view UI, no profile, no recording.
2. **Phase B — Kubernetes backend.** `K8sBrowserBackend`, pod manifests,
   Service port mappings, labels, NetworkPolicy, ServiceAccount RBAC. End
   state: phase-A capabilities work in a K8s cluster and API can resolve pods
   through BrowserRegistry/K8s fallback.
3. **Phase C — UI: live view & thread rendering.** API server WS proxy,
   `/browser/state`, `/browser/control`. SPA right-pane stacked layout,
   activity group, take-control toggle. End state: user can watch and
   take over. Agent notifies user to take over if it needs login.
4. **Phase D — profile persistence & recording.** `ProfileSync`,
   `browser_record_start/stop`, `/recording/download` artifact upload. End
   state: logins persist across sessions; opt-in recording produces session
   artifacts.

## 15. Open questions / future work

- **WebRTC live view.** v1 ships NoVNC only (simpler proxy). WebRTC offers
  better latency and copy/paste; add when there's user demand or measured
  pain points with NoVNC.
- **Multi-tab support.** Kernel-images supports it via CDP `Target.*`; v1
  exposes only single-tab tools to keep the action surface tight. Add
  `browser_open_tab`, `browser_switch_tab`, `browser_close_tab` later.
- **Concurrent sessions per user with shared profile.** Last-writer-wins in
  v1. Revisit when a real complaint appears.
- **Browser-use compatibility.** Hermes' `browser_use` provider integrates
  with the browser-use library. We could wrap kernel-images CDP behind the
  browser-use API for users who already have prompts tuned for it. Not v1.
- **CAPTCHA solver provider integrations.** Out of scope; user takes over
  via live view in v1.
- **Pod warm pool.** Pre-provision N idle browser pods to amortize cold-start
  latency. Not v1; revisit when measured P95 first-action latency is a
  problem.
