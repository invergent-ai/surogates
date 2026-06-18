# VNC-over-WebSocket browser live view — design

- **Date:** 2026-06-18
- **Status:** Approved (design) — pending implementation plan
- **Scope:** Managed-agent browser "live view + take control" transport
- **Repos:** `surogates` (image, harness, SDK) + `surogate-ops` (live-view proxy)

## Problem

The managed-agent browser ("take control") live view currently uses **neko/WebRTC**
(`onkernel/chromium-headful`, neko on `:8080`). Two independent failures:

1. **WebRTC media has no path to the user's browser.** neko streams video over
   WebRTC/UDP (`epr=59000-59100`, `nat1to1=<node-public-ip>`), but the fleet pods
   expose only TCP (`8080`/`10001`/`9222`), with no `hostNetwork`/`hostPort`/NodePort
   and no TURN server. Signaling (TCP/443 via the ops proxy) connects and neko
   negotiates a peer, but media never establishes — across all browser pods over 24h,
   `set video` attempts > 0 and successful ICE connections = 0. Symptom: the browser
   console shows the live-view WebSocket "failed", the preview PNG (HTTP) still works.
   Fixing WebRTC would require operating a TURN server (UDP relay) — new infra.

2. **Google blocks login when CDP is attached.** Take-control exists so a human can
   perform logins the agent cannot (Google, etc.). The documented, community-confirmed
   behavior: `accounts.google.com` shows "this browser or app may not be secure" when
   an automation/CDP surface is attached; the fix is to log in with **no CDP attached**,
   then attach. A CDP-screencast live view (otherwise the industry norm — Browserbase,
   Browserless, Steel) cannot satisfy this: its video and input *are* CDP.

## Goals

- Live view + take-control that works through the existing TCP proxy chain — **no TURN,
  no UDP, no new network infra.**
- A **CDP-free login window**: during human control, chromium has no automation/CDP
  surface for Google to detect.
- Adequate interactivity for the real workload (**logins and forms** — small, localized
  screen updates; not video/animation).

## Non-goals

- High-fidelity / high-FPS / video co-browsing (WebRTC's strength; not the workload).
- Audio.
- Solving Google detection end-to-end. VNC is **necessary but not sufficient**; clean
  launch flags, profile reuse, and residential egress IP are separate, orthogonal levers
  (see "Out of scope").

## Decision

Use **VNC / RFB-over-WebSocket**: an in-pod VNC server attached to chromium's existing X
display, bridged to a WebSocket on `:8080` (the port the harness already expects as
`live_view_url`), rendered in the SDK by a noVNC canvas.

| Transport | No TURN/UDP | CDP-free login | Fit for logins/forms |
|---|---|---|---|
| WebRTC (neko, today) | ✗ needs TURN | ✓ | overkill |
| CDP screencast | ✓ | ✗ (CDP always attached) | good, but fails login requirement |
| **VNC / RFB-over-WS** | ✓ pure TCP | ✓ X-level, zero CDP | **fits** |

WebRTC's only edge is smoothness for motion-heavy content; the image already runs neko at
`1280x720@10` (cost-reduced), so that edge is largely unused, and in this topology WebRTC
must relay through TURN anyway. For logins/forms, VNC's changed-rectangle updates are
responsive.

## Verified facts (PROD, 2026-06-18)

- chromium is launched **clean**: `chromium --remote-debugging-port=9223
  --remote-allow-origins=* --user-data-dir=/home/kernel/user-data --password-store=basic
  --no-first-run --no-sandbox` on a **real Xorg** `DISPLAY=:1`. **No `--enable-automation`,
  no `--headless`** ⇒ `navigator.webdriver` is already **false**.
- **No persistent CDP**: an idle browser pod has **0** established connections to the CDP
  port. kernel-images-api attaches CDP transiently per `POST /playwright/execute`; between
  calls nothing is attached.
- The harness holds no CDP of its own — the agent drives chromium via kernel-images-api
  REST (`/playwright/execute`, `/computer/*`).
- `control.py` acquire/release is a Redis lock; it touches CDP not at all today.
- Browser tools already short-circuit while the Redis control lock exists:
  `_resolve_session_browser` returns `paused_by_user` before `browser_pool.ensure`, and
  `browser_close` has the same lock check. The harness also injects a one-time pause
  notice telling the agent to wait.

**Consequence:** a CDP-free login window needs no forced CDP teardown. It needs only that
**all browser/CDP entry points keep honoring the existing control lock** so the agent
issues no `/playwright/execute` calls while a human holds control. Combined with the
already-clean launch flags, the login window is clean.

## Architecture

```
human ─ noVNC canvas (SDK)
      ─wss /api/sessions/{id}/browser/live/… ─ ops `websocket_live_browser` (token auth)
      ─ runtime/harness WS proxy (control-required stream; RFB input gating as defense-in-depth)
      ─ websockify :8080 (pod) ─ x11vnc ─ Xorg :1 ◀ chromium
```

Pure TCP end-to-end over the existing proxy chain. Human input is RFB Key/Pointer at the X
level — **zero CDP in this path**. The agent keeps using kernel-images-api/CDP for
automation, suspended only while control is held.

## Component changes

### 1. Browser image — `images/browser/Dockerfile`
- Disable neko (it currently `autostart=true` via a Dockerfile `sed`; revert to false /
  remove from supervisord). Frees `:8080`.
- Add a VNC server: **`x11vnc`** attached to `DISPLAY=:1` (Tight/JPEG encoding, localhost
  only, passwordless — the WS layer enforces auth), plus **`websockify`** bridging
  `:8080` → the local VNC port. Both as supervised services.
- Keep chromium, Xorg, mutter, kernel-images-api, envoy unchanged.
- Optional cleanup: drop the unused `chromedriver` service.
- Remove neko-branding asset rewrites (`/var/www/...`) that no longer apply.

### 2. Harness — `surogates/` (mostly already built)
- `api/routes/browser.py` already proxies an RFB-over-WS upstream with input-frame gating
  (`_should_forward_client_frame` → `rfb.is_input_frame` types 4/5/6 forwarded only to the
  control holder). The live stream itself already requires browser control; non-holders
  should keep using `preview.png`, not a view-only RFB session. Verify against a real
  websockify upstream; **harden input gating for WS-frame vs RFB-message boundaries**
  (websockify may split/coalesce the RFB byte stream across WS frames — parse the RFB
  client stream rather than assuming one WS frame = one RFB PDU).
- Keep `live_view_url = ws://<svc>:443 → :8080` (`kubernetes.py`
  `SERVICE_PORT_LIVE_VIEW=443`, `TARGET_PORT_LIVE_VIEW=8080`).
- **Verify and preserve the existing agent suspend path:** browser tools currently reject
  with `paused_by_user` while `browser_control.get(session)` is set, avoiding
  `browser_pool.ensure` and therefore avoiding `/playwright/execute`. Cover every
  browser/CDP call path in tests, including screenshot/state/close and any direct
  `KernelBrowserClient` usage. Current behavior is reject-with-guidance, not queue.
  Release and TTL expiry naturally resume the tools.

### 3. Ops live-view proxy — `surogate-ops` `surogate_ops/server/routes/sessions.py`
- **Delete** the neko-specific iframe plumbing: `_inject_ws_token_interceptor` /
  `_LIVE_VIEW_WS_INTERCEPTOR`, the HTML-rewrite branch in `get_live_browser_asset`, and the
  neko static-asset proxying. The SDK opens the RFB WS directly; there is no iframe HTML to
  rewrite.
- **Keep**: `websocket_live_browser` (RFB WS proxy), `verify_ws_token` auth, the
  `browser/state`, `browser/control`, `browser/preview.png`, and `DELETE /browser` routes.

### 4. SDK — `sdk/agent-chat-react`
- Replace the `<iframe>` in `components/browser/browser-live-view.tsx` with a
  **`@novnc/novnc`** RFB canvas connecting to the live-view WS only after
  `hasUserControl`; the server also enforces this and closes non-holder streams. Drop
  `pwd=admin` from `browserLiveViewUrl` (`work-agent-chat-adapter.ts`); keep the `token`
  query param for WS auth.
- `browser-pane.tsx` control gating (`canUseLiveView = hasUserControl && liveViewUrl`)
  stays.

## Control flow (existing, unchanged)

`POST /browser/control {acquire|release}` → Redis lock (`control.py`). Live view requires
control (`_ensure_live_view_control` for HTTP and equivalent WS checks); non-holders get
preview snapshots instead of an RFB stream. Input-frame gating remains defense-in-depth
inside the WS proxy. Agent browser tools already reject with `paused_by_user` while the
same lock is held, and resume after release or TTL expiry.

## Testing

- **Unit:** RFB input-frame gating, including split/coalesced WS frames (new boundary
  handling).
- **Integration:** noVNC client through the full proxy chain (ops → runtime → websockify →
  x11vnc): frames render for the control holder; non-holders cannot open the RFB stream
  and still receive preview snapshots.
- **Agent-suspend:** while control is held, browser tools return `paused_by_user` and do
  not call `/playwright/execute`; resume on release or TTL expiry.
- **Acceptance:** a human take-control session completes a real Google login (no CDP
  attached during the login), then the agent resumes and observes the logged-in state.

## Open questions / risks

- **RFB framing over WS** — confirm websockify's framing and whether the harness must
  buffer/parse the RFB stream to gate input correctly. (Mitigation in §2.)
- **Agent-suspend coverage** — the mechanism exists in browser-tool preflight and
  `browser_close`; confirm there are no direct browser/CDP paths that bypass it, and ensure
  the agent loop tolerates `paused_by_user` gracefully (the harness already injects a
  browser-pause notice and waits on takeover).
- **x11vnc perf** — tune encoding/quality at `1280x720`; validate interactive latency on a
  login form through the full proxy chain.

## Out of scope (separate workstreams)

Google detection beyond CDP: clean launch flags (already clean), **profile reuse** (S3
`USER_DATA_DIR` sync already present — "log in once, persist cookies, reuse"), and a
**residential/mobile egress IP** (the fleet currently egresses via Hetzner datacenter IPs,
the dominant Google-login blocker). These are orthogonal to the live-view transport.

## References

- Investigation: `surogate-ops` PROD session `37806eff-…` (WebRTC media never connects; no
  TURN; chromium clean-launch + no persistent CDP).
- Google login + CDP: berstend/puppeteer-extra #822; Sunwood-ai-labs/logged-in-google-chrome-skill.
- Industry live-view norms: Browserbase Live View, Browserless Live Debugger (CDP screencast).
