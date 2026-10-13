# Office Editors — Design

**Status:** approved in brainstorming, not yet planned
**Date:** 2026-10-13 (brainstormed 2026-10-04, revised after the desktop app and projects landed)

## Problem

A user and their agent cannot work on a document together. The agent builds
`.docx`, `.xlsx` and `.pptx` files with the bundled skills (docx-js, openpyxl,
pptxgenjs, run in the sandbox), and the user gets a finished file at the end.
The chat UI cannot show it: the workspace `FileViewer` previews PDFs (pdf.js),
images and text only, and the file route answers 415 "Binary files cannot be
viewed" for office files. Nobody can edit anything in the browser, and there is
no way to watch the agent write.

## Goal

The document stays open in the chat's file pane while the user talks to the
agent. The user watches the agent write into it, piece by piece, and edits the
same document at the same time. This works for Word, Excel, PowerPoint and PDF
files, in every kind of workspace: a plain cloud chat, a project, and a folder
on the user's computer (Surogate Desktop).

## Decisions

1. **Engine: Euro-Office** (github.com/Euro-Office), the AGPL fork of ONLYOFFICE
   Docs 9.3 led by IONOS and Nextcloud. It keeps the ONLYOFFICE integration API
   (`DocsAPI.DocEditor`, JWT-signed configs, save callbacks, `/converter`,
   Command Service) and removes the edition gates. Rejected:
   - *ONLYOFFICE Community 9.4*: one in-memory process (no clustering, unsaved
     changes lost on crash, conversions serialized), no mobile editing, no
     white-labelling, and the Automation API (`createConnector`) gated behind
     the `advancedApi` licence flag.
   - *Porting GenOffice* (genspark-ai/genoffice): a single-user Electron suite
     with no collaboration; we would build co-editing for three formats
     ourselves. Its agent workflow ideas are ported instead.
2. **The agent writes through its own editor session**, signed in under the
   agent's name, in every workspace kind. The user sees each edit arrive live as
   a tracked change by the agent. Edits arrive whole, one piece per tool call (a
   paragraph or section, a block of cells, a slide), not streamed token by
   token.
3. **The document server is a required component** of every deployment. The
   pdf.js viewer is deleted; PDFs open in the editor.
4. **The runtime API is the only component that reads or writes documents**,
   and it does so through `workspace_files()` / `session_files()`, the layer
   every workspace route already uses. It also alone creates editing sessions,
   signs editor configs and sends commands to the document server.
5. **Agent editing runs in a new `runtime-office` service**, not in the
   per-session browser pool: editing a document must not cost browser-minutes
   or start a browser pod.
6. **Shared infrastructure.** The document server keeps its state in the shared
   Postgres and in object storage; one new shared RabbitMQ carries its queues.
7. **A conflicting save never overwrites**: it lands as a sibling copy.
8. **The tools borrow Claude Code's Docs and Slides patterns**: outline first
   with pending blocks, markdown as the writing format, stable block ids with
   guards ("their edit wins"), comments that call the agent, and slides written
   as HTML on a fixed canvas.
9. **The current office skills stay.** They build files outside the editor.
   Retiring them is a later decision made on Workspace-Bench results.

## Spike results

Run on 2026-10-04 against `ghcr.io/euro-office/documentserver:v9.3.4-hotfix.1`
in local Docker, with a throwaway host page and Playwright.

| Question | Result |
|---|---|
| Can a hand-written connector edit an open document live? | Yes. About 40 lines of postMessage code registered a connector, read the document and inserted paragraphs; a second tab saw the edit immediately. |
| Can the agent be its own tracked-changes author? | Yes, when it has its own editor session. `GetReviewReport()` attributed every change to "Agent"; the saved file carries `<w:ins w:author="Agent">`. |
| Does `/converter` render pages? | Yes. `outputtype: "png"` with `thumbnail.first: false` returns a zip of per-page PNGs; PDF and PNG each took about 1 s for two pages. |
| Does the save callback deliver the edited file? | Yes. Status 2 arrived when the last editor closed, with a download URL to the edited file. |

Connector protocol, for `runtime-office` (Euro-Office ships no
`createConnector()` wrapper; the editor side is enabled because the server sends
`advancedApi: true`):

- Messages go **directly to the editor iframe** with
  `iframe.contentWindow.postMessage(JSON.stringify(msg), "*")`. Relaying them
  through the host window and `api.js` does nothing.
- Register: `{"type":"onExternalPluginMessage","subType":"connector","data":{"type":"register","guid":"asc.{<uuid>}"}}`.
- Command: `data = {"type":"command","guid":G,"data":"<code>","recalculate":true}`,
  where code is `var Asc = {}; Asc.scope = <json>; var scope = Asc.scope; (<fn>)();`.
- Method: `data = {"type":"method","guid":G,"methodName":"<name>","data":[<args>]}`.
- Replies arrive on the host window as
  `{"type":"onExternalPluginMessageCallback","data":{"guid":G,"type":"onCommandCallback","commandReturnData":…}}`
  (or `onMethodReturn` / `methodReturnData`).
- The callback's download URL uses the document server's browser-facing host;
  the runtime API rewrites it to the internal URL before downloading.

## Architecture

```
 browser / Surogate Desktop (SDK file pane)          runtime-worker (office tools)
   │ ① editor config (adapter → runtime API)            │ ⑤ open / read / edit / run
   │ ② api.js + co-editing WebSocket                    ▼
   ▼                                          runtime-office (Node + headless Chromium;
 runtime-office-docs  ◄───────────────────────  one page per document per session,
 (Euro-Office proxy + docservice)        ②       signed in as the agent)
   │ ③ fetch file   ④ save callback      ▲
   ▼                                     │ /converter
 runtime API ── workspace_files() ── bucket (cloud chat, project) or computer (device)
   │
 runtime-office-converter ◄── RabbitMQ ──► docservice
```

| Component | Lives in | Role |
|---|---|---|
| `runtime-office-docs` | new; surogate-ops chart | Euro-Office nginx proxy + docservice: serves the editor, holds co-editing sessions. One replica. |
| `runtime-office-converter` | new; surogate-ops chart | Euro-Office converter workers: opens, saves, `/converter`. Two replicas. |
| RabbitMQ | new; surogate-ops chart | Euro-Office's conversion queue and messages between its processes. |
| runtime API (new routes) | surogates | Editor configs, file fetch, save callbacks, Command Service calls, `office_sessions`. |
| `runtime-office` | new; surogates | The agent's editor sessions, the connector shim, op compilation, slide measurement. |
| Office tools | surogates worker | `office_read`, `office_edit`, `office_run`. |
| SDK file pane + adapters | surogates SDK, web; surogate-ops frontend | The editor in the chat, mentions, auto-open. |
| ops proxy routes | surogate-ops server | Forward the new runtime routes, like the existing workspace routes. |

Surogate Desktop needs no separate work for the pane: it loads the surogates
web client, which embeds the SDK.

## Data flow

### `office_sessions`

A new table in the surogates database, created by `create_all` from
`surogates/db/models.py`.

| Column | Type | Meaning |
|---|---|---|
| `id` | uuid pk | |
| `session_id` | uuid | The chat whose workspace holds the file; owns comment turns and events. |
| `path` | text | Workspace-relative path. |
| `location` | text | The resolved location key from `workspace_files()` (bucket + key, or device + folder path), so one file has one row however it is reached. |
| `doc_key` | text unique | The document server's key, 32 hex characters. |
| `base_version` | text | The file's version when last read or saved: the storage ETag for bucket files, the device revision for computer files. |
| `status` | text | `open` or `closed`. |
| `editors` | jsonb | User ids from the latest status-1 callback. |
| `created_at`, `updated_at`, `closed_at` | timestamptz | |

A unique partial index on `location` where `status = 'open'` makes one open row
per file.

### Opening a document

`GET /sessions/{id}/workspace/editor?path=` (proxied by ops like
`/workspace/file`):

1. Run the same access check as `/workspace/file`. Readers without write access,
   transcript pages and read-only chats get `mode: "view"`.
2. Resolve the file through `workspace_files()` and read its current version.
   For a computer folder this needs the computer online and the file at most
   50 MiB; otherwise the route returns the device error and the pane shows it.
3. Find the open `office_sessions` row for the location:
   - Found, and the document server still knows its key (Command Service
     `info` answers error 0): reuse the key, and the user joins the live
     session.
   - Not found, or the server answers error 1 (key unknown, e.g. after a
     restart): close any stale row and insert a new one with a random key and
     the current version as base. An `INSERT … ON CONFLICT DO NOTHING` on the
     partial index plus a re-select handles concurrent opens.
4. Return `{document_server_url, config, mentionable_users}`. The config is
   signed with `office.jwt_secret` and carries:
   - `document`: `fileType`, `key`, `title`, `url` (the file-fetch route),
     `permissions` (`edit`, `review`, `comment`, `download`, `print`).
   - `documentType`: `word`, `cell`, `slide` or `pdf`.
   - `editorConfig`: `callbackUrl`, `mode`, `lang`, `user` (`{id: "user:<id>",
     name}`), `coEditing: {mode: "fast", change: true}`, `customization`
     (`forcesave: true`, `uiTheme` from the app theme, our logo).

Editable: `.docx`, `.xlsx`, `.pptx`, `.pdf`. Opened read-only (the editor
converts them for viewing): `.doc`, `.xls`, `.ppt`, `.odt`, `.ods`, `.odp`,
`.rtf`.

The file-fetch route `GET /office/files/{token}` and the callback route
`POST /office/callback/{token}` take a token signed with `office.token_secret`
(runtime API only) that names the row and its purpose. File tokens expire after
15 minutes. Callback tokens have no time limit; they are refused once their row
is closed. Every callback body is also verified against the document server's
JWT.

The agent opens documents the same way: the worker calls the editor route
through `HarnessAPIClient` with the agent's identity
(`user: {id: "agent:<agent_id>", name: <agent display name>}`), so neither the
worker nor `runtime-office` holds the document server's secret.

### Saving

| Callback status | Action |
|---|---|
| 1 (editor joined or left) | Store `editors`; touch `updated_at`. |
| 6 (forced save) | Write the file; keep the row open; record the new version as base. Fires when a user clicks Save, when an agent edit asks for a save, and every 5 minutes through `autoAssembly`. |
| 2 (last editor left, with changes) | Write the file, then close the row. |
| 4 (last editor left, no changes) | Close the row. |
| 3, 7 (save failed) | Log, emit `office.save_failed`, close the row on 3. |

The route answers `{"error":0}` only after the write succeeded; on failure it
answers non-200 so the document server retries.

A write downloads the result from the document server (host rewritten to
`office.internal_url`) and stores it through `workspace_files()`:

| Workspace | How the write lands |
|---|---|
| Plain cloud chat | `upload(..., if_tag=<base_version>)`. `Changed` means the file was modified outside the editor: the save goes to a sibling copy instead. |
| Project | The same guarded write to the project's real file, recorded as a history entry attributed to whoever's changes the save contains (the callback lists their user ids: the user, the agent, or both). |
| Folder on the user's computer | A device write with `expected_revision = <base_version>`. A conflict means a sibling copy. |

The sibling copy is named `<stem> (edited <YYYY-MM-DD HH.MM> UTC)<ext>` next to
the original (with ` 2`, ` 3`… if taken), and an `office.conflict` event with
`{path, conflict_path}` makes the SDK show a notice and refresh the file tree.

**Freshness.** Between saves the stored file can lag the live document by up to
5 minutes (`autoAssembly`). Every agent edit asks for a forced save and waits
for it (up to 10 s), so the agent's sandbox tools see its own changes. A
Command Service answer of error 4 (nothing changed) returns at once.

### Projects and computer folders

A project's threads normally work in copies of the project's files and land
their changes at the end of each turn; landing is what records history and
Undo. A document open in the editor is different: the editor holds the live
version, and both the user and the agent edit that one. The rules:

- The agent's office tools always edit the real file through the editor, never
  its thread's copy.
- **While a file has an open `office_sessions` row, landing leaves that file
  alone**; the editor's saves are what history records.
- In an "Ask every time" folder on a computer, the user approves the agent's
  editing **once per document**, when the agent first edits it, not once per
  save.

The first plan verifies both mechanics against the landing code and the device
approval code before building on them (see "Verify first").

### Failures

- **Document server down:** the pane shows an error and a Download button.
- **Computer offline:** the editor route returns the device error; an open
  editor keeps working, and its saves retry until the computer is back.
- **Runtime API down during a callback:** the document server retries; unsaved
  changes stay in its database; the next open rejoins them because `info` still
  knows the key.
- **Document server restart:** changes survive in Postgres and the cache bucket;
  `autoAssembly` limits any loss to 5 minutes.

## Agent tools

Three harness-side tools, registered in a new `surogates/tools/builtin/office.py`
(listed in `tools/runtime.py`, routed `ToolLocation.HARNESS` in
`TOOL_LOCATIONS`, in their own `office` toolset). They are offered to every
session by default; an agent's tool settings can exclude them, and the schema
filter (`drop_unusable_tools`) and the worker's effective-tools filter must
agree, or the prompt and the offered tools drift apart.

### `office_read(path, view, target?, query?)`

Opens the agent's editor session on the document if needed and returns:

- `outline`
  - `.docx`: blocks in order with a stable id, an 8-character hash of the text,
    the kind (heading level, paragraph, list item, table, image, pending
    block) and the first words; tracked changes and comment threads with ids.
  - `.xlsx`: sheets with their used ranges and header rows.
  - `.pptx`: slides with their id, title, layout and shape count.
  - `.pdf`: pages with their first text line, form fields and annotations.
- `view`: one block, table, cell range, slide or page in full.
- `search`: matches for `query`, with block ids, cell references, slide ids or
  page numbers.

Every result starts with who has the document open ("open by Flavius since
14:02").

### `office_edit(path, ops)`

Applies a batch of ops atomically in the agent's editor session, asks for a
forced save, and returns each op's result, including the ids of new blocks so
the next call can address them. If the path does not exist, it first creates an
empty document of that type, which opens in the user's pane. One call should
carry one piece of work: a section, a block of cells, a slide.

**Targets and guards.** A target is either quoted words
(`{"find": "exact words", "nth": 1}`) or a block id with its hash
(`{"block": "<id>", "hash": "<h>"}`). The guard check and the edit run in the
same editor command, so no concurrent edit can slip between them. If a guarded
block changed since the agent read it, or quoted words are gone, the whole
batch is refused and the result shows the block as it stands: the user's edit
wins, and the agent works from what is there now.

**`.docx` ops:**
- `insert` (target, `side: before|after`, content); `{"end": true}` targets the
  end of the document.
- `replace` (target, content). On quoted words with `{"text": …}` content it
  re-words in place and keeps formatting; on a block it replaces the block.
- `delete` (target), `move` (target, `to`, `side`).
- `comment` (anchor words, body), `reply` (thread id, body), `resolve` (thread
  id).

Content is markdown with typed blocks, Claude Docs style:
`{"markdown": "...<?block k?>...", "blocks": {"k": …}}`, where a block is
`{"type": "pending", "intent": "Risks: what could slip"}`,
`{"type": "chart", "chart": "bar|line|pie|…", "rows": […]}` or
`{"type": "image", "path": "<workspace path>"}`. Headings map to the document's
own Heading 1–3 styles, lists to its numbering, tables to its default table
style. A pending block is a block-level content control tagged
`pending:<key>` with the intent as placeholder text; the agent fills it later by
replacing it. If the user deletes it, the agent drops that section.

**`.xlsx` ops:**
- `set` (range, 2-D values; strings starting with `=` are formulas and stay
  live), with an optional `expect` of the values as read, which acts as the
  guard.
- `format` (range, number format, bold, fill, font colour, alignment, column
  width).
- `chart` (range, type, title, anchor cell).
- `sheet` (`add`, `rename`, `delete`).

**`.pptx` ops:**
- `slides`: add or replace slides from HTML, `{"id", "html"}` each, optionally
  `after` a slide id.
- `order` (ids), `remove` (ids).

A slide is one `<section>` on a fixed 1920×1080 canvas with inline styles from
a closed subset (px lengths, hex or rgb colours, flex or grid layout, padding,
gap, absolute positioning) and the elements `h1`–`h3`, `p`, `ul`/`ol`/`li`, `b`,
`i`, `u`, `a`, `span`, `div`, `img` (workspace path), `table`/`tr`/`th`/`td`,
`svg` (rasterized) and one trailing `<aside>` for speaker notes, at most 200
elements. `runtime-office` renders each slide in Chromium, measures every box,
and refuses a slide whose content overflows the canvas or its container, or
uses text under 24 px. Otherwise it builds native shapes at the measured
positions (1 px = 6350 EMU): painted `div`s become shapes, text elements text
boxes with their runs, `img` pictures, `table` tables, `aside` notes. Fonts must
be ones the document server has; `runtime-office` ships the same font set so
its measurements match the editor's rendering.

**`.pdf` ops:** `pages` (`add`, `remove`, `rotate`), `fill` (field values),
`add_field` (type, page, rect, name), `redact` (search text; applied on save and
irreversible), `annotate` (`free_text` or `stamp`, page, rect, text), and
`replace` of existing text through `RecognizeContent`, pending the font check
in "Verify first".

**Resumed tool calls.** A worker that resumes a session can repeat a tool call.
The runtime API records each applied `office_edit` and `office_run` by
`tool_call_id` with its result; a repeated id returns the stored result without
applying anything. A crash between applying and recording can still repeat one
edit; this is a known gap.

### `office_run(path, code, scope?)`

Runs Office JS API code (`Api.GetDocument()`, `Api.GetActiveSheet()`…) for
whatever the ops do not cover: page setup, styles, accepting tracked changes.
The code is a function expression; `scope` becomes `Asc.scope`; the return
value must be plain data. Each run is one undo step and is undone if it throws;
the error comes back with the API message. It asks for a forced save like
`office_edit`.

### The agent's editor sessions (`runtime-office`)

A Node service with one headless Chromium and one page per document per chat
session. Internal API, behind a bearer token (`office.runtime_office_token`):

- `POST /pages` `{doc_key, session_id, config, document_server_url}` → page id
  (idempotent per document and session).
- `POST /pages/{id}/read`, `/edit`, `/run`; `DELETE /pages/{id}`.
- `GET /health` → open pages and the cap.

The page hosts the editor with the agent's signed config and registers the
connector shim. Markdown is parsed and ops are compiled to Office JS API code in
`runtime-office`; guards run inside the same command as the edit. A page closes
after 10 minutes without a call, so the agent leaves the co-author list. A cap
on open pages per replica (set from the memory measured in "Verify first")
turns excess opens into "office service busy, retry". One replica to start.

### Watching the agent work

- When an `office_edit` or `office_run` tool call arrives live (not during
  history replay), the SDK opens that file in the pane through
  `handleOpenFilePreview`, unless the user is looking at another file.
- The office tool names join `WORKSPACE_MUTATING_TOOLS`
  (`sdk/agent-chat-react/src/runtime/events.ts`) so the file tree refreshes.
- Agent edits follow the document's track-changes mode: when the user turns on
  "track changes for everyone", the agent's edits arrive as tracked changes
  under its name.

### Comments that call the agent

- `mentionable_users` lists the agent (`id: "agent:<agent_id>"`, its name and a
  pseudo-address), which the SDK returns for the editor's `onRequestUsers`.
- When a user @mentions the agent in a comment, the editor fires
  `onRequestSendNotify` in their browser. The SDK forwards it through the new
  adapter method `notifyWorkspaceEditorMention` to
  `POST /sessions/{id}/workspace/editor/mention`, which appends a `user.message`
  to the session ("[Comment on report.docx] Flavius: '…' on '…'", comment text
  framed as untrusted data, the thread id from `actionLink`) and wakes it.
- Before the model runs, the worker posts a short acknowledgement in the thread
  ("On it, I'll reply here when done"). The agent then acts, replies in the
  thread in one or two sentences, and resolves it.

### The `office` skill

A bundled skill with:

- The workflow: read before editing; for a new document, plant the outline as
  pending blocks first, then fill one section per `office_edit` call, with a
  short chat line between calls; findings go in the document, and the chat ends
  with one line saying what the user can do (edit, comment).
- Writing rules: lead with the answer, specifics over adjectives, change only
  what was asked.
- `references/word.md`, `cell.md`, `slide.md` (format, design rules: one idea
  per slide, a type scale of four or five sizes, a hex palette, contrast, the
  vertical-space arithmetic, "too much? split the slide; nothing shrinks"),
  `pdf.md`, and `api.md` for `office_run`.

## Deployment

### Document server

- **Images:** Euro-Office's orchestrated images (docservice and converter as
  separate containers, plus its nginx proxy and schema-init entrypoints), v9.3.5
  pinned **by digest**. Never `:latest`; it failed to start once (Euro-Office
  issue #381). The first plan confirms which image runs the proxy and the
  schema job.
- **`runtime-office-docs`:** a Deployment, one pod, two containers (proxy,
  docservice), one replica. Co-editing state lives in each docservice's memory
  and Euro-Office has no Redis store yet (server PRs #44/#46 open), so two pods
  serving one document could drop a save. Scaling it waits for that store or
  for document-key routing.
- **`runtime-office-converter`:** a Deployment with two replicas. Autoscaling
  waits until load shows the need.
- **Schema:** a Job runs Euro-Office's Postgres schema against a `docs`
  database.
- **RabbitMQ:** one single-node StatefulSet per environment on an `emptyDir`
  volume (its contents are queued messages only), never on the `nfs` storage
  class.
- **Postgres:** a `docs` database and role on the shared cluster (`surogate-pg`
  in PROD, DEV and enterprise), created idempotently with `psql` through
  `kubectl exec` on the primary, like the Hub's database. Connect through
  `surogate-pg-rw`, not the PgBouncer pooler.
- **File cache:** Euro-Office's `storage-s3` backend on a new bucket
  (`surogate-office-cache` in PROD, `surogate-office-cache-dev` in DEV) with a
  bucket-scoped R2 token, path-style addressing; keys in a Secret. On
  enterprise, a Garage bucket added to the kit's bucket loops.
- **Settings in a mounted `local.json`:** `autoAssembly` on (5 min interval),
  JWT on everything (browser configs, `/converter`, Command Service,
  callbacks), `ALLOW_PRIVATE_IP_ADDRESS=true` so it can reach the runtime API
  (only requests signed with our secret get through), third-party plugins off
  if the connector works without them.
- **Public host:** `office.surogate.ai` in PROD (`docs.surogate.ai` is the
  product documentation site), `office.densemax.local` in DEV, `office.<domain>`
  on enterprise. Traefik with cert-manager, like `channels.surogate.ai`; the
  WebSocket passes through. Internal traffic uses `*.svc.surogate.local`
  service names, never a hardcoded `cluster.local`.
- **Resources:** docservice 2 CPU / 4 Gi requested, 8 Gi limit; converters
  2 CPU / 2 Gi each. Liveness and readiness on `/healthcheck`.

### `runtime-office`

A ClusterIP Deployment from a new `images/office/Dockerfile` (Node, Playwright
Chromium, the document server's fonts), published as `surogates-office` by the
release matrix. It loads the editor from the internal document server URL. One
replica; the page cap comes from the measured memory per page (an estimated
400 MB, so about 10 pages in 4–6 Gi).

### Runtime configuration

A new `office:` section on `Settings`, `OfficeSettings` with
`env_prefix = "SUROGATES_OFFICE_"` and a `default_factory`, required at boot the
way `HubSettings` is (bootstrap raises when it is empty):

```yaml
office:
  public_url: https://office.surogate.ai                       # browsers load the editor from here
  internal_url: http://runtime-office-docs.surogates.svc.surogate.local
  runtime_api_internal_url: http://runtime-api.surogates.svc.surogate.local:8000  # document server → runtime API
  runtime_office_url: http://runtime-office.surogates.svc.surogate.local:8080
  jwt_secret: ...            # shared with the document server
  token_secret: ...          # runtime API only: file and callback tokens
  runtime_office_token: ...  # worker → runtime-office
```

### Delivery per environment

- **Chart:** new templates in `/work/surogate-ops/k8s/surogates-runtime/`
  (`office-docs`, `office-converter`, `office-rabbitmq`, `office-schema-job`,
  `office-deployment`), each gated on an `office:` values block and built like
  the voice role (#533); the public Ingress follows `channels-deployment.yaml`.
- **PROD:** `helm template runtime k8s/surogates-runtime -f values.production.yaml
  -s templates/<x>.yaml`, then `kubectl diff`, then `kubectl apply`, one template
  at a time. Never `helm upgrade` (the `runtime` release is stale). Also: the
  DNS record, the R2 bucket and token, the `docs` database, and
  `runtime-office` added to master1's `restart-agent-pods.sh`. Order: RabbitMQ,
  database and bucket; then the document server; then the runtime config; then
  the runtime upgrade.
- **DEV:** the same templates rendered with a dev values file and applied to
  node1, exposed under `office.densemax.local` with the mkcert certificate. The
  runtime still runs on the workstation, so `runtime_api_internal_url` points at
  the workstation, and `runtime-office` runs locally.
- **Enterprise:** a new stage in the `/work/eurolife` kit after `postgres` and
  `valkey`: the `docs` role and database, the Garage bucket, RabbitMQ, the
  document server, `runtime-office`, and mirrored images for air-gapped
  installs. Customer fonts go in through a mounted fonts folder.

### Operations signals

Save failures, conflicts and `runtime-office` cap refusals are logged with
session, path and document key. Metrics wait until a dashboard needs them.

## UI (SDK)

- `FileViewer` opens `.docx`, `.xlsx`, `.pptx`, `.pdf` (and the read-only
  formats) in the editor. The office branch goes before the image check, which
  today draws a base64 office file as a broken image. It loads `api.js` once
  from `document_server_url` and mounts `DocsAPI.DocEditor` with the config.
  The pdf.js viewer and the `pdfjs-dist` dependency (SDK and web) are deleted.
- The editor lives in `WorkspaceFileDrawer`, the right-hand pane, which stays
  open while the user chats; it can expand to a large dialog for editing room.
- New adapter methods `getWorkspaceEditorConfig` and
  `notifyWorkspaceEditorMention`, implemented in the surogates web adapter, the
  ops frontend adapter (with proxy routes copying the `/workspace/tree`
  pattern) and the example app. ops picks them up with the next SDK release.
- Auto-open, the refresh hook and track-changes behaviour as described under
  "Watching the agent work". `office.conflict` and `office.save_failed` show a
  notice.
- Surogate Desktop gets all of this through the web client. The first plan
  checks that its window policy (`desktop/src/shell/window-policy.ts`, the
  same-origin navigation rules in `main-window.ts`) lets the editor's iframe
  load.

## Testing

- **Runtime API, unit tests:** config and token signing; concurrent
  get-or-create on the partial index; every callback status; the conflict copy
  for each workspace kind; the `tool_call_id` journal.
- **`runtime-office` and the tools, integration tests:** a CI job that starts a
  pinned Euro-Office container and covers open, each view, every op type, guard
  refusal, undo on error, slide overflow refusal and the save round trip.
- **SDK, component tests:** the new `FileViewer` branch, auto-open live but not
  on replay, the adapter calls.
- **End to end, on DEV:** a user opens a `.docx` in the pane, asks the agent to
  add a section, sees it arrive as a tracked change by the agent, edits beside
  it, and the save reaches the workspace. Run once in a plain chat, once in a
  project and once in a computer folder.

## Plans

| # | Plan | Delivers | Needs |
|---|---|---|---|
| 1 | Platform and editing for users | The chart templates, RabbitMQ, database and bucket in DEV; `OfficeSettings`; the editor, file and callback routes; `office_sessions`; saves through `workspace_files()` in all three workspace kinds; ops proxy routes and adapters; the editor in the file pane; pdf.js removed. Starts with verify items 1–2 and 10. | none |
| 2 | Rollout | PROD per-template applies, DNS, bucket, database; the enterprise kit stage. Document server before the runtime upgrade. | 1 |
| 3 | Live agent editing for documents | `runtime-office`; `office_read`, `office_edit` (docx), `office_run`; the `tool_call_id` journal; auto-open; the `office` skill (word). Starts with verify items 3–8. | 1 |
| 4 | Sheets, slides and PDF | xlsx ops; HTML slides measured into native shapes; PDF ops; the skill's cell, slide and pdf references. Starts with verify item 9. | 3 |
| 5 | Comments that call the agent | Mentions, the notify route, the acknowledgement, reply and resolve. | 3 |

## Verify first

1. How landing treats a file with an open editor session, and how to make it
   leave that file alone and record the editor's saves with the right
   attribution.
2. How "Ask every time" folders treat writes from the editor, and how to make
   the user approve the agent once per document.
3. A connector command is one undo step (the rollback on error relies on it).
4. Agent edits pick up "track changes for everyone" set by a user.
5. Memory per editor page in `runtime-office` (sets the cap).
6. Paragraph and table ids are the same for every co-editor while a document is
   open (stable targets).
7. Content controls show their placeholder text (pending blocks).
8. The connector works with third-party plugins disabled.
9. PDF text edited through `RecognizeContent` keeps the original embedded fonts
   (decides the PDF `replace` op).
10. Surogate Desktop's window policy lets the editor iframe load.

Plan 5 also confirms that `onRequestSendNotify` carries the comment id in
`actionLink`.

## Known gaps

- **One docservice replica** until Euro-Office ships a Redis store for
  co-editing state.
- **Phones open documents read-only** until Euro-Office merges its mobile
  editing PRs (web-apps #312–315).
- **A resumed tool call can repeat one edit** if the worker dies between
  applying and recording it.
- **A save can still race** an outside write in the moment between the version
  check and the write (the S3 conditional is a HEAD before the write, not a
  native `If-Match`).
- **The agent lingers** in the co-author list up to 10 minutes after its last
  edit.
- **Computer folders need the computer online** to open a document, and files
  over 50 MiB cannot be opened there.

## Deferred

- HTML preview and source editing.
- Retiring the `docx`, `xlsx` and `pptx` skills (decided later on
  Workspace-Bench results).
- Streaming text into the document as the model generates it.
- Desktop's own Library and History pages opening files in the editor.
- Version history inside the editor, design systems for decks, PDF to Excel or
  PowerPoint conversion.
