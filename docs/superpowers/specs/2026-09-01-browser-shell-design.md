# Virtual Browser Shell — Design

**Status:** approved in brainstorming, not yet planned
**Date:** 2026-09-01

## Problem

The agent browser's live view is a noVNC pane onto the pod's whole X desktop:
`x11vnc` attaches to Xorg `:1`, `websockify` bridges RFB to a WebSocket, and
`BrowserLiveView` renders it. Two things are wrong with it.

**It is slow.** Measured against a real pod, an *idle* browser showing a static
page costs **1229 KB/s**, because `images/browser/supervisor/x11vnc.conf` runs
x11vnc with `-noxdamage` — no damage rectangles, so it polls and re-sends an
unchanging screen forever.

**It is over-privileged.** `RFBClientMessageGate` drops KeyEvent/PointerEvent
frames when the viewer does not hold the control lease, but a viewer who *does*
hold it is driving a desktop, not a browser: any URL scheme, devtools, the
filesystem, any application on the display. The widest exposure is the profile
setup dialog in Settings, where a human is deliberately handed control to log
into sites.

## Goal

Replace the desktop pane with a browser-shaped control — tab strip, address bar,
viewport — that renders the actual page content from the pod and exposes a
command surface small enough to enumerate on one screen.

## Decisions

Taken during brainstorming; each closed a fork in the design.

| Question | Decision |
|---|---|
| What may a human do? | **Watch, and when holding the lease, take over the page**: click, type, key, scroll, navigate, back, forward, reload. No tab lifecycle, no downloads, no file upload. |
| What does the tab strip do? | **Show every tab, click to switch which one you watch.** The agent owns opening and closing. One screencast at a time per connection. |
| Does profile setup move too? | **Yes.** Both consumers move and `x11vnc`/`websockify` leave the image. Half-migrating would leave the desktop reachable exactly where the exposure is widest. |
| What does the frontend speak? | **A narrow purpose-built protocol.** CDP terminates server-side. |

## Measurements

All from a real pod (`onkernel/chromium-headful`, Chrome 147), same page, same
activity. These are the numbers the design argues from; they are not estimates.

| State | CDP screencast (1280×800 q70) | RFB desktop | Ratio |
|---|---|---|---|
| Idle | **7.2 KB/s** (1 frame total) | **1229 KB/s** | ~170× |
| Scrolling Wikipedia | 258 KB/s | 3317 KB/s | ~13× |
| Frame ceiling | 25 fps, ~40 ms median gap | — | — |

Screencast cost by configuration, scrolling, real content:

```
uncapped jpeg q70   1328 KB/s   366 KB/frame   ← what you get without a cap
1280x800 jpeg q70    258 KB/s    74 KB/frame   ← chosen
1280x800 jpeg q50    140 KB/s    37 KB/frame
1280x800 png          35 KB/s    0.1 fps       ← unusable, PNG encode too slow
```

The native viewport is 1890×1984, so `maxWidth`/`maxHeight` must be set or every
frame is 366 KB. PNG is not a fallback; it is five times slower than the frame
budget allows.

Two contention questions were verified rather than assumed:

- **A second CDP session running a screencast does not perturb the agent.**
  `get_state` took 0.04/0.02/0.02s with no screencast and 0.01/0.02/0.01s while
  streaming, returning identical trees.
- **JavaScript dialogs are already auto-dismissed.** With our own `Page.enable`
  session attached, `Page.javascriptDialogOpening` arrives, then
  `javascriptDialogClosed` with `result=False`, and the page never blocks — the
  kernel-images-api's own connection answers first. This deleted a protocol verb
  (see *Dialogs* below).

## Why a narrow protocol rather than proxied CDP

The rejected alternative was to proxy CDP to the frontend behind a method
allowlist. Upstream's `server/lib/devtoolsproxy` even supplies a curated
38-method vocabulary that excludes every dangerous verb.

It was rejected because an allowlist has to be exhaustively right forever, and
one miss is total compromise: `Runtime.evaluate` in a page carrying the tenant's
captured login profile reads the cookies of every account that profile is signed
into. A purpose-built protocol is safe by construction instead — the frontend
cannot ask for JavaScript execution because no message expresses it. It also
keeps CDP's quirks (flat sessions, `sessionId` routing, frame acks) out of React.

Enforcing the vocabulary inside the image, by patching upstream's Go proxy, was
considered as defence in depth and **deferred**: port 9222 is a ClusterIP
service, so the runtime API is already the only route to it, and adding a Go
build to what is currently a thin apt-layer Dockerfile buys nothing today. It
remains the right hardening if the pod ever becomes reachable another way, and
nothing in this design precludes it.

## Architecture

```
React shell  ──WS──▶  /api/sessions/{id}/browser/shell   ──CDP──▶  pod :9222
 (typed msgs)          translation + control lease                  (ClusterIP)
```

| Component | Responsibility |
|---|---|
| `surogates/browser/cdp.py` *(new)* | Minimal CDP client: connect, `call(method, params, session)`, event subscription. |
| `surogates/browser/shell.py` *(new)* | Protocol types and the CDP translation. Pure — message in, CDP call out; frame in, message out. |
| `surogates/api/routes/browser.py` | New `shell` WebSocket endpoint beside `proxy_live_view_ws`. |
| `sdk/agent-chat-react/src/components/browser/browser-shell.tsx` *(new)* | Tab strip, address bar, viewport canvas. |

`cdp.py` exists because the repo has no CDP client: `KernelBrowserClient`
reaches CDP only *through* Playwright, by posting JavaScript to
`/playwright/execute`. The shell must speak it directly.

`shell.py` is separated from the route for the reason `serialize.py` is
separated from `client.py`: the mapping is pure and belongs under unit test
without a browser.

**A flat session is mandatory.** `Page.*`, `Runtime.*` and `Input.*` all return
`'Page.enable' wasn't found` on both the `/devtools/browser/…` and
`/devtools/page/…` endpoints, because upstream's `devtoolsproxy` fronts 9222 and
makes the page path behave like a browser session. Every page command must go
through `Target.attachToTarget({flatten: true})` and carry its `sessionId`.

### Shell chrome

Settled visually against three candidates ([canvas](https://claude.ai/code/artifact/3e9cda62-2a1e-4534-9f8a-8f2d52f79709)).
The pane today spends 84px on a header and a control bar; adding a tab strip
and an address bar naively would have made it 158px, a quarter of a 520×660
pane before any page pixels.

**One toolbar, 44px:** back, forward, reload, take control, URL field, overflow.
The pane header and the control bar both disappear into it; Close and Maximize
move behind `⋯`. The tab strip is a second 34px row that renders **only when a
second tab exists**, so the common case is 44px of chrome.

Two consequences that are decisions, not details:

- **Take control is an icon, so colour carries the mode.** It keeps the same
  pointer glyph in both states and fills amber when held — the existing control
  bar swaps to a counter-clockwise arrow for "Return control", which one slot
  from Reload would read as a second refresh button. Amber appears nowhere else
  in the toolbar, the viewport takes a 2px amber inset while control is held,
  and a "You have control · click to return" pill sits at the foot of the page
  area as the plain-language backstop.
- **The viewport jumps 34px when the strip appears.** Accepted: reserving the
  row permanently would give up most of what the single-tab case wins, and the
  shift is honest feedback that the agent opened a tab.

### Deletions

Once the shell ships: `browser-live-view.tsx`, the `@novnc/novnc` dependency
(declared in **both** `sdk/agent-chat-react/package.json` and
`web/package.json` — the api-image web-build installs only web's lockfile, so
both must go) and the `sdk/agent-chat-react/src/novnc.d.ts` type shim;
`surogates/browser/rfb.py` with `RFBClientMessageGate`; and `x11vnc.conf`,
`websockify.conf` and the `x11vnc websockify` apt line from
`images/browser/Dockerfile`. `images/browser/test_live_view_rfb.py` asserts
x11vnc and websockify are running, so it is deleted (or replaced by a shell
equivalent) in the same change as the image edit, not later.

Deleting x11vnc removes the *viewer*, not the display: Chrome still runs headful
in Xorg `:1`, and the agent's own `screenshot()` goes through Playwright's
`page.screenshot()` rather than the framebuffer, so no agent path is affected.

`BrowserEndpoint.live_view_url` and its registry field stay until the image
ships without websockify, then go. Registry entries outlive a deploy, so
removing the field first would break in-flight sessions.

## Protocol

One JSON object per WebSocket message, except frames.

**Frames are binary.** CDP delivers base64; the server decodes and sends the raw
JPEG as a binary WebSocket message. The measured figures above are already
decoded bytes, so this keeps the wire at 258 KB/s rather than reducing it —
forwarding CDP's base64 verbatim is what would inflate it, to roughly 344 KB/s.
Only one tab streams per connection, so a binary message is unambiguously a
frame of the attached tab and carries no header.

**Coordinates are normalized floats 0–1.** The client sends "38% across, 61%
down"; the server multiplies by the live viewport before dispatching. The server
owns the viewport truth, so a client that never learns the page is 1890×1984
cannot send a wrong pixel, and a viewport change mid-stream desyncs nothing.

```
client → server                            → CDP
  click       {x,y,button,count}             Input.dispatchMouseEvent
  scroll      {x,y,dx,dy}                    Input.dispatchMouseEvent mouseWheel
  type        {text}                         Input.insertText
  key         {key,mods}                     Input.dispatchKeyEvent ×2
  navigate    {url}                          Page.navigate    ← scheme-validated
  back / forward                             Page.navigateToHistoryEntry
  reload                                     Page.reload
  switch_tab  {id}                           Target.attachToTarget

server → client
  <binary>                                   Page.screencastFrame, decoded
  nav    {url,title,loading,canBack,canFwd}  Page.frameNavigated + history
  tabs   [{id,title,url,active}]             Target.targetCreated/Destroyed/
                                             InfoChanged
  lease  {held}                              control lease
  dialog {kind,message}                      Page.javascriptDialogOpening
```

`Input.insertText` carries typed text rather than per-character key events: it
handles IME, paste and non-ASCII correctly in one call, and unlike the
xdotool-based `/computer/type` endpoint it reaches contenteditable editors — the
same lesson `KernelBrowserClient.type_text` already documents. `key` covers the
keys that command a page rather than type into it.

### Dialogs

`dialog` is informational only. Verification showed a `confirm()` is
auto-dismissed as `false` by the kernel-images-api before a human could answer,
so a `dialog_reply` verb would be dead code and is not in the protocol. The
message exists so a profile-setup login that needs a dialog *accepted* fails
visibly rather than silently.

This is a real limitation: no human and no agent can accept a JavaScript dialog
in this browser. It is pre-existing, affects the agent identically today, and is
not introduced by this design.

### Security properties

| | Today | After |
|---|---|---|
| Human with lease reaches | the whole X desktop | 9 message types, 8 of them against one page target |
| Execute JavaScript | yes | no message expresses it |
| Read the cookie jar | yes | no message expresses it |
| Reach `file://` | yes | scheme allowlist rejects it |
| Idle cost | 1229 KB/s | 7 KB/s |

`navigate` is the one security-critical validation, because `Page.navigate`
renders `file:///etc/passwd` and executes `javascript:` URLs. The allowlist is
`http` and `https` only, enforced server-side.

**Unchanged, and stated deliberately:** a human holding the lease can still
navigate to any http(s) site *in a browser carrying the tenant's captured login
profile*, so those cookies remain reachable by visiting the site. That is what
take-over means; it is identical to today and this design does not fix it.
Anyone who can see the session likewise sees whatever the agent browses.

Two smaller measures: cap WebSocket message size, because an `Input.insertText`
carrying 50 MB is a trivial denial of service, and bound the command rate per
connection.

### Control and concurrency

The lease gate is a set membership test on the message type: command verbs
require the lease, `switch_tab` does not. This is the same rule
`RFBClientMessageGate` enforces, minus its 86 lines of stream reassembly —
those exist only because websockify's frames do not align with RFB message
boundaries, and one JSON object per message has no such problem.

Each WebSocket connection holds its own CDP attachment and screencast, so
switching what you watch never moves another viewer's view. On switch the server
stops the old screencast and drains before starting the new one, closing the
stale-frame race server-side rather than guarding it in React.

Input interleaving between agent and human is already handled by the existing
pause mechanism (`_paused_by_user_result`) and is reused unchanged.

## Error handling

| Failure | Behaviour |
|---|---|
| Pod dies, CDP socket drops | Close the client WS with a distinct code; the shell reports the browser is gone and does not reconnect. |
| Lease lost mid-session | Keep streaming frames, drop command messages, send `lease{held:false}`; the shell greys its controls. Watching never stops. |
| Watched tab destroyed | `Target.targetDestroyed` → re-attach to the active tab, push fresh `tabs`. |
| Client stops acking frames | Chrome stalls the stream by design; a watchdog restarts the screencast after a bounded wait. |
| Oversized or malformed message | Drop and count. Never decode the params of an unknown verb. |

## Testing

- **Unit, no browser.** `shell.py` message→CDP mapping, coordinate
  normalization, the lease gate's verb membership, and the `navigate` scheme
  allowlist with a rejection table covering `file:`, `javascript:`, `chrome:`,
  `data:`. The security-critical validation is tested here because it is pure.
- **`browser_e2e` against real Chromium.** Attach and stream; a click at a
  normalized coordinate lands where it should; a tab switch drains the old
  stream before the new one starts; navigate updates `nav`. The marker, fixture
  pattern and `data:` page technique already exist.
- **Not automated.** Visual fidelity and latency under load need a human
  looking at the result; asserting them in CI would be theatre.

## Known gaps

- **Screencast does not capture browser-native UI.** `<select>` dropdowns are
  the live one — no CDP method renders them, so the shell must read the DOM and
  draw its own, or accept the gap. File pickers and print dialogs are out of
  scope by the chosen command surface.
- **Profile-setup logins needing file upload or a download have no path.**
  Accepted knowingly when choosing to migrate both consumers. `DOM.setFileInputFiles`
  is available if this turns out to matter.
- **JavaScript dialogs cannot be accepted** by anyone, as above.
- **Image-level enforcement is deferred**, on the reasoning that 9222 is
  ClusterIP-only today.

## Open question for the plan

Whether `-noxdamage` can be dropped from `x11vnc.conf` as an interim measure,
before the shell lands. It is the direct cause of the 1229 KB/s idle poll and is
one line, but it was presumably set deliberately — XDAMAGE misses updates on
some drivers — so it needs verifying rather than deleting. This is independent
of the shell and could ship first.
