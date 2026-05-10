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
- **Live view** on port `443` (NoVNC and WebRTC).

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

A new `BrowserPool` (sibling to `SandboxPool`) maps `session_id → browser_pod`.
Lazy provisioning on the first `browser_*` tool call; destroyed on session
end. Browser tools run **harness-local** (in the worker process) and call the
browser pod's REST API over the cluster network via a thin Python client.

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
│  │ - profile rsync on provision/teardown │                      │
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
│  │    Live view (NoVNC/WebRTC) on :443                          │
│  │    /home/kernel/profile/  ← user-data-dir                    │
│  │                                                              │
│  Service: browser-{id[:12]}.surogates.svc                       │
│  NetworkPolicy: ingress only from worker + api-server           │
└─────────────────────────────────────────────────────────────────┘
                  ▲
                  │ wss://...   (live view)
                  │
┌─────────────────────────────────────────────────────────────────┐
│ API Server                                                       │
│  GET /v1/sessions/{id}/browser/state ← provision status         │
│  WS  /v1/sessions/{id}/browser/live  ← live-view WS proxy       │
│  POST /v1/sessions/{id}/browser/control ← acquire/release      │
└─────────────────────────────────────────────────────────────────┘
                  ▲
                  │ wss + SSE
                  │
              [Web Chat UI]
```

The user's browser only ever talks to the API server. The browser pod stays
on the cluster network, reachable from worker and api-server only.

### 4.2 New module layout

```
surogates/browser/
├── __init__.py
├── client.py        # KernelBrowserClient — httpx wrapper around kernel-images REST API
├── pool.py          # BrowserPool — session_id → pod_url + lifecycle
├── kubernetes.py    # K8sBrowserBackend — provisions browser pods
├── process.py       # ProcessBrowserBackend — runs kernel-images via docker for dev
├── profile.py       # ProfileSync — rsync tenant profile to/from S3 via /fs/upload_zstd
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

1. Calls `BrowserPool.ensure(session_id, spec)` — provisions on first call.
2. Builds a `KernelBrowserClient` bound to the pod URL.
3. Issues the corresponding REST call.
4. Returns the result as a JSON string (the standard tool-result shape).

This keeps the existing `SandboxPool` logic untouched. The browser pod is a
separate resource with a separate lifecycle.

## 5. Tool surface

Discrete tools, one per kernel-images REST endpoint we expose. Each is
governance-checked like any other tool.

| Tool | kernel-images endpoint | Purpose |
|---|---|---|
| `browser_navigate` | `/playwright/execute` (`page.goto`) | Navigate to URL; returns final URL + page title |
| `browser_get_state` | `/playwright/execute` (a11y snapshot) | Default perception: structured a11y tree with `@e1`-style refs, plus URL and title |
| `browser_screenshot` | `/computer/screenshot` | PNG of viewport (or region); on-demand vision escape hatch |
| `browser_click` | `/computer/click_mouse` | Click by coords or by `@ref` (resolved via cached a11y snapshot) |
| `browser_type` | `/computer/type` | Type text; supports `@ref` to focus first |
| `browser_press_key` | `/computer/press_key` | Press named key(s) — Tab, Enter, ctrl+l, etc. |
| `browser_scroll` | `/computer/scroll` | Scroll at coords by delta |
| `browser_drag` | `/computer/drag_mouse` | Drag along a path |
| `browser_wait` | (in-process sleep) | Wait N ms — for animations / async loads |
| `browser_record_start` | `/recording/start` | Begin mp4 capture |
| `browser_record_stop` | `/recording/stop` | Stop mp4 capture; uploads recording artifact to session bucket |
| `browser_close` | (BrowserPool.destroy_for_session) | Explicit close before session end |

Tools land in `surogates/tools/builtin/browser.py` (one file, single
responsibility — registrations + handlers thin enough to fit). Each handler
delegates to `KernelBrowserClient`. The `@ref → coords` resolution lives in
the client since it caches the last a11y snapshot per browser pod.

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

## 7. Live view — WebSocket proxy

The API server acts as a WebSocket proxy between the SPA and the browser
pod's port 443.

### 7.1 API endpoints

```
GET  /v1/sessions/{id}/browser/state
  → 200 { status: "live"|"provisioning"|"closed", url, title, control_owner }
  → 404 if no browser provisioned

WS   /v1/sessions/{id}/browser/live
  ← Upgrade with session JWT
  → API server validates JWT
  → API server resolves browser pod via BrowserPool
  → API server opens upstream WS to browser-{id[:12]}.surogates.svc:443
  → Bidirectional pump
  → Honor browser_user_control flag for input frames

POST /v1/sessions/{id}/browser/control
  body: { action: "acquire" | "release" }
  → 200 — emits BROWSER_CONTROL_GRANTED / BROWSER_CONTROL_RETURNED
```

### 7.2 Read-only enforcement

Kernel-images supports container-level read-only via `ENABLE_READONLY_VIEW`
but lacks a runtime toggle. We start the container in interactive mode and
gate input at the proxy:

- Outbound (pod → user) frames are always forwarded.
- Inbound (user → pod) frames carrying input events are dropped unless the
  session's `browser_user_control` flag is true.

For NoVNC, "input frames" are RFB messages with types `KeyEvent (4)` or
`PointerEvent (5)`. For WebRTC, input goes over a separate data channel that
we gate at the data-channel multiplexer. We start with NoVNC for v1
(simpler proxy) and add WebRTC in a follow-up.

### 7.3 Control flow

User-control acquisition:

1. SPA POSTs `/v1/sessions/{id}/browser/control { action: "acquire" }`.
2. API server sets session-level flag `browser_user_control: true`. Emits
   `BROWSER_CONTROL_GRANTED` event.
3. WS proxy starts forwarding input frames.
4. Worker's next `browser_*` tool call short-circuits with
   `{"error": "paused_by_user", "guidance": "..."}`.
5. A one-time system-injected message ("The user has taken control of the
   browser. Wait for them to finish before continuing.") is prepended to the
   next LLM iteration so the model doesn't keep retrying.

User-control release:

1. SPA POSTs `{ action: "release" }`.
2. API server clears flag, emits `BROWSER_CONTROL_RETURNED`, queues a wake
   on the session via the orchestrator's Redis queue.
3. Worker resumes naturally on next iteration; browser tools succeed again.

The worker is **not suspended** — it just gets `paused_by_user` from any
browser tool while the user has control. Same pattern as
`sandbox_unavailable_result` (`surogates/sandbox/base.py`).

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
own dedicated WS at `/v1/sessions/{id}/browser/live`.

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
| Input gating | API server proxy drops input frames unless `browser_user_control` is true |
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
  port_base: 30000   # docker -p {port_base + N}:443

# config.prod.yaml — K8sBrowserBackend
browser:
  enabled: true
  backend: "kubernetes"
  namespace: "surogates"
  service_account: "surogates-browser"
  image: "ghcr.io/onkernel/chromium-headful:stable"
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
  session end. Mock backend.
- `ProfileSync` — upload/download round-trip with `LocalBackend` + a mock
  kernel-images `/fs/*` server.
- `K8sBrowserBackend` — pod manifest builder, status mapping (the same
  pattern as existing `tests/test_kubernetes_sandbox.py`).
- API server `/browser/control` flag mutation, event emission.
- WS proxy frame gating — synthetic NoVNC frames in/out, assert input frames
  dropped unless control flag is set.
- `paused_by_user` short-circuit in browser tool handlers.
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

1. **Phase A — backend skeleton.** `BrowserBackend` protocol +
   `ProcessBrowserBackend` (docker run) + `BrowserPool` + `KernelBrowserClient`
   + the discrete tools wired through `ToolRouter`. End state: agent can
   navigate, screenshot, click, type via dev backend; no live view, no
   profile, no recording.
2. **Phase B — Kubernetes backend.** `K8sBrowserBackend`, pod manifests,
   NetworkPolicy, ServiceAccount RBAC. End state: phase-A capabilities work
   in a K8s cluster.
3. **Phase C — UI: live view & thread rendering.** API server WS proxy,
   `/browser/state`, `/browser/control`. SPA right-pane stacked layout,
   activity group, take-control toggle. End state: user can watch and
   take over.
4. **Phase D — profile persistence & recording.** `ProfileSync`,
   `browser_record_start/stop`, recording artifact upload. End state:
   logins persist across sessions; opt-in recording produces session
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
