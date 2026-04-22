# Desktop Channel — Plan

## 1. Product positioning

The desktop channel is **a computer-use product**: a Tauri app that drives the user's
installed applications (Office suite, browsers, line-of-business systems) on the
user's behalf, with a chat UI on the side. It is **not** a web-wrapper of the existing
browser SPA — its purpose is native capabilities the browser cannot offer.

It is one channel among many (Web, Slack, Teams, Telegram, Desktop). The agent loop,
session state, and governance stay on the server exactly as they do for the other
channels. The desktop's uniqueness is that it is **also a sandbox backend** — tools
can execute on the user's machine, reached over the channel's own WebSocket.

## 2. Target platforms

- **Windows** and **macOS** only.
- Linux is **out of scope** for V1 (Wayland input injection is too unreliable to
  ship a computer-use product on; revisit only if demand justifies the investment).

## 3. Architecture

Three components, two edges:

```
┌────────────────────┐     WSS     ┌──────────────┐     HTTP    ┌────────┐
│   Tauri app        │────────────▶│  API server  │◀────────────│ Worker │
│   (user's laptop)  │  chat +     │              │   tool RPC  │        │
│                    │  tool RPC   │              │             │        │
└────────────────────┘             └──────────────┘             └────────┘
```

- **The desktop app never talks to the worker directly.** All traffic funnels
  through the API server: one hostname to allow-list, one TLS cert, one JWT.
- **The worker never talks to the desktop directly.** It calls an internal
  endpoint on the API server, which forwards frames over the open WebSocket.
- The worker's existing `SandboxPool` gains one more backend (`DesktopSandboxClient`);
  the API server gains one more WebSocket route.

### The "desktop sandbox" lives in the Tauri app

The thing that actually executes tools — the plugin host, the tool handlers, the
OS permissions mediator — is the Tauri app. The Python class on the worker named
`DesktopSandboxClient` is a thin RPC proxy (~80 LOC) that satisfies the `Sandbox`
protocol so the worker's `SandboxPool` can dispatch uniformly. It is not a sandbox;
it is a handle.

### No worker-on-desktop

We evaluated running the harness inside the Tauri app (Claude Code / Cursor model)
and rejected it: porting ~7000 LOC of `AgentHarness` to Rust or TS, running a second
harness implementation for messaging channels, weakening governance, and complicating
credential management would cost 4–6 months for gains (tool-call latency) that are
dwarfed by LLM inference time. The server worker stays where it is.

## 4. Code layout

```
apps/desktop/                          ← THE desktop sandbox
├── src-tauri/
│   ├── Cargo.toml
│   ├── tauri.conf.json
│   └── src/
│       ├── main.rs                    ← Tauri bootstrap, tray, global hotkey
│       ├── transport.rs               ← WebSocket client, reconnect, frame routing
│       ├── auth.rs                    ← JWT bootstrap, keychain
│       ├── plugin_host/
│       │   ├── mod.rs
│       │   ├── registry.rs            ← auto-registers handlers from plugins/
│       │   ├── dispatch.rs            ← RPC → handler lookup → execute
│       │   ├── oauth.rs               ← shared OAuth runner
│       │   └── credentials.rs         ← OS keychain (Keychain / Credential Manager)
│       ├── consent/
│       │   ├── mod.rs
│       │   ├── session_grant.rs       ← session-start capability grant
│       │   ├── overlay.rs             ← always-visible "agent active" bar
│       │   ├── panic_key.rs           ← Esc×3 global halt
│       │   └── sensitive_windows.rs   ← black-box password manager / banking
│       ├── handlers/                  ← built-in tools (not plugins)
│       │   ├── screenshot.rs
│       │   ├── input.rs               ← click, type, key, scroll
│       │   ├── windows.rs             ← enumerate, focus, AX tree
│       │   ├── file_ops.rs            ← read/write/patch rooted at user dir
│       │   └── terminal.rs            ← subprocess on host shell
│       └── audit.rs                   ← local SQLite replay log
└── src/                               ← reused from web/
    └── plugins/
        ├── PluginHost.tsx             ← renders plugin setup UIs
        └── registry.ts                ← auto-registers TS handlers

surogates/
├── sandbox/
│   ├── base.py                        ← Sandbox protocol (existing)
│   ├── pool.py                        ← add desktop branch in ensure()
│   ├── process.py                     ← existing
│   ├── kubernetes.py                  ← existing
│   └── desktop.py                     ← NEW — DesktopSandboxClient, ~80 LOC
│
├── api/routes/
│   └── desktop.py                     ← NEW — WebSocket + internal execute route,
│                                        ~150 LOC. In-memory socket dict per pod.
│
└── channels/
    └── desktop.py                     ← channel identity + session routing

plugins/                               ← repo top-level, platform-shipped only
├── office/
│   ├── plugin.yaml                    ← manifest (read by backend + frontend)
│   ├── handlers/                      ← TS and/or Rust, bundled into Tauri binary
│   ├── setup/                         ← React components for config UI
│   ├── skills/                        ← optional, loaded by backend PromptBuilder
│   └── experts/                       ← optional
├── salesforce/
├── hubspot/
├── workday/
└── sap/
```

## 5. Transport

### One WebSocket, multiplexed

The desktop holds **one** connection to the API server. Over it, both session
traffic and tool-execution RPCs flow as typed frames.

| Direction        | Frame type      | Purpose                                     |
|------------------|-----------------|---------------------------------------------|
| server → desktop | `tool.execute`  | Worker wants a tool to run on the desktop   |
| desktop → server | `tool.result`   | Handler finished; here's the output         |
| server → desktop | `session.event` | Assistant reply, tool progress (replaces SSE for this channel) |
| desktop → server | `user.message`  | User typed something                        |
| server → desktop | `session.state` | Session paused/resumed/ended                |
| both             | `heartbeat`     | Keep-alive                                  |

Binary frames used for screenshot payloads (WebP, ≤1280×800) and file blobs.
Text frames for everything else.

### V1 gateway is not a gateway

No Redis pub/sub, no ownership registry, no separate package. Two FastAPI routes
in [surogates/api/routes/desktop.py](surogates/api/routes/desktop.py):

```python
@router.websocket("/v1/desktop/connect")     # auth, register in dict, read forever
@router.post("/internal/desktop/{id}/execute") # lookup dict, send frame, await reply
```

In-memory dict per pod. Single API replica holds all sockets. When the API restarts,
desktops reconnect — a 2-second blip. Good enough for dev, staging, and the first
year of production.

### When HA becomes necessary

Only when >1 API replica is needed. Add then:
- Redis key `desktop:{id} → pod_ip` on connect
- Worker does one Redis GET, then direct HTTP to the owning pod
- Or Redis pub/sub for request/reply if pod-to-pod HTTP is undesirable

~100 LOC of additions, not a redesign.

## 6. Session-to-desktop binding

`sessions` table gains two columns:

```sql
ALTER TABLE sessions
  ADD COLUMN sandbox_mode TEXT NOT NULL DEFAULT 'auto',   -- 'auto'|'process'|'k8s'|'desktop'
  ADD COLUMN desktop_id UUID REFERENCES desktops(id);     -- NULL for non-desktop sessions
```

New `desktops` table tracks registered desktops per user:

```sql
CREATE TABLE desktops (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL REFERENCES users(id),
    name            TEXT,                   -- "Flavius's MacBook"
    platform        TEXT NOT NULL,          -- 'windows' | 'macos'
    app_version     TEXT NOT NULL,
    last_seen_at    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

Binding is **sticky and picked at session creation**. A user with two desktops
chooses one (defaults to the connecting desktop). If the chosen desktop goes
offline mid-session, the harness catches `DesktopOffline`, session pauses,
resumes on reconnect. Sessions never hop between desktops mid-flight.

## 7. Tool surface — four layers

Layered so each higher tier is optional; the agent falls back to the layer below.

| Layer | Tools                                                                 | Reliability                                   | Scope          |
|-------|------------------------------------------------------------------------|-----------------------------------------------|-----------------|
| **L1** Vision + input (Anthropic `computer` parity) | `screenshot`, `click`, `double_click`, `right_click`, `mouse_move`, `scroll`, `type`, `key`, `wait`, `cursor_position` | Works everywhere, fragile on dynamic UIs | MVP, ~3–4 weeks |
| **L2** Accessibility tree                           | `window_tree`, `list_windows`, `focus_window`, `find_element` (role/name) | Rock-solid where supported, huge token savings | ~3–4 weeks |
| **L3** Browser (CDP)                                | `browser.goto`, `browser.dom`, `browser.click_selector`, `browser.eval`, `browser.screenshot`, `browser.download` | Surgical, fast, auditable | ~2–3 weeks |
| **L4** Office / enterprise apps (plugins)           | `excel.*`, `word.*`, `outlook.*`, `powerpoint.*`, `salesforce.*`, etc. | Industrial-strength where it applies | ~1 month per plugin |

L1 is the fallback that always works. L2–L4 are shortcuts the agent picks when
available. Ship L1 in MVP; add L2–L4 on demand.

## 8. Plugins — platform-shipped, frontend-only

### Rules
- **Only the platform team ships plugins.** No marketplace, no org upload, no
  third-party submission. Plugins live in the repo under `plugins/`.
- **Plugins are frontend-only.** All tool handlers run in the Tauri app. No
  Python plugin handlers. No backend plugin runtime. No `surogates/plugins/`
  Python package.
- **Plugins require the desktop channel.** Tool handlers exist only in the
  Tauri binary. Slack/Teams/Telegram sessions cannot invoke plugin tools.
- **Customer credentials never touch the platform backend.** OAuth flows run
  in the Tauri app; tokens live in OS keychain.

### Manifest (`plugin.yaml`)

```yaml
name: salesforce
version: 1.2.0
display_name: Salesforce CRM
description: Query and modify Salesforce opportunities, accounts, contacts
vendor: surogates-platform

tools:
  - name: salesforce.query
    description: Run a SOQL query
  - name: salesforce.create_opportunity
skills:                       # optional, backend-shipped
  - path: skills/salesforce-best-practices/SKILL.md
experts:                      # optional
  - path: experts/salesforce-writer/SKILL.md

config_schema:                # JSON Schema, rendered as form in Tauri
  type: object
  properties:
    instance_url: { type: string, format: uri }
credentials:
  - name: oauth
    type: oauth2
    flow: authorization_code
    provider_config:
      authorize_url: https://login.salesforce.com/services/oauth2/authorize
      scopes: [api, refresh_token]

policies:
  default_scope: org_opt_in   # auto | org_opt_in | user_opt_in
  capabilities: [crm.read, crm.write]
  data_classification: confidential
```

Backend reads manifests at startup (tool schemas, skills, experts). Tauri build
bundles handlers + setup UIs into the binary.

### Enrollment

One small DB table:

```sql
CREATE TABLE plugin_enrollments (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id          UUID NOT NULL REFERENCES orgs(id),
    user_id         UUID REFERENCES users(id),    -- NULL = org-wide
    plugin          TEXT NOT NULL,                -- references static catalog
    enabled         BOOLEAN DEFAULT true,
    always_active   BOOLEAN DEFAULT false,
    UNIQUE (org_id, user_id, plugin)
);
```

No `config` or `credential_refs` columns — both live on the desktop.

### Progressive disclosure

With many plugins, tool context explodes. The agent sees a `list_plugins` tool
and a one-sentence description of each enrolled plugin; calling `activate_plugin`
loads that plugin's tool schemas into subsequent turns. Mirrors the existing
skill progressive-disclosure pattern.

### OAuth model

- **V1**: platform-owned OAuth app per plugin (one "Surogates for Salesforce"
  shared across all customers). Simple, fast onboarding.
- **Enterprise option (later)**: customer brings their own OAuth client_id /
  client_secret via plugin config. ~2 days of schema work when the first deal
  requests it.

## 9. Consent & security model (computer-use specifics)

Per-click prompts die under computer-use volume (50–200+ tool calls per session).
Consent is session-level plus continuous supervision:

- **Session start**: single explicit grant — "This session can see your screen
  and control your mouse/keyboard for up to N minutes. Stop with Esc×3."
- **Always-visible overlay**: unmissable bar at top of screen, "Agent active —
  [task summary] — [Stop]". Non-modal, non-hideable.
- **Cursor trail**: 150–300ms animated target before each click. Buys the user
  time to panic-stop and aids debugging.
- **Sensitive-window registry**: bundle-IDs / title patterns for password
  managers, banking apps, etc. Screenshots of those windows are black-boxed
  at capture time, never hit the wire. Ship with a default list, user-extensible.
- **Panic key**: global `Esc×3` within 1 second halts all tool execution.
- **Replay log**: local SQLite, `(timestamp, tool, args, screenshot_hash)` per
  call. Also best post-incident forensics.
- **Session end**: summary of actions, capabilities auto-revoked. Grants do
  not persist across sessions by default.

Governance still runs server-side (AGT `PolicyEngine` in `GovernanceGate`) —
desktop consent is a **second** gate, not a replacement. Both must pass.

## 10. OS permissions

### macOS — first-run shepherd required

Three separate grants in System Settings → Privacy & Security, each must be
detected and requested:

- **Accessibility** — mouse/keyboard control. `AXIsProcessTrustedWithOptions`.
- **Screen Recording** — screenshots. `CGPreflightScreenCaptureAccess`.
- **Input Monitoring** — `Esc×3` panic key capture. `IOHIDCheckAccess`.

Flow: detect each, open the exact Settings pane, handle the app-restart-after-grant
requirement (macOS needs relaunch for AX changes to take effect). Budget a week
for this alone.

### Windows — lighter but signing-sensitive

- Most AX/UIA works without elevation.
- UAC required for specific operations (BlockInput, global input hooks).
- Windows Defender will flag unsigned binaries doing input injection. EV cert
  plus SmartScreen reputation reduces friction substantially.

## 11. Distribution

| Platform | Bundle         | Signing                                              | Auto-update                 |
|----------|----------------|------------------------------------------------------|-----------------------------|
| Windows  | `.msi` / `.exe`| EV code-signing cert (~$400/yr, Sectigo/DigiCert)    | Tauri `updater` plugin      |
| macOS    | `.dmg` + `.app`| Apple Developer ID ($99/yr) + **notarization** (mandatory 10.15+) | Tauri `updater` plugin      |

CI: GitHub Actions matrix (Tauri has an official action). **Procure certs
before implementation starts** — Apple notarization and Windows EV cert
issuance can each take days to a week. Don't block a release on paperwork.

## 12. Phased scope

### Phase 1 — Desktop channel MVP (8–10 weeks, 1 engineer)

- Tauri app boots on Win + macOS, embeds `web/dist`, JWT auth
- Persistent WebSocket to API server, auto-reconnect
- `DesktopSandboxClient` + `sandbox_mode` / `desktop_id` on sessions
- L1 tools (screenshot, click, type, key, scroll)
- Session-start consent + always-visible overlay + panic key
- macOS permissions first-run flow
- Basic code signing, internal distribution
- Smoke test: Claude 4.7 runs a computer-use task end-to-end

**Exit criterion**: agent can open a browser, search for something, and click
a result on a fresh Win + Mac install, with consent flow passing.

### Phase 2 — Reliable computer-use (6–8 weeks)

- L2 (accessibility tree — `window_tree`, `find_element`)
- L3 (CDP browser — `browser.goto`, `browser.dom`, `browser.click_selector`)
- Sensitive-window registry
- Replay log (local SQLite)
- Auto-update
- Production signing + notarization
- Screenshot optimization (WebP, downscale, budget)

### Phase 3 — Plugin framework (~6 weeks)

- `plugins/` repo convention + manifest loader
- `plugin_enrollments` table + REST API
- `ToolLocation.DESKTOP_PLUGIN` + router integration
- Progressive disclosure (`list_plugins` / `activate_plugin`)
- Tauri plugin host (registry, dispatch, OAuth runner, credential store)
- Plugin SDK (TS + Rust scaffolds)
- Admin UI (enroll, always-active toggle)
- User UI (per-user enable, OAuth connect)
- Audit tagging (`plugin_id` on events)

### Phase 4 — First-party plugins

Shipped incrementally, each ~1 month average. Recommended order:

1. **Office** (4–6 weeks) — COM on Windows, AppleScript on macOS, the flagship
2. **Salesforce** (1–2 weeks) — REST + OAuth, all TypeScript
3. **HubSpot** (1–2 weeks) — same shape as Salesforce
4. **Workday** (2–3 weeks) — REST + ISU auth model
5. **SharePoint / OneDrive** (2 weeks) — Graph API + MSAL
6. **SAP** (4–6 weeks) — OData works; RFC/BAPI requires a companion Java
   process if needed; GUI scripting on Windows via Rust+COM

## 13. Open decisions (flag before Phase 1)

- **Per-user vs org-wide OAuth for plugins** — default per-user, allow org-wide
  for service-account-style plugins (SAP).
- **Always-active plugins** — e.g., mandate Office on desktop sessions (bypasses
  progressive disclosure). Config on `plugin_enrollments.always_active`.
- **Multi-monitor + DPI** — L1 tools must handle per-monitor DPI scaling on
  Windows and Retina on macOS correctly from day one. Cheap if designed in,
  painful to retrofit.
- **LLM model gating** — `computer_use: true` capability flag in
  `model_metadata.py`; desktop sessions refuse non-vision models.
- **Session iteration budget** — raise default for desktop sessions (Hermes
  default ~100; computer-use sessions routinely hit 200+).
- **Screenshot storage** — `tool.result` screenshots must not inline into
  `events.data`. Write to `session-{id}` Garage bucket, store only the S3 key
  in the event. Reuse `tool_result_storage` truncation pattern.

## 14. What not to build

- **No worker-on-desktop.** Harness stays server-side.
- **No Redis pub/sub / ownership registry in V1.** Add only when >1 API replica
  is actually needed.
- **No separate desktop gateway deployment.** It's two FastAPI routes.
- **No dynamic desktop plugin loading.** Plugins bundle into the Tauri binary
  via Cargo features. "Dynamic plugins" is a maybe-V2 problem.
- **No plugin marketplace.** Platform-shipped only.
- **No web-channel plugin host (V1).** Plugins are desktop-only. Revisit only
  if demand proves.
- **No Linux support.**
- **No mobile support.** (iOS/Android agents are a different product category.)
