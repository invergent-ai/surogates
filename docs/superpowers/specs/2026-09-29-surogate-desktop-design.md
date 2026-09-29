# Surogate Desktop — Design

**Status:** approved in brainstorming, not yet planned
**Date:** 2026-09-29

## Problem

Agent users reach an agent only through the web client at `<slug>.<domain>` or
a messaging channel. Everything the agent does with files, commands and the
browser happens in a cloud sandbox pod whose workspace is an S3 prefix. A user
who wants an agent to work on files that live on their own computer has to
upload them, and download the results, one at a time. The agent's browser is a
cloud browser, logged in to nothing the user uses.

## Goal

A desktop app for agent users, in the style of Claude Cowork. The user picks a
folder on their computer and the agent works in it: it reads and writes files
there, runs commands there, and drives a browser installed on that computer.
The agent's reasoning stays in the cloud, so it is the same agent, with the
same models, skills, memory, knowledge bases, MCP tools, governance and
billing, that the user reaches in the web client.

## Non-goals for the first release

- Windows and macOS. Linux first, Windows second, macOS last. Nothing in the
  first release may depend on a macOS-only or Windows-only mechanism.
- Agent builders. Studio stays a web app; the desktop app is for people using
  agents.
- Running the harness, the LLM loop or a local model on the laptop.
- Any access to a local session's files while the computer is offline.
- `.deb`, `.rpm` or AppImage packages. The app installs with a script.

## Decisions

Taken during brainstorming; each closed a fork in the design.

| Question | Decision |
|---|---|
| Who is it for? | **Agent users only.** |
| What runs where? | **Reasoning in the cloud, hands on the laptop.** File, terminal and browser tools act on the user's computer; every other tool stays in the cloud. |
| New UI or the existing web client? | **The existing web client, loaded from the agent's URL** in an Electron window, plus a small bridge. No second chat UI. |
| First platforms | **Linux, then Windows, then macOS.** |
| Local isolation | **Anthropic Sandbox Runtime (`srt`)**, used as a Node library (`@anthropic-ai/sandbox-runtime`). |
| Workspace | **The local folder is the session's only workspace.** Uploads, artifacts, the whiteboard and research notes live there. |
| Folder binding | **One folder per session.** Defaults to the last folder used with that agent; a fresh folder is created when none is picked; fixed once the chat starts; sub-agents share it. |
| Browser | **The user's installed Chrome, Chromium, Edge, Brave or Vivaldi**, launched by the app with a separate agent profile over a pipe. Chosen in desktop Settings → Browser. No bundled browser download. |
| Approvals | **The user picks a mode per session.** "Work freely inside the folder" (default) or "Ask every time". |
| Servers | **surogate.ai and enterprise installs.** An agent is added by URL on any domain. |
| Laptop asleep or app closed | **The session waits** inside the tool call, with no time limit, and continues when the app reconnects. The app keeps running after its windows close, with optional start at login. |
| How the worker learns the laptop is back | **It doesn't need to.** The worker's request waits in a Redis queue; the reply is what wakes it. |
| Where tool logic runs | **In the worker's existing Python.** The laptop performs only raw file, process and browser operations. The tools are not rewritten in TypeScript. |
| Files from other devices | **Visible and usable from any device while the laptop is online. Nothing while it is offline**, not even a file list. |
| Packaging | **An install script and a tarball** under the user's home, with in-app updates. |

## Architecture

```
Desktop app (Linux first)                              Surogate server (surogate.ai or enterprise)
┌─────────────────────────────────────┐               ┌───────────────────────────────────────┐
│ Main process                        │  device link  │ api                                   │
│  windows · tray · start at login    │◀─── wss ─────▶│  device socket, presence in Redis     │
│  agent list · surogates:// links    │               │         ▲                             │
│  device link client · updates       │               │         │ Redis relay                 │
├─────────────────────────────────────┤               │         ▼                             │
│ Tool host (utilityProcess)          │               │ worker                                │
│  raw file / process operations      │               │  DesktopSandbox · DeviceWorkspaceIO   │
│  commands via srt                   │               │  DeviceBrowserClient · wait helper    │
│  browser controller ──── pipe ──────┼─▶ Chrome/     │  (tool logic stays in Python)         │
├─────────────────────────────────────┤   Chromium/   │                                       │
│ Agent windows                       │   Edge        │ api                                   │
│  the web client, loaded from the    │◀─── https ───▶│  web client · REST · SSE              │
│  agent's URL, plus a small bridge   │  (agent       │  (same origin, unchanged)             │
└─────────────────────────────────────┘   profile)    └───────────────────────────────────────┘
```

The desktop project lives at `desktop/` in this repo, beside `web/`.

One rule runs through every section: **the laptop is the authority for what may
happen on the laptop.** The server decides how to edit a file; the laptop
decides whether it may. Folder, approval mode, read and write rules, and
approval answers are held and enforced on the laptop and cannot be set by the
server or by the web page.

## 1. The app's processes

**Main process.** Owns windows, the tray icon where one exists, start at login,
`surogates://` links, the list of agents (URL and name), the device link and
update checks. It never executes anything a tool call asks for. It stays thin;
logic lives in small modules with injected dependencies that are tested without
Electron.

**Agent windows.** One window per agent, loading `https://<agent-url>/`. The web
client runs there exactly as in a browser, same origin, no code changes needed
for it to work.
- Each agent gets its own storage partition, `persist:agent-<hash>`. The name is
  a hash only: Electron percent-escapes other characters into the partition's
  folder name, and on Windows such a folder silently loses its cookies.
- A preload script exposes `window.surogateDesktop` only when the page's origin
  is in the user's agent list.
- Closing a window hides it. An open chat keeps streaming and raising
  notifications.
- `contextIsolation: true`, `sandbox: true`, `nodeIntegration: false`.
  `setWindowOpenHandler` denies by default; the allowlist admits the Composio
  OAuth popup the web client opens. `openExternal` admits `http:` and `https:`
  only.

**Device link.** Runs in the main process so it stays connected with every
window closed. Section 2.

**Tool host.** An Electron `utilityProcess`, the only place where commands, file
changes and browser actions happen. The main process forwards it operations
from the device link. If it hangs or crashes, the main process restarts it and
reports in-flight operations as interrupted. `srt`'s `SandboxManager` is
initialised here once; each session's policy is applied per command.

**Screens the desktop draws itself.** Local pages, never loaded from a server:
the Agents window, Settings, approval prompts, quit and add-agent confirmations,
and the operating system's folder dialog.

## 2. The device link

### Registering a device

Once per agent the user adds:

1. The user signs in to the agent inside its desktop window.
2. The web client sees the bridge and calls `POST /v1/devices` with a computer
   name ("Flavius's ThinkPad").
3. The api inserts a row in a new `devices` table: `id, org_id, agent_id,
   user_id, name, token_hash, token_prefix, created_at, last_seen_at,
   revoked_at`. `create_all` creates the table; no migration script.
4. The api returns a device token `surg_dev_<random>` once and stores only its
   SHA-256 hash, the same scheme as `surg_sk_` keys
   (`surogates/tenant/auth/service_account.py:187`).
5. The web client hands the token to the main process through the bridge; the
   main process stores it with `safeStorage`.

Each device row covers one agent, so each agent has its own device ID, queue and
socket. A separate device token lets the user revoke one computer without
signing out elsewhere, and keeps the user's login tokens out of the main
process.

### Connection

The main process opens `wss://<agent-host>/api/v1/devices/connect` with
`Authorization: Bearer surg_dev_…` (Node can send headers; the token never
appears in a URL).
- Heartbeat every 15 s. The api refreshes `surogates:device:{id}:online` (45 s
  TTL, value = its pod ID) and `last_seen_at`.
- Reconnect with backoff on disconnect.
- A 401 means revoked: stop reconnecting and ask the user to sign in again.

### An operation, end to end

The unit crossing the link is an **operation** (Section 3), not a whole tool
call.

```
worker (DeviceWorkspaceIO)         Redis                          api pod holding the socket     desktop app
  check presence once ─────────▶ device:{id}:online
  (if missing: emit device.waiting)
  HSET pending {op_id} ────────▶ device:{id}:pending
  PUBLISH nudge ───────────────▶ device:{id} ────────────────────▶ send every pending op ──────▶ run, record
  BLPOP reply:{op_id} ◀──────── reply:{op_id} ◀─────────────────── push result ◀─────────────── result
  HDEL pending, return result
```

- **The pending hash is the source of truth; the publish only reduces latency**,
  the same rule the channel delivery outbox follows. Whenever a socket
  connects, the api sends everything pending for that device. No "device
  connected" notice is needed.
- **Duplicates are expected and harmless.** An api pod can die mid-forward and a
  worker can restart and queue again. Reads, stats, listings, searches and
  same-bytes writes are safe to repeat. `run` and `start` carry the fixed ID
  `{tool_call_id}:run`, and the laptop's operation record (below) guarantees
  they execute at most once.
- **Pause or stop while waiting.** The worker also watches the session's
  existing channel `surogates:session:{id}`, which carries the pause event. On
  pause it deletes its pending entry and publishes a `cancel` for the operation;
  the tool host kills the running process if there is one.
- **`DesktopSandbox.status()` always returns `RUNNING`.** Otherwise
  `SandboxPool.ensure` would treat an offline laptop as a dead sandbox and try to
  replace it. Waiting happens in the operation, not in the pool.

### The shared wait helper

`ask_user_question` today parks the worker inside the tool call for up to 30
minutes, polling the event log every second and renewing the lease
(`surogates/tools/builtin/ask_user_question.py:52`). It returns the tenant slot
through `released_for_wait` (`surogates/runtime/turn_gate.py:149`) but keeps the
worker's slot in the dispatcher semaphore
(`surogates/orchestrator/dispatcher.py:270`).

One helper replaces that pattern for both waits:
- returns **both** the tenant slot and the worker's dispatcher slot for the
  duration, and re-acquires them afterwards;
- wakes on Redis (a reply key or the session channel) instead of polling the
  database;
- renews the lease.

`ask_user_question` keeps its 30-minute cap and now wakes on the
`ask_user_question.response` nudge that `SessionStore.emit_event` already
publishes. The device wait has no cap. Known ceiling: one parked coroutine and
one lease renewal per waiting session; ending the turn and resuming it later is
the upgrade if thousands of sessions wait at once.

### The laptop is the authority for the folder

When the desktop creates a session it records `root session id → folder,
approval mode` locally (Section 8). The folder path is also sent to the api for
display and for `workspace_path`, but the laptop does not trust it back.
- An operation is accepted only for a root session this app created.
  Operations carry the **root** session ID (`sandbox_session_key`), because
  children created in the cloud by delegation, tasks or schedules are unknown to
  the laptop.
- The operation runs against the folder the app recorded, whatever the request
  says.

### Disconnections

While the app is offline nothing can reach the folder. Chat works from any
device; cloud-only tools keep working; the first local operation waits and the
session shows "Waiting for <computer>". The user can pause or stop the session
from any device, which removes the pending operation. Scheduled runs on a local
session start and wait at their first local operation. There is no fallback to
a cloud sandbox.

| Event | A command running at that moment | Outcome |
|---|---|---|
| Network drops, laptop sleeps | keeps running, or is suspended with the laptop and resumes on wake | the result is stored locally and sent on reconnect |
| User quits | if sessions are working, the app asks first ("2 sessions are working on this computer. Quit anyway? They'll wait until you open Surogate again."); on quit, running commands are stopped and recorded **interrupted** | on reconnect the agent sees "interrupted: the app was closed while this command ran" |
| App crash or power loss | dies with no finished record | on reconnect the laptop finds a *started* marker without a result and reports **interrupted (the app stopped unexpectedly)**; it never re-runs the command |
| Background processes | end with the app | later `poll` / `read_output` return "the process ended when the app quit" |

**Operation record.** The tool host writes, per operation: *received*;
*started* (for `run` and `start`, on disk before the command begins); *result*
(on disk before sending); *acknowledged* (the api put the result into Redis).
After reconnecting, the app re-sends unacknowledged results and the api re-sends
pending operations; each side ignores what it already handled. Records are
deleted 24 hours after acknowledgement.

## 3. The operation interface

`surogates/tools/builtin/file_ops.py` (2,620 lines), `terminal.py` (854),
`surogates/tools/utils/process_registry.py` (1,144) and the research tools stop
calling `os`, `pathlib` and `subprocess` directly and go through a `WorkspaceIO`
passed to each handler:

| Operation | Returns | Notes |
|---|---|---|
| `stat(path)` | type, size, mtime, or not found | |
| `read(path, max_bytes)` | bytes | 50 MB cap for documents; text stays paged |
| `write(path, bytes)` | new stat | atomic: temp file, then rename |
| `delete(path)` | | refuses the folder root |
| `list(path)` / `walk(path, limits)` | entries | depth and count limits |
| `changed_since(time)` | entries | filtered on the laptop; for the end-of-turn summary |
| `search(pattern, path, mode)` | ripgrep results | `rg` runs where the files are |
| `run(command, workdir, timeout, env)` | exit code, output | inside `srt` on the laptop |
| `start` / `poll` / `read_output` / `kill` | process handle, state | `terminal(background=true)` and the `process` tool |

Implementations:
- **`LocalWorkspaceIO`**: today's behaviour, used by the cloud sandbox's
  executor. Cloud behaviour does not change.
- **`StorageWorkspaceIO`**: the S3 workspace via `StorageBackend`, keyed by the
  root session; used by api and harness code for cloud sessions (Section 6).
- **`DeviceWorkspaceIO`**: sends operations to the laptop (Section 2).

**Routing stays as it is.** The six tools remain sandbox-routed. For a desktop
session `SandboxPool` selects the `DesktopSandbox`, whose `execute(name, input)`
runs the existing Python handler **inside the worker** bound to a
`DeviceWorkspaceIO`. Patch matching, read tracking, document parsing (PDF, Word,
Excel, PowerPoint, OpenDocument, RTF), image reads through vision, output limits
and lint all stay in Python. The `process` tool, harness-routed today, uses the
same `DeviceWorkspaceIO` for desktop sessions.

**Lint** after writes goes through `run` and is skipped when the linter is not
installed on the laptop.

**Cost.** `patch` is two round trips (read, write); a document read is one large
read. On a normal connection that adds about 100–200 ms per tool call.

## 4. Rules on the laptop, and approvals

All rules run in the tool host. The worker never sees them and cannot change
them.

**Reads.** `denyRead: ["~"]`, with `allowRead` re-admitting the session folder
and toolchain folders: `~/.nvm`, `~/.pyenv`, `~/.cargo`, `~/.rustup`,
`~/.local/bin`, `~/.local/lib`, `~/go`, `~/.bun`, `~/.deno`, `~/.sdkman`,
`/home/linuxbrew`. System paths stay readable. Documents, `~/.ssh`, `~/.aws`,
keyrings, browser profiles and the app's own data are unreadable without a list
of secrets to maintain. Paths are literal (Linux `srt` does not take Seatbelt
globs).

**Writes.** Only the folder and a per-session temporary folder
`~/.local/share/Surogate/tmp/<session>`. `srt`'s mandatory denies apply
(`.bashrc`, `.gitconfig`, `.git/hooks`, `.git/config`, …), plus the harness's
existing list (`.env*`, `credentials.json`, `secrets.yaml`). Package caches are
redirected into the temporary folder via `XDG_CACHE_HOME`, `npm_config_cache`,
`PIP_CACHE_DIR`, `UV_CACHE_DIR` rather than granted write access to `~/.cache`
or `~/.npm`.

**Network.** Commands reach only allowed domains, starting from the harness's
current list (`surogates/tools/builtin/terminal.py:141`: GitHub, PyPI, npm). Any
other domain triggers `srt`'s ask callback. File operations make no network
calls.

**Environment.** Built explicitly: `HOME`, `LANG`, `TMPDIR`, the cache variables,
and `PATH` read once from the user's login shell so nvm and pyenv shims resolve.
Nothing else from the app's environment passes through.

**Path checks for file operations.** Resolve with `realpath` (symlinks cannot
escape), require the result to be inside the folder or an allowed read path,
then apply the same deny rules as `srt`.

**Approval prompts** appear in a window the desktop draws itself, never in the
web page. When the agent window is hidden a system notification opens it. A
prompt names the agent, the session and exactly what is asked (command, domain
or path). Focus starts on **Deny**.

| Mode | Asks before |
|---|---|
| **Work freely** (default) | a domain not on the list · a path outside the folder · the first browser use in the session |
| **Ask every time** | the above, plus every command, every file write, and every browser action that changes something (navigate, click, type, drag) |

- Buttons: **Allow** and **Deny**; **Allow for this session** for a domain or a
  path; **Stop asking for this session** in "Ask every time", which switches the
  session to "Work freely".
- While a prompt is open the worker waits with the shared helper.
- Deny returns "the user denied this" as the operation result; the model sees it
  as the tool result.

**Only the desktop can make a session less safe.** The page runs server content,
so:
- The page may request "last used", "new" or the folder dialog. It never sends
  a path.
- The page may switch a session to "Ask every time". Switching to "Work freely"
  happens only in the desktop's own prompt or menu.
- Approvals are answered only in the desktop's window. The bridge has no call
  for answering one.

## 5. The local browser

**Raw operations on the laptop; logic in the worker.**

| Operation | Playwright call |
|---|---|
| `navigate(url, wait_until)` | `page.goto` |
| `evaluate(function_body)` | `page.evaluate`, in the page's own context, never in Node |
| `mouse(click / move / down / up / wheel, x, y, button)` | `page.mouse.*` |
| `keyboard(type / press, text or keys)` | `page.keyboard.*` |
| `screenshot(options)` | `page.screenshot`, returning bytes |
| `close()` | closes this session's tab |

A `DeviceBrowserClient` in the worker has the same public methods as
`KernelBrowserClient` (`surogates/browser/client.py`). The `@eN` reference logic
(snapshot script, reference cache, key-name mapping) stays in Python; the
snapshot's page-side JavaScript reaches the laptop through `evaluate`. The
laptop never receives Playwright code: `KernelBrowserClient` today sends
Playwright snippets to kernel-images' `/playwright/execute`, and executing such
snippets on the laptop would let whoever controls the server run programs
there. Browser tools pick `DeviceBrowserClient` for desktop sessions and skip
`BrowserPool`, and with it browser-minute billing.

**Choosing the browser.** Desktop Settings → **Browser**, stored locally, for
all agents. The setting is a program path, so it is never settable from the web
page.
- **Automatic** (default): the first supported browser found.
- Each Chromium-based browser detected, with its version: Chrome, Chromium,
  Edge, Brave, Vivaldi.
- **Custom…**: a program file, accepted only if `--version` identifies it as
  Chromium-based.
- Firefox is not offered (Playwright drives only its own patched Firefox); the
  setting says so.
- Snap and Flatpak builds are not supported: their confinement interferes with
  the profile folder and pipe control.
- A change applies the next time an agent's browser starts.

**No supported browser.** Browser tools return "No supported browser on this
computer. Install Chrome, Chromium, Edge, Brave or Vivaldi (not the Snap or
Flatpak build), or pick one in Settings → Browser." The agent continues with its
other tools. The web client's browser pane shows the same message with a button
to desktop Settings → Browser.

**Profiles.** One browser process per agent with a persistent profile at
`~/.config/Surogate/browser-profiles/<agent-id>/<browser-id>`. Profiles are per
browser because Chrome and Edge cannot safely share one; switching browsers
starts with fresh logins (the setting says so) and switching back restores the
old profile. Parallel sessions of one agent each get a tab. A session's tab
closes on `browser_close`, when the session ends, or when the app quits.
Playwright launches the browser over `--remote-debugging-pipe`, so no debugging
port is opened.

**Downloads and uploads.** Downloads land in the session folder. File inputs
accept only paths inside the folder.

**Navigation policy.** Chrome is not wrapped in `srt`. Top-level navigation is
checked on the laptop: `file:`, `chrome:`, `devtools:` and similar schemes are
blocked; loopback and private-network addresses prompt, with "Allow for this
session".

**Watching and taking over.** The window is on the user's screen; the cloud live
view is not used. The web client's browser pane shows "The browser is open on
this computer" with **Show browser** and **Take over** / **Hand back**. The
buttons appear only in the desktop app on the computer the session is bound to;
elsewhere the pane shows only the message. Taking over makes browser operations
return the existing `paused_by_user` result until handed back.

## 6. Harness and api changes

**Marking a desktop session.** The session-creation request carries
`execution: {kind: "device", device_id}` and the folder path. The api accepts it
only when the device belongs to this user and agent and is not revoked;
otherwise it strips it, as it strips other client-supplied keys today
(`surogates/api/routes/sessions.py:578`). The server advertises
`desktop_sessions: true` in `/api/v1/auth/config`.
- `workspace_path` is the **real folder path**. `srt` has no portable way to
  present a folder under another name, and commands the model writes use
  whatever path it sees. Code that hard-codes `/workspace` reads
  `workspace_path` instead (skill staging's `staged_at`, missions' `repo=`).
- The S3 storage fields are still stamped and left unused, so
  `create_child_session` (which raises when they are missing,
  `surogates/session/provisioning.py:162`) and the cleanup jobs need no special
  case.
- Children created by delegation, tasks or schedules copy `execution` and
  `workspace_path` from the parent.

**One guard at the sandbox pool.** Any sandbox request for a desktop session
goes to the `DesktopSandbox`, whoever makes it. The expert tool loop
(`surogates/tools/router.py:260`) and Arbor (`surogates/tools/builtin/arbor.py`)
call `pool.execute` directly with a default spec and would otherwise start an
empty cloud sandbox.

**One resolver for everything else.** `workspace_io(session)` returns
`StorageWorkspaceIO` for cloud sessions and `DeviceWorkspaceIO` for desktop
sessions. The touchpoints below call it instead of `StorageBackend` directly.
Because the resolver keys cloud sessions by the root, this also fixes two
existing bugs: skill staging (`surogates/api/routes/skills.py:614,634`) and
`media_gen` (`surogates/tools/builtin/media_gen.py:720`) key by the calling
session, so child sessions write to the wrong prefix.

| Goes through the laptop | Switched off for desktop sessions (first release) | Not affected |
|---|---|---|
| file panel api routes: tree, file, upload, download, delete (`surogates/api/routes/workspace.py`) | checkpoints and rollback (already off by default; no routes exist) | memory, knowledge bases, document cache |
| message attachments: `uploads/…` and inlined text (`sessions.py:1096-1199`) | saga compensation via `_checkpoint` | Slack/Telegram attachment ingest |
| artifacts `_artifacts/…` (`surogates/artifacts/store.py`), including board tools | `/code` and `run_coding_agent` | cloud browser profiles |
| whiteboard canvas `_whiteboard/canvas.json` | Arbor | |
| research notes `.research/…` (tools move onto `WorkspaceIO`) | | |
| spilled tool output `.surogates-results/…` (already via the pool's `write_file`) | | |
| skill staging `.skills/…` | | |
| image reads for vision, `media_gen` output, browser screenshots | | |
| AGENTS.md / CLAUDE.md context and subdirectory hints (still injection-scanned) | | |
| end-of-turn changed-files list, via `changed_since` | | |

A switched-off feature returns "not available for sessions on a local folder" as
its tool error.

**Deleting a session never deletes the folder.** `delete_session` and the
cleanup job call `delete_prefix`; for a device workspace it does nothing, and
the laptop refuses to delete the folder root.

**Offline.** File routes return "The files are on <computer>, which is offline"
and the web client shows it. No file list is cached.

**New events.** `device.waiting {device_name}` and `device.resumed`: added to
`EventType` (`surogates/session/events.py`), the SDK's `AgentChatEventType`,
`AGENT_CHAT_LISTENED_EVENTS` and `applyAgentChatEvent`.

## 7. The desktop shell and sign-in

**Agents window.** Lists added agents with Open, Remove, **Add agent**. Opens at
start, and again when the app is launched while already running (single-instance
lock).

**Adding an agent.** The user pastes a URL; the app calls
`GET /api/v1/auth/config` to confirm it is a surogates agent and saves its name
and icon. A `surogates://open?url=…` link (the web client's **Open in desktop
app** button) opens an existing agent, or asks "Add the agent at <host>?" first
for a new one.

**Settings.** Browser (Section 5); **Start at login**, written as
`~/.config/autostart/surogate.desktop` (Electron's login-item API does not
support Linux); **Keep running when all windows are closed** (on by default).

**Staying alive and quitting.** Closing windows hides them; the device link
stays connected. **Quit Surogate** is in the tray menu where a tray exists, the
Agents window menu, and Ctrl+Q; it confirms when sessions are working. On GNOME
without a tray, launching the app again shows the Agents window.

**Sign-in.** Username/password and Firebase email/password work in the window
unchanged. Google and GitHub (Firebase popups; Google refuses embedded
browsers) use a system-browser handoff with a loopback redirect and PKCE
(RFC 8252):

1. The web client calls `desktop.signInExternally("google")` instead of
   `signInWithPopup`.
2. The main process listens once on `127.0.0.1:<random port>` and opens the
   system browser at `https://<agent>/login?desktop_state=<state>&code_challenge=<challenge>&port=<port>`.
3. The user signs in normally. The web client, seeing `desktop_state`, calls
   `POST /api/v1/auth/desktop/code` as the signed-in user; the api stores a
   one-time code bound to the challenge in Redis for 60 s.
4. The browser redirects to `http://127.0.0.1:<port>/callback?code=…&state=…`.
   The main process checks `state` and exchanges the code with its verifier at
   `POST /api/v1/auth/desktop/token` for access and refresh tokens.
5. The main process writes the tokens into that window's storage under the keys
   the web client already reads (`surogates_auth_token`,
   `surogates_auth_refresh_token`) and reloads it.
6. The browser tab shows "Signed in, you can return to Surogate".

**Signing out** clears the window's storage and, through the bridge, deletes the
device token and calls `DELETE /v1/devices/{id}`.

## 8. The web client's desktop mode

**Contract.** `web/src/lib/desktop-bridge.ts` holds the bridge types and
`getDesktop()`, which returns `undefined` in a browser. `desktop/` imports the
same file for its preload and main-process handlers.

**Versions.** The bridge reports a `version`; the web client uses only calls
that version has. On a server without `desktop_sessions`, the app shows the web
client with ordinary cloud sessions and the Agents window notes "This server
doesn't support local folders yet".

| Call | Does | Safety rule |
|---|---|---|
| `registerDevice(token)` | hands the device token to the main process | |
| `prepareFolder("last" \| "new" \| "pick")` | the main process picks the folder (OS dialog for `pick`), returns the display path and a one-time token | the page never sends a path |
| `bindSession(sessionId, token)` | records `session → folder, mode` on the laptop | |
| `setMode(sessionId, "ask")` | switches to "Ask every time" | safer direction only |
| `requestFreeMode(sessionId)` | opens the desktop's confirmation for "Work freely" | the desktop decides |
| `revealFolder(sessionId)` | opens the folder in the file manager | |
| `browser.show(sessionId)`, `takeOver`, `handBack` | Section 5 | pausing only makes a session safer |
| `signInExternally(provider)` | Section 7 | |
| `openSettings(section)`, `showWindow()` | opens desktop Settings; raises the window | |

**New chat.** Under the composer: **Folder: ~/Surogate/<agent>/<new chat>** with
**Change…**, and **Mode: Work freely ▾**. On the first message the web client
calls `prepareFolder`, creates the session with `execution` and the returned
path, then calls `bindSession`. Every new chat in the desktop app is local;
sessions created earlier in a browser keep their cloud sandbox.

**Inside a local session.** Header: folder name with **Open folder**, the mode,
"on <computer>". A "Waiting for <computer>" line driven by `device.waiting` /
`device.resumed`. File panel, artifacts, whiteboard and uploads unchanged
through the api. Browser pane per Section 5. Sessions list: a laptop icon and
the computer's name, also in the browser.

**Notifications (desktop only).** The standard `Notification` API, shown through
the Linux notification service, when the window is hidden or unfocused: turn
finished (`session.complete`), the agent asked a question (`ask_user_question`
tool call), a new inbox item. A click calls `showWindow()` and opens the
session. Laptop approval prompts are raised by the desktop itself.

**Settings → Devices** (browser and desktop): name, created, last seen,
**Revoke**. Revoking closes the socket; that device's sessions show "Waiting for
<computer>" until the user signs in there again.

## 9. Install and updates

**Artifact.** `surogate-desktop-<version>-linux-x64.tar.gz` (electron-builder
`dir` target, tarred) and `latest.json` with `{version, url, sha256}` and an
Ed25519 signature; the public key is built into the app. Built by a new job in
the release workflow on GitHub-hosted runners. arm64 later.

**Hosting.** surogate.ai serves `https://surogate.ai/desktop/install.sh`; the
tarballs and `latest.json` live on Cloudflare R2. Enterprise installs host the
same signed files on their server or an internal URL (placed by the enterprise
install kit). The app updates from the base URL it was installed from, recorded
by the script, so an air-gapped install never contacts us.

**`curl -fsSL <base>/desktop/install.sh | bash`**
1. Detect architecture and package manager (apt, dnf, pacman, zypper).
2. Explain what needs `sudo`, ask once, then install `bubblewrap`, `socat`,
   `ripgrep`, and on Ubuntu 24.04+ with
   `kernel.apparmor_restrict_unprivileged_userns=1` an AppArmor profile allowing
   user namespaces for the app and `bwrap`. The app never runs with
   `--no-sandbox`.
3. Download the tarball, verify hash and signature, unpack into
   `~/.local/share/surogate/versions/<version>/`, point `current` at it.
4. Add `~/.local/bin/surogate`, a `.desktop` entry with an icon, and the
   `surogates://` handler via `xdg-mime`.
5. Check for a supported browser; print a note if none (no failure).
6. Optional `--ca-cert <file>`: import a company CA into `~/.pki/nssdb` with
   `certutil`, the store Chromium and Electron read on Linux.

Safe to re-run; re-running repairs or updates. `--uninstall` removes the app,
launcher entry, link handler and autostart file, asks before deleting app data
(device tokens, browser profiles), and never touches session folders.

**In-app updates.** Check `<base>/desktop/latest.json` at start and every 6
hours. Download, verify, unpack into `versions/<new>/`, show **Restart to
update**. Switch at restart or the next quit, never while a command runs. One
previous version is kept; `install.sh --version <x>` rolls back.

**Missing dependencies.** Checked at start. Local sessions fail with
"Surogate's sandbox tools are missing; run the install script again" and the
Agents window says the same. No unsandboxed fallback.

**Windows and macOS later.** Windows: the same layout under `%LOCALAPPDATA%`,
installed by a PowerShell script; the command runner sits behind one module so
Windows can use `srt`'s Windows mode (alpha today: commands run as a separate
`srt-sandbox` user, per-user tools are invisible, certificate revocation checks
are blocked) or a WSL2 runner. macOS: code signing and notarization, its own
release job.

## 10. Testing and the first spike

**First spike** (throwaway, on clean Ubuntu 24.04 and Fedora VMs; output is a
written yes/no per question):

| # | Question | If no |
|---|---|---|
| 1 | Does Electron start with its sandbox under Ubuntu's user-namespace restriction with our AppArmor profile, and does `srt`'s `bwrap` work under it? | adjust the profile; never `--no-sandbox` |
| 2 | Can a `utilityProcess` use `srt` as a library; does `denyRead ["~"]` + `allowRead` behave with literal paths; do `npm install` / `pip install` work with redirected caches; does the network ask callback fire? | adjust the policy builder |
| 3 | Is a background server started by one `srt` command reachable from the next (each Linux command gets its own network namespace)? | one long-lived sandboxed shell per session for all its commands |
| 4 | Can `playwright-core` in the `utilityProcess` launch system Chrome, Chromium and Edge with a persistent profile over a pipe; do navigation checks and downloads into the folder work? | narrow the supported browsers |
| 5 | After a worker restart, does the harness replay re-issue a tool call that never got a result? | add a replay step for unfinished calls |
| 6 | Does loopback + PKCE sign-in work with Firebase Google on a real agent? | adjust the login-page flow |

**Harness and api (pytest, testcontainers Postgres and Redis).**
- `file_ops`, `terminal`, `research`, `process` tests parametrized over
  `LocalWorkspaceIO` and `DeviceWorkspaceIO` with an in-process fake laptop.
- Relay: pending hash and resend after reconnect; duplicate operations ignored,
  `{tool_call_id}:run` never twice; cancel on pause; the wait helper returns both
  slots (asserted on the dispatcher semaphore) and wakes on the reply;
  `ask_user_question` wakes over Redis.
- Pool guard: `pool.execute` for a desktop session reaches `DesktopSandbox`,
  including from the expert loop and Arbor.
- Resolver: every "goes through the laptop" touchpoint reads and writes through
  the fake laptop; switched-off features return their error; deleting a session
  never deletes the folder.
- Creation and devices: `execution` stripped unless the device is valid;
  children inherit; tokens stored hashed; a revoked device gets 401 and its
  socket closes.

**Desktop (vitest, Node project).** Pure policy modules: path authorization
(`realpath`, symlink escape, root vs child), `srt` config builder, environment
builder, navigation policy, approval-mode rules, the operation record
(received → started → result → acknowledged; interrupted on quit or crash),
browser detection and `--version` check, update signature verification.
Device-link protocol against a fake WebSocket server: reconnect, resend,
duplicates, cancel.

**Web client and SDK (vitest).** Reducer handles `device.waiting` /
`device.resumed`; without a bridge the web client behaves exactly as today; the
desktop new-chat flow against a fake bridge.

**End to end (CI, `ubuntu-latest`, xvfb).** Playwright `_electron` drives the
real app against a local surogates stack and a scripted fake OpenAI-compatible
LLM returning fixed tool calls: write `hello.txt`; run a command; hit a
non-allowed domain and approve it; open a page in the local browser; quit
mid-command and restart (expect "interrupted"); block and restore the network
(expect resume). The install script runs on the same runner, a real Ubuntu
24.04 VM with AppArmor.

**Manual release check** on a clean VM: install, Google sign-in, a local session
using files, a command and the browser, quit mid-command, sleep and wake,
update.

## Build order

The work splits into sub-projects that can each be planned separately. A
suggested order, each ending in something demonstrable:

1. **First spike** (Section 10). Answers the six questions before anything is
   built on them.
2. **`WorkspaceIO` refactor** of `file_ops`, `terminal`, `process_registry` and
   the research tools, with `LocalWorkspaceIO` only. Cloud behaviour unchanged,
   proven by the existing tests.
3. **Device link and `DesktopSandbox`**: devices table and endpoints, relay,
   wait helper (including `ask_user_question`), pool guard, a minimal desktop
   tool host running file operations and `srt` commands. Demo: an agent edits a
   file in a local folder.
4. **Desktop shell and web-client desktop mode**: Agents window, sign-in
   handoff, bridge, new-chat folder and mode controls, approvals, disconnection
   handling, notifications.
5. **Resolver and touchpoints** (Section 6).
6. **Local browser.**
7. **Install script, release job, in-app updates.**
