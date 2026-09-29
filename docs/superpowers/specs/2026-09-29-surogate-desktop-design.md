# Surogate Desktop Design

Status: design approved. The first spike gates implementation planning
Date: 2026-09-29

Review scope: checked against the current repository and upstream runtime
documentation. The first spike must validate the remaining platform assumptions
before implementation planning.

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

- Windows and macOS. Ubuntu first, Windows second, macOS last. Nothing in the
  first release may depend on a macOS-only or Windows-only mechanism.
- Linux distributions other than Ubuntu, and Ubuntu interim releases (such as
  24.10 or 25.04). The supported releases are Ubuntu 24.04 LTS and every later
  LTS release (26.04 LTS today), on x64.
- Agent builders. Studio stays a web app. The desktop app is for people using
  agents.
- Running the harness, the LLM loop or a local model on the laptop.
- New access to a local session's files while the computer is offline.
- `.deb`, `.rpm` or AppImage packages. The app installs with a script.

## Decisions

Product choices from brainstorming, with implementation details corrected by
this review. The spike gates the technical choices that still need proof.

| Question | Decision |
|---|---|
| Who is it for? | Agent users only. |
| What runs where? | Reasoning in the cloud, hands on the laptop. File, terminal and browser tools act on the user's computer. Every other tool stays in the cloud. |
| New UI or the existing web client? | The existing web client, loaded from the agent's URL in an Electron window, plus a small bridge. No second chat UI. |
| First platforms | Ubuntu LTS from 24.04 on (24.04, 26.04 and later LTS releases), then Windows, then macOS. No interim Ubuntu releases and no other Linux distributions. |
| Local isolation | Anthropic Sandbox Runtime (`srt`), used as a Node library (`@anthropic-ai/sandbox-runtime`). |
| Workspace | The local folder is the session's only workspace. Uploads, artifacts, the whiteboard and research notes live there. |
| Folder binding | One folder per session. Defaults to the last folder used with that agent. A fresh folder is created when none is picked. Fixed once the chat starts. Sub-agents share it. |
| Browser | The user's installed Chrome, Chromium, Edge, Brave or Vivaldi, launched by the app with a separate agent profile over a pipe. Chosen in desktop Settings → Browser. No bundled browser download. |
| Approvals | The user picks a mode per session. "Work freely inside the folder" (default) or "Ask every time". |
| Servers | surogate.ai and enterprise installs. An agent is added by URL on any domain. |
| Laptop asleep or app closed | The session waits inside the tool call, with no time limit, and continues when the app reconnects. The app keeps running after its windows close, with optional start at login. |
| How the worker resumes | A durable operation journal tracks unfinished work. Redis wakes the worker when the operation changes. Reconnect reconciles the journal with the laptop. |
| Where tool logic runs | In the worker's existing Python. The laptop performs only raw file, process and browser operations. The tools are not rewritten in TypeScript. |
| Files from other devices | Visible and usable from any device while the laptop is online. Offline file access is unavailable. Existing chat history remains visible. |
| Packaging | An install script and a tarball under the user's home, with in-app updates. |

## Architecture

```
Desktop app (Ubuntu first)                             Surogate server (surogate.ai or enterprise)
┌─────────────────────────────────────┐               ┌───────────────────────────────────────┐
│ Main process                        │  device link  │ api                                   │
│  windows · tray · start at login    │◀─── wss ─────▶│  device socket, presence in Redis     │
│  agent list · surogates:// links    │               │         ▲                             │
│  device link client · updates       │               │         │ Redis relay                 │
├─────────────────────────────────────┤               │         ▼                             │
│ Tool hosts (one per root session)   │               │ worker                                │
│  raw file / process operations      │               │  DesktopSandbox · DeviceWorkspaceIO   │
│  commands via srt                   │               │  DeviceBrowserClient · wait helper    │
│  browser host ────────── pipe ──────┼─▶ Chrome/     │  (tool logic stays in Python)         │
├─────────────────────────────────────┤   Chromium/   │                                       │
│ Agent windows                       │   Edge        │ api                                   │
│  the web client, loaded from the    │◀─── https ───▶│  web client · REST · SSE              │
│  agent's URL, plus a small bridge   │  (agent       │  (same origin, unchanged)             │
└─────────────────────────────────────┘   profile)    └───────────────────────────────────────┘
```

The desktop project lives at `desktop/` in this repo, beside `web/`.

One rule runs through every section: the laptop is the authority for what may
happen on the laptop. The server decides how to edit a file. The laptop
decides whether it may. The laptop owns folder bindings and enforces read and
write rules. Only native approval can expand access. The web page can request
stricter restrictions, including Ask every time mode.

The local folder remains the workspace, but processing uses cloud services.
File contents read by tools, screenshots and command output cross the device
link. The harness can retain derived content in chat history and document
caches under its existing retention policy. There is no S3 mirror of the
folder. The folder picker explains this before you enable local access.

## 1. The app's processes

Main process. Owns windows, the tray icon where one exists, start at login,
`surogates://` links, the list of agents (URL and name), the device link and
update checks. It never executes anything a tool call asks for. It stays thin.
Logic lives in small modules with injected dependencies that are tested without
Electron.

Agent windows. One window per added agent, loading its HTTPS origin. It uses
the shared web client with the desktop-aware flows in Sections 7 and 8.

- Each registered agent gets its own storage partition, `persist:agent-<hash>`.
  Hash the canonical server origin and agent identity into a filesystem-safe
  name. Signing out clears that partition's login state before another user
  signs in.
- A preload script exposes `window.surogateDesktop` only when the page's origin
  is in the user's agent list.
- Every main-process IPC handler also validates the sending window, its
  top-level frame and its exact registered origin. It checks argument schemas
  and session ownership. Subframes and OAuth popups have no bridge. Agent
  windows stay on the registered origin. Approved external links open in the
  system browser. See [Electron's IPC guidance](https://www.electronjs.org/docs/latest/tutorial/security#17-validate-the-sender-of-all-ipc-messages).
- Closing a window hides it. An open chat keeps streaming and raising
  notifications.
- `contextIsolation: true`, `sandbox: true`, `nodeIntegration: false`.
  `setWindowOpenHandler` denies by default. The allowlist admits the Composio
  OAuth popup the web client opens. `openExternal` admits `http:` and `https:`
  only.

Device link. Runs in the main process so it stays connected with every
window closed. Section 2.

Tool hosts. The main process starts one Electron `utilityProcess` per active
root session for file operations and commands. Children share their root's
host. Each host initialises its own `SandboxManager` and network proxies.
The runtime holds configuration and proxy state in module globals, so changing
one shared manager per command would mix concurrent sessions' permissions.
See the [SandboxManager implementation](https://github.com/anthropics/sandbox-runtime/blob/main/src/sandbox/sandbox-manager.ts).

A separate browser host owns each agent identity's browser process (Section 5).
The main process routes operations and native approval decisions to these hosts.
Each host enforces the relevant local policy. A host crash interrupts its
unfinished operations. Supervision must terminate its complete command process
trees before a replacement host accepts work, including after an app crash.
The spike must verify this lifecycle. Idle hosts may exit when they have no
running processes.

Screens the desktop draws itself. Local pages, never loaded from a server:
the Agents window, Settings, approval prompts, quit and add-agent confirmations,
and the operating system's folder dialog.

## 2. The device link

### Registering a device

Once per agent the user adds:

1. The user signs in to the agent inside its desktop window.
2. The web client sees the bridge and calls `POST /api/v1/devices` with a computer
   name ("Flavius's ThinkPad").
3. The api inserts a row in a new `devices` table: `id, org_id, agent_id,
   user_id, name, token_hash, token_prefix, created_at, last_seen_at,
   revoked_at, credential_generation`. `create_all` creates the new table.
4. The api returns a device token `surg_dev_<random>` once and stores only its
   SHA-256 hash, the same scheme as `surg_sk_` keys
   (`surogates/tenant/auth/service_account.py:187`).
5. The web client hands the token to the main process through the bridge. The
   main process verifies its device identity against the registered origin and
   stores it with `safeStorage` when a real OS secret store is available.
   Linux's `basic_text` backend provides no useful protection. In that case the
   app stores the token in a file readable only by the user (mode `0600`) in its
   private config directory and shows a notice that credentials on this
   computer are not encrypted. This matches the protection the web client's
   refresh token already has in the partition's `localStorage`, and keeps start
   at login working without a new sign-in after each reboot.
   See [Electron safeStorage](https://www.electronjs.org/docs/latest/api/safe-storage).

Each device row covers one agent, so each agent has its own device ID, queue and
socket. A separate device token lets the user revoke one computer without
signing out elsewhere. Persisted credentials and local bindings are keyed by
the canonical server origin plus org, agent and user IDs. The main process uses
the device token for the link. Login tokens pass through it only transiently
during the external sign-in handoff.

Device tokens authenticate only the device link and that device's lifecycle
endpoints. They cannot create chats or call arbitrary workspace routes. The
server checks the owning principal and root session on every queued operation
and accepts replies only for operations assigned to the authenticated device.

### Connection

The main process opens `wss://<agent-host>/api/v1/devices/connect` with
`Authorization: Bearer surg_dev_…` (Node can send headers. The token never
appears in a URL).

- Negotiate a protocol version and supported operations before forwarding work.
  Unsupported operations return a capability error without starting work.
- Heartbeat every 15 s. The api refreshes `surogates:device:{id}:online` (45 s
  TTL, value = pod ID plus connection generation). Only that generation can
  refresh or clear its presence. A new socket supersedes the old one.
  Coalesce database `last_seen_at` writes.
- Reconnect with backoff on disconnect.
- A rejected token or revocation stops reconnecting and suspends local work.
  Token rotation closes older sockets. Restoring a revoked device also needs
  native confirmation (Section 8).

### An operation, end to end

Tool handlers send individual operations across the link (Section 3).

```
worker                       Postgres / Redis             api                 desktop
  persist operation ───────▶ operation journal
  publish wakeup ──────────▶ Redis ──────────────────────▶ reconcile ────────▶ record, run
  await state change ◀────── Redis ◀────────────────────── commit result ◀─── local result
  read committed result ◀─── operation journal
  commit tool result ──────▶ mark consumed ──────────────▶ acknowledge ─────▶ reclaim payload
```

- A new `device_operations` journal in Postgres is authoritative for requests
  and terminal outcomes. Redis pending entries and notifications are delivery
  aids that can be rebuilt. The api reconciles on connect and at each heartbeat,
  so a missed publish cannot strand an operation indefinitely.
- An envelope includes protocol version, device ID, root and calling session
  IDs, invocation ID, operation ordinal, request digest and execution generation.
  Hash the immutable request, excluding delivery generation and retry metadata.
  Tools use their persisted tool-call ID as the invocation ID. API file
  operations use a persisted request ID. Journal each operation before dispatch
  and resume the recorded sequence on worker recovery. One tool can run several
  commands, including lint. `{tool_call_id}:run` alone is insufficient.
- Every operation has a stable ID. A duplicate with the same digest returns
  the recorded result or joins the running operation. A changed digest is a
  protocol error. File mutations and browser actions use the same rule as
  commands. Repeating an old write could overwrite a later user edit.
- Keep results until the harness commits its tool result or the API request
  reaches a terminal state. A destructive `BLPOP` reply cannot be the only copy.
  Replay resumes the original invocation before asking the model for more work.
  A worker that loses its session lease cannot dispatch more operations or
  commit results under the old generation. The existing `lease_token` is the
  generation: `try_acquire_lease` issues a fresh one on every acquisition,
  including when it steals an expired lease (`surogates/session/store.py:2039`).
  Each journal row records the token that dispatched it, and dispatch and
  commit require it to match the current lease. No change to `session_leases`.
- Pause or stop persists cancellation for the affected calling session and its
  children, then publishes a wakeup. Reconnect applies cancellations before
  starting pending work. The laptop records cancellation and rejects delayed
  deliveries for it. It terminates running process trees and dismisses prompts.
  Completed effects remain. While offline, an already-running command can
  continue until the laptop receives the stop.

- `DesktopSandbox.status()` always returns `RUNNING`. Otherwise
  `SandboxPool.ensure` would treat an offline laptop as a dead sandbox and try to
  replace it. Waiting happens in the operation, not in the pool.

### The shared wait helper

`ask_user_question` today parks the worker inside the tool call for up to 30
minutes, polling the event log every second and renewing the lease
(`surogates/tools/builtin/ask_user_question.py:52`). It returns the tenant slot
through `released_for_wait` (`surogates/runtime/turn_gate.py:149`) but keeps the
worker's slot in the dispatcher semaphore
(`surogates/orchestrator/dispatcher.py:270`).

One helper replaces that pattern for both waits. It tracks slot ownership per
turn and releases the tenant slot and dispatcher slot once the whole turn is
waiting. A waiting operation cannot release slots while parallel sibling tools
still run. Nested waits share this ownership record. Resume reacquires both
slots in the dispatcher's acquisition order before active work continues.
Cancellation and dispatcher cleanup release only slots still owned.

The helper subscribes before checking durable state, then uses Redis as a wakeup.
It rechecks on subscription recovery and at a bounded interval to survive a lost
notification. It renews the lease while waiting and while reacquiring slots.
Lease loss stops that worker's turn. Worker shutdown detaches the wait without
cancelling the durable device operation, so another worker can recover it.

`ask_user_question` keeps its 30-minute cap and now wakes on the
`ask_user_question.response` nudge that `SessionStore.emit_event` already
publishes. The device wait has no cap. Known ceiling: one parked coroutine and
one lease renewal per waiting session. Ending the turn and resuming it later is
the upgrade if thousands of sessions wait at once. Waiting releases execution
capacity, but still counts toward a configured per-device limit on parked
sessions. At the limit, a new local operation fails at once with "Too many
sessions are waiting for <computer>" as its tool result, so the agent tells the
user. There is no second kind of wait.
Execution timeouts start when the local operation starts and exclude time
awaiting native approval. A disconnect does not reset a running command's
remaining execution budget.

### The laptop is the authority for the folder

When the desktop binds a session it records its full agent identity and
`root session id → folder, approval mode` locally (Section 8). The folder path
is also sent to the api for display and for `workspace_path`, but the laptop
does not trust it back.

- An operation is accepted only for a root session this app created.
  Operations carry the root session ID (`sandbox_session_key`), because
  children created in the cloud by delegation, tasks or schedules are unknown to
  the laptop.
- The operation runs against the folder the app recorded, whatever the request
  says.
- Bindings are immutable. Session creation must finish the local binding
  handshake before accepting messages or uploads. A missing, moved or replaced
  root returns `folder_unavailable`. Restore the original folder or select a
  folder for a new session. The server cannot substitute one.

### Disconnections

While the app is offline nothing can reach the folder. Chat works from any
device. Cloud-only tools keep working. The first local operation waits and the
session shows "Waiting for <computer>". The user can pause or stop the session
from any device, which durably cancels the pending operation. Scheduled runs on a local
session start and wait at their first local operation. There is no fallback to
a cloud sandbox.

| Event | A command running at that moment | Outcome |
|---|---|---|
| Network drops, laptop sleeps | keeps running, or is suspended with the laptop and resumes on wake | the result is stored locally and sent on reconnect |
| User quits | if sessions are working, the app asks first ("2 sessions are working on this computer. Quit anyway? They'll wait until you open Surogate again."). On quit, running commands are stopped and recorded interrupted | on reconnect the agent sees "interrupted: the app was closed while this command ran" |
| App crash or power loss | dies with no finished record | on reconnect the laptop finds a started marker without a result and reports interrupted (the app stopped unexpectedly). It never re-runs the command |
| Background processes | end with the app | later `poll` / `read_output` return "the process ended when the app quit" |

Operation record. A local transactional journal stores each operation ID
and request digest, followed by received, started, result and consumed states.
Flush started durably before any effectful operation and flush its result before
sending it. A crash between those writes yields interrupted with an unknown
outcome. It never triggers an automatic rerun. This covers file changes and
browser actions as well as `run` and `start`.

The api acknowledges receipt only after durable commit. A separate consumed
acknowledgement follows the committed tool result or completed API request.
Large payloads may be reclaimed 24 hours after consumption. Compact completion
and cancellation records remain for the lifetime of the root binding, so a
delayed retry after 24 hours cannot execute again. Removing a binding leaves it
revoked locally and rejects further operations for that root. A deliberate
retry after an interrupted operation gets a new invocation ID.

## 3. The operation interface

Workspace access in `surogates/tools/builtin/file_ops.py`, `terminal.py`,
`surogates/tools/utils/process_registry.py` and the research tools goes through
an asynchronous `WorkspaceIO` passed to each handler. Request-scoped adapters
replace host filesystem calls. They must not change the worker's global working
directory or environment to impersonate a laptop session.

| Operation | Returns | Notes |
|---|---|---|
| `stat(path)` | type, size, mtime, revision, or not found | revision supports conflict checks |
| `read(path, offset, max_bytes, revision?)` | bytes, revision, EOF | 50 MiB document cap. Bounded text pages. Reject a changed revision |
| `write(path, bytes, expected_revision?)` | new stat | temp file and atomic rename. Reject detected revision conflicts |
| `delete(path)` | | refuses the folder root |
| `list(path)` / `walk(path, limits)` | entries | depth and count limits |
| `changes(cursor)` | changed or deleted entries, next cursor | laptop-owned cursor avoids worker/laptop clock skew |
| `search(pattern, path, mode)` | ripgrep results | `rg` runs where the files are |
| `run(command, workdir, timeout, env)` | exit code, output | inside `srt` on the laptop |
| `start` / `poll` / `wait` / `read_output` / `kill` | process handle, state | background commands and the `process` tool, including PTY mode |
| `write_stdin(handle, bytes)` / `list_processes()` | input acknowledgement or process summaries | preserve the process tool's write, submit and list actions |

Implementations:

- `LocalWorkspaceIO`: today's behaviour, used by the cloud sandbox's
  executor. Cloud behaviour does not change.
- `StorageWorkspaceIO`: the S3 workspace via `StorageBackend`, keyed by the
  existing workspace identity. Used by api and harness code for cloud sessions
  (Section 6). This is a filesystem adapter. S3 cannot implement process methods.
- `DeviceWorkspaceIO`: sends operations to the laptop (Section 2).

Separate filesystem and process capabilities in the adapter contract. A storage
adapter must report unsupported process operations, with no worker-local shell
fallback.

Routing stays as it is. The six tools remain sandbox-routed. For a desktop
session `SandboxPool` selects the `DesktopSandbox`, whose `execute(name, input)`
runs the existing Python handler inside the worker bound to a
`DeviceWorkspaceIO`. Patch matching, read tracking, document parsing (PDF, Word,
Excel, PowerPoint, OpenDocument, RTF), image reads through vision, output limits
and lint all stay in Python. The `process` tool, harness-routed today, uses the
same `DeviceWorkspaceIO` for desktop sessions.

Parsing and paths. Laptop paths are opaque to the worker. Only the laptop
resolves paths and validates containment. Document parsers that require a local
filename receive downloaded bytes in a private temporary file in an isolated
cloud parser process. Audit parser dependencies in the worker image and bound
CPU, memory and runtime. Remove temporary files after parsing. Document caches
include the workspace identity and content revision in their keys.

Writes and lint. Patch reads a revision and passes it to write. A detected
conflict returns an error for the model to re-read. It must not overwrite the
newer content. Serialize app-issued changes to the same path. Lint goes through
`run`, with its own operation ordinal, and is skipped when the linter is absent.
In Ask every time mode, a lint command also needs approval.

Transport bounds. Control frames are versioned JSON. Binary data uses
numbered chunks of at most 1 MiB, with a transfer ID and content digest. Enforce
per-transfer and aggregate in-flight byte limits with backpressure. Persist
large journal payloads in bounded transfer records outside Redis, retaining
unfinished transfers for replay and deleting consumed payloads on the retention
schedule. These records are temporary protocol data, never a workspace mirror.
Process output is capped and paged by offset. Process handles are opaque and
scoped to the root binding. A host restart invalidates its live process handles.

Cost. Patch requires at least a read and a write round trip, plus any stat
or lint work. Measure latency and large-file transfer behavior in the spike.
Network delay and document size determine the cost.

## 4. Rules on the laptop, and approvals

The tool and browser hosts enforce local rules. The worker receives operation
outcomes and cannot change those rules.

Reads. Build an explicit allowlist for the chosen folder and session temp
directory. Re-admit the system runtime paths needed by installed tools, plus
specific locally discovered toolchain directories. Start with a broad read deny
and verify the generated mounts in the spike. Denying only `~` would leave user
data on other mounts readable. Avoid granting whole trees such as `~/.cargo`
that can also hold credentials. The app's data and browser profiles stay denied,
including when XDG paths place them outside the home directory.

Writes. Allow the folder and a per-session temporary directory under
`~/.local/share/surogate/tmp/<session>`, with explicit protected paths. Preserve
`srt`'s mandatory write denies and the protections in `file_ops.py` and
`terminal.py`. Redirect package caches with `XDG_CACHE_HOME`, `npm_config_cache`,
`PIP_CACHE_DIR` and `UV_CACHE_DIR`. Audit implicit writable paths supplied by the
runtime and isolate temporary data between roots. Reject `/`, the home directory,
and any root that would grant access to the app's own state or credentials.

Pin the `srt` version and test its effective mounts. Linux write rules accept
literal paths. A pattern such as `.env*` is not a supported blanket write deny.
Read globs expand against files present at command launch. Build explicit paths
for the command policy and test protected files created or renamed during a
command. Keep any stronger filename filtering in raw file APIs explicit.
See the [runtime's filesystem rules](https://github.com/anthropics/sandbox-runtime#filesystem-configuration).

Network. Commands start with the package-host allowlist in
`surogates/tools/builtin/terminal.py`. Other destinations invoke the native
approval callback. Each root's proxy enforces its own grants. Deny local IPC
sockets, including Docker and the user session bus, and fail closed if the
required enforcement is unavailable. File operations make no network calls.

Environment. Build it explicitly from trusted local settings: `HOME`,
`LANG`, `TMPDIR`, cache variables and a locally discovered toolchain `PATH`.
Retain the proxy variables required by `srt`. Validate the operation's `env`
overrides. They cannot replace the sandbox's policy or protected environment
settings. Do not inherit the main process's credentials or run project-supplied
shell startup files outside the sandbox.

Path checks. Authorize normalized paths by directory components. Validate
existing targets and the parent of a new file. A `realpath` check followed by an
ordinary file open is vulnerable to symlink replacement. Use a filesystem
boundary or descriptor-relative helper that enforces containment during the
actual operation, and test symlink and rename races. Apply the same policy to
searches and downloads. Refuse special files and magic links.

Outside-folder grants name an exact file or directory and specify read or write
access. Only native approval creates one. Protected app state remains denied.
Filesystem grants affect newly launched commands. A denied command is never
rerun automatically because it may already have produced effects.

Approval prompts appear in a window the desktop draws itself, never in the
web page. When the agent window is hidden a system notification opens it. A
prompt names the agent, the session and exactly what is asked (command, domain
or path). Focus starts on Deny.

| Mode | Asks before |
|---|---|
| Work freely (default) | first browser use. Additional filesystem or network grants, as defined above and in Section 5 |
| Ask every time | the above, plus each command invocation and file mutation. Effectful browser operations and process stdin writes also require approval |

- Buttons: Allow and Deny. Allow for this session for a domain or a
  path. Stop asking for this session in "Ask every time", which switches the
  session to "Work freely".
- An approval is bound to the immutable operation ID and request digest. A
  reconnect reopens the same pending prompt without granting or executing twice.
  Local state owns the mode for the root and all its children.
- While a prompt is open the worker waits with the shared helper.
- Deny returns "the user denied this" as the operation result. The model sees it
  as the tool result.

Only the desktop can make a session less safe. The page runs server content,
so:

- The page may request "last used", "new" or the folder dialog. It never sends
  a path.
- The page may switch a session to "Ask every time". Switching to "Work freely"
  happens only in the desktop's own prompt or menu.
- Approvals are answered only in the desktop's window. The bridge has no call
  for answering one.

## 5. The local browser

Raw operations on the laptop. Logic in the worker.

| Operation | Playwright call |
|---|---|
| `navigate(url, wait_until)` | `page.goto` |
| `observe(script_id, parameters)` | `page.evaluate` with a bundled, versioned observation script |
| `evaluate(function_body)` | arbitrary `page.evaluate`. Treated as effectful, never executed in Node |
| `mouse(click / move / down / up / wheel, x, y, button)` | `page.mouse.*` |
| `keyboard(type / press, text or keys)` | `page.keyboard.*` |
| `screenshot(options)` | `page.screenshot`, returning bytes |
| `set_input_files(target, paths)` | `setInputFiles` after local file authorization |
| `close()` | closes this session's tab and owned popups |

A `DeviceBrowserClient` in the worker has the same public methods as
`KernelBrowserClient` (`surogates/browser/client.py`) for browser interaction.
The `@eN` reference cache and key-name mapping stay in Python. Observation
scripts are shared, versioned assets shipped with both the worker and desktop.
`observe` accepts their IDs and validated parameters. The server cannot label
arbitrary JavaScript as an observation. Every arbitrary `evaluate` requires
approval in Ask every time mode because it can click or send authenticated
requests. The laptop never receives Playwright code: `KernelBrowserClient`
today sends Playwright snippets to kernel-images' `/playwright/execute`, and
executing such
snippets on the laptop would let whoever controls the server run programs
there. Browser tools pick `DeviceBrowserClient` for desktop sessions and skip
`BrowserPool`, and with it browser-minute billing. Cloud profile import/export
and `storage_state` are unsupported for this adapter. The app keeps persistent
profiles on the laptop. Page-visible content and evaluated results can still
reach the cloud through browser tools.

Choosing the browser. Desktop Settings → Browser, stored locally, for
all agents. The setting is a program path, so it is never settable from the web
page.

- Automatic (default): the first supported browser found.
- Each Chromium-based browser detected, with its version: Chrome, Chromium,
  Edge, Brave, Vivaldi.
- Custom…: a program file selected in the native dialog. `--version` helps
  identify it, but executes that program and is not a safety or compatibility
  check. Accept only after explicit local selection and a bounded launch test.
- Firefox is not offered (Playwright drives only its own patched Firefox). The
  setting says so.
- Snap and Flatpak builds are not supported: their confinement interferes with
  the profile folder and pipe control. Ubuntu ships Chromium only as a Snap, so
  on a standard Ubuntu install the supported browsers are the `.deb` builds of
  Chrome, Edge, Brave and Vivaldi. Chromium counts only when installed from a
  non-Snap source.
- A change applies the next time an agent's browser starts. Pin `playwright-core`
  and maintain a tested browser/version matrix. Detection alone cannot promise
  support for Brave, Vivaldi or a newly updated Chromium build. See
  [Playwright's executable-path guidance](https://playwright.dev/docs/api/class-browsertype#browser-type-launch-option-executable-path).

No supported browser. Browser tools return "No supported browser on this
computer. Install Google Chrome, Microsoft Edge, Brave or Vivaldi, or pick one
in Settings → Browser. The Snap build of Chromium is not supported." The agent
continues with its
other tools. The web client's browser pane shows the same message with a button
to desktop Settings → Browser.

Profiles. One browser process per agent identity with a persistent profile
under the app's private config directory at
`browser-profiles/<identity-hash>/<browser-id>`. The identity includes server
origin, org, agent and user. Profiles are per browser. Switching browsers starts
with fresh logins and switching back restores the old profile. Parallel sessions
of that identity share login state and each get a tab. Closing a session closes
its tab and owned popups without ending another session's browser.

Launch headed with `chromiumSandbox: true` and `--remote-debugging-pipe`. No
debugging port is opened. Playwright's Chromium sandbox option defaults to false,
so assert the actual launch flags in tests. See the
[launch options](https://playwright.dev/docs/api/class-browsertype#browser-type-launch-option-chromium-sandbox).
Do not copy the user's normal browser profile or attach to their existing
browser process.

Downloads and uploads. Stage downloads in a private temporary directory,
then save to an authorized path in the session folder. Validate suggested names
and enforce the same conflict and approval rules as file writes. File inputs
accept locally authorized files only. Block agent-triggered native file pickers
that could bypass path checks.

Navigation and network policy. Chrome uses its own sandbox and is not
wrapped in `srt`. Agent-controlled navigation permits HTTP and HTTPS. Block file
and browser-internal schemes, including through redirects and popups. New tabs
inherit the calling session's policy before any agent action.

Top-level URL checks alone do not protect local services. The browser network
guard must also cover subresources, page JavaScript requests and WebSockets,
including requests from workers. Check resolved IPv4 and IPv6 destinations and
redirects to prevent DNS rebinding into loopback or private networks. A native
approval grants a specific local/private origin to one session. The browser
spike must prove that the shared-profile design can enforce this scope and that
requests cannot bypass the guard. If it cannot, change the browser isolation
model before offering private-network access. Unsupported request paths fail
closed.

Watching and taking over. The window is on the user's screen. The cloud live
view is not used. The web client's browser pane shows "The browser is open on
this computer" with Show browser and Take over / Hand back. The
buttons appear only in the desktop app on the computer the session is bound to.
Elsewhere the pane shows only the message. Taking over records a local pause and
dismisses queued browser approvals. Browser operations then return the existing
`paused_by_user` result. Handing back
requires a native user action or native confirmation. A web page cannot resume
agent control on its own.

## 6. Harness and api changes

Marking a desktop session. The session-creation request carries
`execution: {kind: "device", device_id}` and a folder display path. The api
validates that the device belongs to this user and agent and is not revoked.
Invalid or unsupported device execution returns an explicit error. Silently
stripping it would create a cloud session when the user chose local execution.
The server advertises `desktop_sessions: true` in `/api/v1/auth/config`.

- Create the root in a binding-pending state. Reject messages, uploads and
  scheduled work until the device acknowledges the binding over its authenticated
  link. The renderer cannot mark it bound. Section 8 defines the handshake.
- `execution` and `workspace_path` are server-owned after creation. Strip these
  keys from generic config updates. An existing cloud session cannot switch
  execution kinds. A device session cannot fall back to a cloud sandbox.
- `workspace_path` is the real folder path accepted by the local binding.
  Code that hard-codes `/workspace` reads it instead, including skill staging's
  `staged_at` and missions' `repo=`. Apply it after cloud workspace stamping so
  `stamp_workspace_config` cannot replace it with an S3 path.
- Keep storage metadata for compatibility with `create_child_session` until
  provisioning supports explicit workspace kinds. Every consumer, including
  cleanup, must branch on `execution` before using it. Unused metadata is not
  permission to read or write a cloud workspace.
- Children created by delegation, tasks or schedules inherit the root's device
  execution and binding, with its `workspace_path`. Child config cannot override
  these fields. New desktop roots are user-owned web sessions only.

One guard at the sandbox pool. Any sandbox request for a desktop session
goes to the `DesktopSandbox`, whoever makes it. The expert tool loop
(`surogates/tools/router.py:260`) and Arbor (`surogates/tools/builtin/arbor.py`)
call `pool.execute` directly with a default spec and would otherwise start an
empty cloud sandbox.

One resolver for everything else. `workspace_io(session)` returns
`StorageWorkspaceIO` for cloud sessions and `DeviceWorkspaceIO` for desktop
sessions. The touchpoints below call it instead of `StorageBackend` directly.
Cloud resolution must preserve `workspace_boundary` for managed channels and
storage prefixes. Preserve `_workspace_root_id`'s legacy behavior in
`surogates/api/routes/workspace.py`: use a recorded root when present, otherwise
the session's own ID. `sandbox_session_key` has a different legacy fallback and
cannot replace it blindly. Route skill staging and `media_gen` through this
resolver so children consistently use their root's workspace.

| Goes through the laptop | Switched off for desktop sessions (first release) | Not affected |
|---|---|---|
| file panel api routes: tree, file, upload, download, delete (`surogates/api/routes/workspace.py`) | checkpoints and rollback (already off by default. No routes exist) | memory and knowledge bases |
| message attachments: `uploads/…` and inlined text (`sessions.py:1096-1199`) | saga compensation via `_checkpoint` | Slack/Telegram attachment ingest |
| artifacts `_artifacts/…` (`surogates/artifacts/store.py`), including board tools | `/code` and `run_coding_agent` | cloud browser profiles |
| whiteboard canvas `_whiteboard/canvas.json` | Arbor | |
| research notes `.research/…` (tools move onto `WorkspaceIO`) | | |
| spilled tool output `.surogates-results/…` (already via the pool's `write_file`) | | |
| skill staging `.skills/…` | | |
| image reads for vision, `media_gen` output, browser screenshots | | |
| AGENTS.md / CLAUDE.md context and subdirectory hints (still injection-scanned) | | |
| end-of-turn changed-files list, via `changes(cursor)` | | |

A switched-off feature returns "not available for sessions on a local folder" as
its tool error.

Deleting a session never deletes the folder. `delete_session` and the
cleanup job call `delete_prefix`. For a device workspace it does nothing, and
the laptop refuses to delete the folder root. Deletion still cancels that
session's operations. Deleting a root retires its local binding on reconnect.
Child deletion leaves the shared root binding intact.

Offline and HTTP requests. The indefinite wait applies to harness tools.
Interactive file routes fail promptly with a structured `device_offline` error
and "The files are on <computer>, which is offline" when presence is absent.
Bound online reads by an HTTP deadline and cancel unfinished requests on expiry.
Uploads or deletes that need native approval return 202 with an operation ID.
The web client tracks that operation to completion and retries with the same
request ID. They must not hold an HTTP request open indefinitely or repeat a
mutation after a timeout. No live file list is served from a cloud cache.

New events. `device.waiting {device_id, device_name, reason}` and
`device.resumed` are added to `EventType` (`surogates/session/events.py`), the
SDK's `AgentChatEventType`, `AGENT_CHAT_LISTENED_EVENTS` and
`applyAgentChatEvent`. Persist the current wait reason so a page reload restores
it. Track waits per calling session, including disconnects after an operation
starts. Clear the state only when that session has no remaining blocked local
operations. Revocation appears as access revoked with a recovery action.

## 7. The desktop shell and sign-in

Agents window. Lists added agents with Open, Remove, Add agent. Opens at
start, and again when the app is launched while already running (single-instance
lock).

Adding an agent. The user pastes an HTTPS URL. The app normalizes its origin,
rejects embedded credentials and calls `GET /api/v1/auth/config` to check server
compatibility. A new origin always needs native confirmation, including after
a redirect. Private enterprise hosts are allowed after that confirmation with
normal certificate validation. Save the canonical origin and display name.
A `surogates://open?url=…` link opens an existing agent or requests the same
native add-agent confirmation. Links cannot supply local paths or grant access.

Settings. Browser (Section 5). Start at login, written as
`~/.config/autostart/surogate.desktop` (Electron's login-item API does not
support Linux). Keep running when all windows are closed (on by default).

Staying alive and quitting. Closing windows hides them. The device link
stays connected. Quit Surogate is in the tray menu where a tray exists, the
Agents window menu, and Ctrl+Q. It confirms when sessions are working. On GNOME
without a tray, launching the app again shows the Agents window.

Sign-in. Username/password and Firebase email/password work in the window
unchanged. Google and GitHub use a system-browser handoff with a loopback
redirect and PKCE (RFC 8252). Google requires the external browser flow.

1. The web client calls `desktop.signInExternally("google")` instead of
   `signInWithPopup`. GitHub uses the same provider-scoped handoff.
2. The main process listens once on `127.0.0.1:<random port>` and opens the
   system browser at `https://<agent>/login?desktop_state=<state>&code_challenge=<challenge>&port=<port>`.
3. The user signs in normally. The web client, seeing `desktop_state`, calls
   `POST /api/v1/auth/desktop/code` as the signed-in user. The api stores a
   one-time code bound to the S256 challenge, state, exact agent origin,
   authenticated user and loopback redirect URI in Redis for 60 s. Validate the
   port and permit only the fixed `127.0.0.1` callback, with no arbitrary redirect.
4. The browser redirects to `http://127.0.0.1:<port>/callback?code=…&state=…`.
   The main process checks `state`, the callback path and the pending origin,
   then exchanges the code with its verifier at
   `POST /api/v1/auth/desktop/token` for access and refresh tokens. The api
   validates the full binding and atomically consumes the code once. Limit
   attempts and expire the loopback listener on completion or cancellation.
5. After rechecking that the original window still has the expected origin,
   the main process hands the tokens to a narrow preload completion handler.
   It stores them using the existing `surogates_auth_token` and
   `surogates_auth_refresh_token` keys and reloads. The main process discards
   transient login tokens. No tokens or verifier enter URLs or logs.
6. The browser tab shows "Signed in, you can return to Surogate".

Signing out or removing an agent first suspends local bindings and cancels
that identity's work. Revoke its device token through
`DELETE /api/v1/devices/{id}`, close its browser host, then clear the window's
login storage and local device token. If offline, stop local work immediately
and show server revocation as pending until it can complete. The next online
sign-in reconciles that pending revocation before enabling execution. Browser
profile retention is an explicit local choice. Another user never inherits it.

## 8. The web client's desktop mode

Contract. `web/src/lib/desktop-bridge.ts` holds the bridge types and
`getDesktop()`, which returns `undefined` in a browser. `desktop/` imports the
same file for its preload and main-process handlers.

Versions. The bridge reports a `version`. The web client uses only calls
that version has. On a server without `desktop_sessions`, the app shows the web
client with ordinary cloud sessions and the Agents window notes "This server
doesn't support local folders yet".

| Call | Does | Safety rule |
|---|---|---|
| `registerDevice(token)` | verifies and stores the device credential. Returns device identity | bound to the sending window and canonical origin |
| `prepareFolder("last" \| "new" \| "pick")` | confirms the folder and mode locally. Returns display path, binding nonce and a one-time token | token is bound to this window and user, and expires after five minutes. The page never sends a path |
| `bindSession(sessionId, token)` | verifies the pending server session, persists its local binding and confirms over the device link | one use. Cannot change an existing binding |
| `getBinding(sessionId)` | reports folder, mode and device state to the owning window | returns no credentials. Native changes also emit an update |
| `setMode(sessionId, "ask")` | switches to "Ask every time" | safer direction only |
| `requestFreeMode(sessionId)` | opens the desktop's confirmation for "Work freely" | the desktop decides |
| `revealFolder(sessionId)` | opens the folder in the file manager | |
| `browser.show(sessionId)`, `takeOver`, `handBack` | Section 5 | handing back needs native confirmation |
| `signInExternally(provider)` | Section 7 | |
| `openSettings(section)`, `showWindow()` | opens desktop Settings. Raises the window | |

New chat. Under the composer, show the folder and mode from native state.
The default is the last folder used with this agent identity. If none exists,
create a new folder under `~/Surogate/<agent>/`. Change opens the native picker.
The first message uses this order:

1. `prepareFolder` obtains native confirmation and records the selected folder
   and mode under a short-lived token. The binding nonce may be sent to the
   server. The token is consumed only by the local bridge. Every new chat asks,
   including for the default folder, so a server cannot silently bind a session
   to a folder. The confirmation is a small native sheet showing the folder and
   mode; Enter accepts them, and Change opens the native picker.
2. Create a session with device execution, the display path and binding nonce.
   The api records it as binding-pending and sends its descriptor over the
   authenticated device link.
3. `bindSession` matches that descriptor to the prepared token and full agent
   identity. It persists the immutable root binding before sending confirmation
   over the link. The api marks the session bound and acknowledges it. Retrying
   this handshake for the same root and token is idempotent.
4. Only after that acknowledgement may the client upload attachments and send
   the first message. Failed creation expires the preparation. A disconnect
   after local persistence resumes the same handshake on reconnect.

New chats use local execution when the server advertises support. The legacy
server case follows the cloud-only behavior above and labels it clearly.
Existing cloud sessions keep their original execution kind.

Inside a local session. Header: folder name with Open folder, the mode,
"on <computer>". A "Waiting for <computer>" line driven by `device.waiting` /
`device.resumed`. File panel, artifacts and whiteboard keep their existing views.
File mutations also show pending native approvals and asynchronous operation
status. The browser pane follows Section 5. The sessions list shows a laptop
icon and the computer's name, including in the web browser.

Notifications (desktop only). The standard `Notification` API, shown through
the Linux notification service, when the window is hidden or unfocused: turn
finished (`session.complete`), the agent asked a question (`ask_user_question`
tool call), a new inbox item. A click calls `showWindow()` and opens the
session. Laptop approval prompts are raised by the desktop itself.

Settings → Devices (browser and desktop): name, created, last seen,
Revoke. Revocation invalidates the credential and cancels queued work before
closing the socket. A connected laptop stops the identity's running operations.
An offline laptop learns of revocation when it reconnects. Sessions show
"Local access revoked" rather than an ordinary offline wait.

Restoring access requires the same user to sign in and confirm the existing
folder bindings in the native app. A dedicated reauthorization endpoint rotates
the token on the same device record and advances its credential generation.
A different user gets a separate device and no inherited bindings. Cancelled
operations stay cancelled. Resuming work creates a new invocation.

## 9. Install and updates

Artifact. `surogate-desktop-<version>-linux-x64.tar.gz` (electron-builder
`dir` target, tarred) and `latest.json` with `{version, platform, arch, url,
sha256}` plus a detached Ed25519 signature over the exact manifest bytes.
The installer and app embed the trusted public key. Verify the manifest before
using its URL, then verify the archive's hash and platform. Build in a new
release-workflow job on GitHub-hosted runners. arm64 later.

Hosting. surogate.ai serves `https://surogate.ai/desktop/install.sh`. The
tarballs and `latest.json` live on Cloudflare R2. Enterprise installs host the
same signed files on their server or an internal URL (placed by the enterprise
install kit). The app updates from the base URL it was installed from, recorded
by the script, so an air-gapped install never contacts us.

`curl -fsSL <base>/desktop/install.sh | bash`

1. Check `/etc/os-release` and the architecture. Continue only when `ID` is
   `ubuntu`, `VERSION` contains `LTS`, `VERSION_ID` is 24.04 or later, and the
   machine is x64. Anything else stops with "Surogate Desktop supports Ubuntu
   24.04 LTS or a later LTS release (x64)" before any change is made.
2. Explain what needs `sudo`, ask once, then install `bubblewrap`, `socat` and
   `ripgrep` with apt, and, when `kernel.apparmor_restrict_unprivileged_userns=1`
   (the default on both releases), an AppArmor profile allowing user namespaces
   for the app and `bwrap`. The app never runs with `--no-sandbox`.
3. Verify the signed manifest and tarball, then extract into a new staging
   directory. Reject archive paths and link targets outside that directory.
   Move the verified tree into `~/.local/share/surogate/versions/<version>/`
   and atomically replace the `current` symlink.
4. Add `~/.local/bin/surogate`, a `.desktop` entry with an icon, and the
   `surogates://` handler via `xdg-mime`.
5. Check for a supported browser. Print a note if none (no failure).
6. Optional `--ca-cert <file>`: configure the company CA for the Electron and
   Chromium trust path, installing `certutil` if needed. Also configure the
   Node TLS clients used by the device link and updater with an explicit CA
   bundle. Test both paths on the supported versions. Keep certificate and
   hostname validation enabled. Store this setting locally.

Safe to re-run. Re-running repairs or updates. `--uninstall` removes the app,
launcher entry, link handler and autostart file, asks before deleting app data
(device tokens, browser profiles), and never touches session folders.

In-app updates. Check `<base>/desktop/latest.json` at start and every 6
hours. Accept only a newer signed version for the installed platform and update
channel. Download and verify into staging, then show Restart to update.
Switch atomically after hosts stop and operation journals are flushed. Keep the
previous version until a successful launch. Persisted bindings and operation
records must survive an update. `install.sh --version <x>` permits an explicit
rollback only when that version can read the current local state schema.

Missing dependencies. Checked at start. Local sessions fail with
"Surogate's sandbox tools are missing. Run the install script again" and the
Agents window says the same. No unsandboxed fallback.

Windows and macOS later. Windows: the same layout under `%LOCALAPPDATA%`,
installed by a PowerShell script. The command runner sits behind one module so
Windows can evaluate the pinned `srt` release's Windows support or a WSL2 runner
in its own spike. Do not carry Linux isolation or certificate assumptions over.
macOS requires code signing and notarization, with its own release job.

## 10. Testing and the first spike

First spike on clean Ubuntu 24.04 and 26.04 VMs, the oldest and newest
supported LTS releases. Record the exact Electron,
`srt` and `playwright-core` versions, with a written result and evidence for each
question. Passing these checks is required before planning the implementation.

| # | Question | If no |
|---|---|---|
| 1 | Do Electron and `srt` start under Ubuntu's AppArmor restriction with their sandboxes enabled? | adjust the profile and retest without disabling sandboxing |
| 2 | Do separate utility processes isolate concurrent roots' filesystem and network grants, including implicit temp paths? Do package installs work with redirected caches? | correct the policy and process model |
| 3 | Can later commands and the local browser reach a session's background server through authorized endpoints? | design a session runner or explicit forwarding, then retest grants and process cleanup |
| 4 | Can the pinned Playwright launch supported installed browsers over a pipe with Chromium sandboxing, authorized downloads and complete navigation/network enforcement? | narrow browser support or revise isolation before enabling the feature |
| 5 | Can worker recovery resume the recorded operation sequence without repeating an effect after result-delivery or Redis failures? | implement durable invocation replay before shipping the relay |
| 6 | Does loopback plus S256 PKCE sign-in work with Firebase Google and GitHub on a real agent, including rejection of replayed or mismatched codes? | correct the login-page flow |
| 7 | Does the actual file-operation boundary reject symlink and rename races, special files and protected paths during concurrent commands? | replace the unsafe filesystem primitive before proceeding |
| 8 | Does a killed tool host or Electron main process leave no running command descendants, and does restart report ambiguous effects as interrupted? | add process supervision and prove cleanup before replay |

Harness and api (pytest, testcontainers Postgres and Redis).

- Parametrize file, terminal, research and process tests over `LocalWorkspaceIO`
  and `DeviceWorkspaceIO` with an in-process fake laptop. Cover parsing from
  bytes and revision conflicts, plus interactive process input and PTY output.
- Relay tests cover reconnect, lost pub/sub notifications, Redis reset and
  worker failure after result receipt but before tool-result commit. Exercise
  multiple operations within one tool call and duplicate IDs with changed
  payloads. Include writes, deletes and browser effects in replay tests.
- Persist cancellation while offline and race it against completion and
  reconnect. Check retries older than the payload-retention window. Verify
  lease fencing and stale socket generations.
- Wait tests assert both slot counts under parallel and nested waits. Cover
  cancellation, failed reacquisition and shutdown without double release.
  `ask_user_question` must recover an answer whose Redis notification was lost.
- Pool requests for a desktop root never provision a cloud sandbox. The expert
  loop reaches the device adapter. Disabled Arbor calls return their feature
  error before doing work.
- Resolver tests cover every laptop touchpoint, managed-channel boundaries,
  legacy workspace IDs and child inheritance. Session deletion never removes
  the folder. Offline file routes return promptly, and approved HTTP mutations
  use stable request IDs.
- Creation rejects invalid execution settings and blocks work before the device
  binding acknowledgement. Test handshake recovery, immutable config, token
  scope and cross-user access. Revocation cancels work and reauthorization
  preserves device identity without replaying cancelled invocations.

Desktop (vitest plus Linux integration tests). Unit-test policy builders,
bridge sender validation and approval rules. Check observation-script IDs and
arbitrary JavaScript approval, including hand-back while the page is untrusted.
Exercise the operation journal through crashes at each transition. Use actual
filesystem races and sandboxed commands to prove containment and independent
session policies. A fake WebSocket server tests reconnect and protocol limits.

Browser tests cover popups, redirects and blocked schemes, with private-network
requests from subresources and workers. Verify launch flags, upload boundaries
and concurrent sessions sharing a profile. Installer tests cover manifest
signatures, archive traversal and crash recovery during the atomic switch.
Test `safeStorage` without a keyring (the `0600` fallback, its permissions and
its notice) and enterprise CA trust in both TLS stacks.

Web client and SDK (vitest). Test event reducers and restoration of persisted
wait state, including revocation. Exercise the full folder-binding handshake
against a fake bridge, pending file approvals and the older-server cloud path.
Without a bridge, the web client keeps its existing behavior.

End to end (CI, pinned `ubuntu-24.04`, xvfb). Playwright `_electron` drives
the real app against a local stack and a scripted fake OpenAI-compatible LLM.
Cover a file edit with a command, domain approval and browser use. Quit during
a command and expect interrupted on restart. Drop the connection around result
receipt and verify that the effect occurs once. Test binding failure before the
first attachment upload and pause during an offline wait.

Run installer and AppArmor acceptance checks in clean Ubuntu 24.04 and 26.04
desktop VMs with the
restriction explicitly enabled. A hosted CI image alone does not establish
behavior on a fresh Ubuntu installation.

Manual release check on a clean VM: install and sign in, use a local folder
with commands and the browser, then exercise quit and sleep recovery. Verify
native take-over, revocation recovery and an update with existing bindings.

## Build order

The work splits into sub-projects that can each be planned separately. A
suggested order, each ending in something demonstrable:

1. First spike (Section 10). Resolve the platform and recovery gates before
   building on them. Record any required changes to this design.
2. `WorkspaceIO` refactor of `file_ops`, `terminal`, `process_registry` and
   the research tools, with `LocalWorkspaceIO` only. Cloud behaviour unchanged,
   proven by the existing tests.
3. Device link and `DesktopSandbox`: device and operation tables, relay,
   wait ownership, durable replay and the pool guard. Add minimal native binding
   and approval controls with isolated tool hosts. Demo: an agent edits a local
   file, then recovers a connection loss without repeating the effect.
4. Desktop shell and web-client desktop mode: Agents window, sign-in
   handoff, bridge, new-chat folder and mode controls, approvals, disconnection
   handling, notifications.
5. Resolver and touchpoints (Section 6).
6. Local browser.
7. Install script, release job, in-app updates.
