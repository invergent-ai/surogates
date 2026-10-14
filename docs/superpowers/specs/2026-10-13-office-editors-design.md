# Office Editors — Design

**Status:** approved in brainstorming, revised after review, not yet planned
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
   agent's name, in every workspace kind. The user sees each edit arrive live,
   attributed to the agent (as a tracked change when track changes is on).
   Edits arrive whole, one piece per tool call (a paragraph or section, a block
   of cells, a slide), not streamed token by token.
3. **The editor is the only writer of an open document.** While a document is
   open, the agent edits it through the editor, and sandbox writes to that path
   are refused.
4. **Documents are read and written through the workspace layer**
   (`workspace_files()` / `session_files()`), the one every workspace route
   already uses, so device rules and project history apply. Only the runtime API
   writes saves; the runtime API signs users' editor configs and the worker signs
   the agent's, both through one shared `surogates/office/` module.
5. **Agent editing runs in a new `runtime-office` service**, not in the
   per-session browser pool: editing a document must not cost browser-minutes
   or start a browser pod.
6. **Shared infrastructure.** The document server keeps its state in the shared
   Postgres and in object storage; one new shared RabbitMQ carries its queues,
   so converters scale like any other worker.
7. **A conflicting save never overwrites**: it lands as a sibling copy.
8. **The tools borrow Claude Code's Docs and Slides patterns**: outline first
   with pending blocks, markdown as the writing format, stable block ids with
   guards ("their edit wins"), comments that call the agent, and slides written
   as HTML on a fixed canvas.
9. **The current office skills stay.** They build files outside the editor.
   Retiring them is a later decision made on Workspace-Bench results.
10. **The pdf.js viewer is deleted**; PDFs open in the editor.
11. **The `office:` configuration is optional** until every environment has the
    document server: without it the editor route answers 503 and the office tools
    are not offered. After the rollout every deployment has it.

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
- Exceptions inside a command are swallowed (`safePluginEval` in
  `sdkjs/common/macros.js`) and partial changes stay. `runtime-office` therefore
  wraps every command in its own `try`/`catch` and undoes the partial change
  itself.
- The callback's download URL uses the document server's browser-facing host;
  the runtime API rewrites it to the URL it reaches the server by.

## Architecture

```
 browser / Surogate Desktop (SDK file pane)              runtime-worker (office tools)
   │ ① editor config (adapter → [ops →] runtime API)        │ ⑤ signs the agent's config;
   │ ② api.js + co-editing WebSocket                        │   read / edit via runtime-office
   ▼                                                        ▼
 office-docs  ◄───────────────────────────── runtime-office (Node + headless Chromium,
 (Euro-Office proxy + docservice)              ②       one browser context per document per session)
   │ ③ fetch file   ④ save callback   ▲
   ▼                                  │
 runtime API ── pending saves (cache bucket) ── workspace layer ── bucket / project history / computer
   │
 office-converter ◄── RabbitMQ ──► docservice
```

| Component | Lives in | Role |
|---|---|---|
| `office-docs` | new; surogate-ops `charts/office` | Euro-Office nginx proxy + docservice: serves the editor, holds co-editing sessions. One replica. |
| `office-converter` | new; surogate-ops `charts/office` | Euro-Office converter workers: opens, saves, `/converter`. Two replicas. |
| RabbitMQ (`office-rabbitmq`) | new; surogate-ops `charts/office` | Euro-Office's conversion queue and messages between its processes. |
| runtime API (new routes) | surogates | Users' editor configs, file fetch, save callbacks, the pending-save writer, Command Service calls, `office_sessions`. |
| `surogates/office/` | new; surogates | The shared module: sessions, config signing, tokens, used by the runtime API and the worker. |
| `runtime-office` | new; surogates | The agent's editor sessions, the connector shim, op compilation, slide measurement. |
| Office tools | surogates worker | `office_read`, `office_edit`; the turn-end save. |
| SDK file pane + adapters | surogates SDK, web; surogate-ops frontend | The editor in the chat, save state, mentions, auto-open. |
| ops proxy routes | surogate-ops server | Forward the new routes, carrying the acting user. |
| Desktop approvals | surogates desktop | Ask once per document for editor saves. |

Surogate Desktop needs no separate work for the pane: it loads the surogates
web client, which embeds the SDK.

## Identity

- **Users through the surogates web client or Surogate Desktop:** the runtime
  API takes the user from their own token.
- **Users through ops:** ops calls the runtime API with its service account, so
  every ops user would look the same. Ops therefore sends the acting user's id
  and display name in `X-Surogate-Acting-User-Id` and
  `X-Surogate-Acting-User-Name` (percent-encoded), and the runtime API trusts
  them only on service-account tokens. Editor user ids are `user:<id>` for
  runtime users and `sa:<service account id>:<acting id>` for people acting
  through a service account.
- **The agent:** the worker builds and signs the agent's config itself
  (`user: {id: "agent:<agent_id>", name: <agent display name>}`) through the
  shared module. No route accepts an `agent:` identity, so a user cannot mint an
  agent config and forge its edits or comments. The worker's `HarnessAPIClient`
  carries the session user's token and is not used for this.

## Data flow

### `office_sessions`

A new table in the surogates database, created by `create_all` from
`surogates/db/models.py`.

| Column | Type | Meaning |
|---|---|---|
| `id` | uuid pk | |
| `path` | text | Workspace-relative path. |
| `location` | text | The resolved location key from the workspace layer (bucket + key, or device + folder path), so one file has one row however it is reached. The workspace layer exposes it. |
| `session_ids` | jsonb | The chats whose panes or agents opened the document; events go to all of them. |
| `doc_key` | text unique | The document server's key, 32 hex characters. |
| `base_version` | text | The file's version when the document server fetched it or a save landed: the storage ETag for bucket files, the device revision for computer files. |
| `status` | text | `open` or `closed`. |
| `editors` | jsonb | User ids from the latest status-1 callback. |
| `pending_save` | jsonb | A save waiting to land: cache-bucket key, the save's `lastsave` time, attempts. |
| `created_at`, `updated_at`, `closed_at` | timestamptz | |

A unique partial index on `location` where `status = 'open'` makes one open row
per file.

### Opening a document

`GET /sessions/{id}/workspace/editor?path=` (proxied by ops like
`/workspace/file`, adding the acting user):

1. Run the same access check as `/workspace/file`, plus the hidden-path check
   used for changes (`_validate_change`), so nobody edits `.threads/` or
   `_whiteboard/` files the file panel hides. Transcript pages and read-only
   chats get `mode: "view"`; everyone else who can open the workspace edits.
2. Resolve the file through the workspace layer. For a computer folder this
   needs the computer online and the file at most 50 MiB; otherwise the route
   returns the device error and the pane shows it.
3. Find the open `office_sessions` row for the location and reuse its key, or
   insert a new row with a random key (`INSERT … ON CONFLICT DO NOTHING` on the
   partial index, then re-select). A key stays in use until a status 2 or 4
   callback closes its row; there is no probe of the document server. If the
   server has forgotten a key, opening it just refetches the file.
4. Add the session to `session_ids` and return
   `{document_server_url, config, mentionable_users}`. The config is signed with
   `office.jwt_secret` and carries:
   - `document`: `fileType`, `key`, `title`, `url` (the file-fetch route),
     `permissions` (`edit`, `review`, `comment`, `download`, `print`).
   - `documentType`: `word`, `cell`, `slide` or `pdf`.
   - `editorConfig`: `callbackUrl`, `mode`, `lang`, `user` (`{id: "user:<id>",
     name}`), `coEditing: {mode: "fast", change: false}` (users cannot switch to
     strict mode, where the agent's edits would stop showing live),
     `customization` (`forcesave: true`, `uiTheme` from the app theme, our logo).

The file-fetch route records `base_version` when the document server downloads
the file, so the base is the version the editor actually opened.

Editable: `.docx`, `.xlsx`, `.pptx`, `.pdf`. Opened read-only (the editor
converts them for viewing): `.doc`, `.xls`, `.ppt`, `.odt`, `.ods`, `.odp`,
`.rtf`.

The file-fetch route `GET /office/files/{token}` and the callback route
`POST /office/callback/{token}` take a token signed with `office.token_secret`
(runtime API and worker only) that names the row and its purpose. File tokens
expire after 15 minutes. Callback tokens have no time limit; they are refused
once their row is closed. Every callback body is also verified against the
document server's JWT.

### Saving

| Callback status | Action |
|---|---|
| 1 (editor joined or left) | Store `editors`; touch `updated_at`. |
| 6 (forced save) | Take the save (below); keep the row open. Fires when a user clicks Save, at the end of every turn in which the agent edited the document, and every 5 minutes through `autoAssembly`. |
| 2 (last editor left, with changes) | Take the save, then close the row once it lands. |
| 4 (last editor left, no changes) | Close the row. |
| 3, 7 (save failed) | Log, emit `office.save_failed`, close the row on 3. |

**Taking a save.** The document server retries a callback only 3 times, 1, 2
and 4 seconds apart, while a computer can be offline or waiting for approval
much longer. So the callback route downloads the edited file at once (host
rewritten), stores it in the chat's workspace bucket under
`_office/pending/<doc_key>` (outside every chat's prefix; R2 tokens are scoped to
one bucket, so the runtime keeps to the bucket it already has), records it in
`pending_save`, and answers `{"error":0}`. A newer save of the same document
replaces a pending one. A worker job (beside the other job loops in
`orchestrator/worker.py`) lands it, claiming rows with `FOR UPDATE SKIP LOCKED`
so worker replicas never land one save twice, and retries until it lands. The pane shows "Saved" once it lands, and "Saving to <computer>…" while it
waits.

**Landing a save**, through a new guarded write on the workspace layer,
`write_if(path, data, revision)`, used only by office saves (every other write
keeps today's behaviour):

| Workspace | How the save lands |
|---|---|
| Plain cloud chat | The bucket write is conditional on the ETag (`storage.upload(..., if_tag=)`); `Changed` is a conflict. |
| Project, in the cloud | The runtime API lands the save into the project's real files and history itself, the way it already lands a user's changes (`BucketHistory` in `surogates/workstreams/bucket.py`), under the project's lock, as one history entry. Its author comes from the users in the save's `history.changes`: "you" for a user's edits, the editing thread for the agent's, both when both edited. |
| Project, on a computer | The project's coordinator session is bound to the real folder; the save is a device write through that binding, conditional on the device revision. The computer records it as your edits at its next landing. |
| Folder on the user's computer | A device write conditional on the device revision (`expected_revision`), made with the session owner's device access. |

Device writes from the editor carry an `editor:` request prefix. Surogate
Desktop's approvals (`desktop/src/binding/approvals.ts`) ask once per document
per editing session in an "Ask every time" folder, then let that document's
editor saves through. Today only the whiteboard canvas is exempt.

**Conflicts.** When a guarded write finds the file changed outside the editor,
the save lands as a sibling copy named `<stem> (edited <YYYY-MM-DD HH.MM> UTC)<ext>`
(` 2`, ` 3`… if taken), the row closes, and the runtime API sends the `drop`
command so the editors reload. The pane reopens the original as it now stands
and shows a notice linking to the copy (`office.conflict`,
`{path, conflict_path}`).

**Freshness.** Between saves the stored file can lag the live document by up to
5 minutes (`autoAssembly`). At the end of every turn in which the agent edited a
document, the worker saves it: `runtime-office` first waits until the agent's
editor has no unsent changes (the editor uploads them about a second after the
last edit), then the runtime API sends `forcesave` with the turn id as
`userdata` and waits up to 30 s for the callback that carries it. One turn, one
save, one history entry, which matches how landing records a project thread's
turn. `read_file` on a document with an open row says so and points the agent to
`office_read`.

### Projects

A project's threads work in copies of the project's files and land their changes
at the end of each turn. A document open in the editor is the project's real
file, shown to everyone in the project, and the editor is its only writer:

- The agent's office tools reach the real file through the editor, never the
  thread's copy.
- While a file has an open row, the sandbox refuses writes to that path in the
  thread's copy (the workspace layer's write check), with a message that points
  to the office tools. So landing never carries a thread's change to an open
  document; the editor's saves are what history records.

### Cleanup

A reaper closes rows that never saw a status-1 callback within 10 minutes (a
config fetched but never opened) and rows of sessions that are archived or
deleted; for the latter it first sends `drop`, so a deleted chat's document
cannot save back into its removed workspace.

### Failures

- **Document server down:** the pane shows an error and a Download button.
- **Computer offline:** opening a document returns the device error; an open
  editor keeps working, and its saves wait in the cache bucket until the
  computer is back.
- **Runtime API down during a callback:** the document server retries three
  times; after that the edits stay in the document server's database, and the
  next open of the key rejoins them.
- **Document server restart:** changes survive in Postgres and the cache bucket;
  `autoAssembly` limits any loss to 5 minutes.

## Agent tools

Two harness-side tools, registered in a new `surogates/tools/builtin/office.py`
(listed in `tools/runtime.py`, routed `ToolLocation.HARNESS` in
`TOOL_LOCATIONS`, in their own `office` toolset). They are offered in chats that
have a file pane (web, Surogate Desktop, project threads) and not in voice,
public-website, Slack or Telegram chats, nor to strict coordinators.
`drop_unusable_tools` and the worker's effective-tools filter gate them the same
way, so the prompt and the offered tools agree.

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

Applies a batch of ops atomically in the agent's editor session and returns each
op's result, including the ids of new blocks so the next call can address them.
If the path does not exist, the worker first writes Euro-Office's blank template
of that type there, and the new document opens in the user's pane. One call
should carry one piece of work: a section, a block of cells, a slide.

**Targets and guards.** A target is quoted words
(`{"find": "exact words", "nth": 1}`), a block id with its hash
(`{"block": "<id>", "hash": "<h>"}`) or a pending block (`{"pending": "<key>"}`).
The guard check and the edit run in the same editor command, so no concurrent
edit can slip between them. If a guarded block changed since the agent read it,
or quoted words are gone, the whole batch is refused and the result shows the
block as it stands: the user's edit wins, and the agent works from what is there
now. A batch that fails partway is undone by `runtime-office`.

**`.docx` ops:**
- `insert` (target, `side: before|after`, content); `{"end": true}` targets the
  end of the document.
- `replace` (target, content). On quoted words with `{"text": …}` content it
  re-words in place and keeps formatting; on a block or pending block it
  replaces it.
- `delete` (target), `move` (target, `to`, `side`).
- `comment` (anchor words, body), `reply` (thread id, body), `resolve` (thread
  id).

Content is markdown with typed blocks, Claude Docs style:
`{"markdown": "...<?block k?>...", "blocks": {"k": …}}`, where a block is
`{"type": "pending", "intent": "Risks: what could slip"}`,
`{"type": "chart", "chart": "bar|line|pie|…", "rows": […]}` or
`{"type": "image", "path": "<workspace path>"}` (the worker reads the image
through the workspace layer and hands its bytes to `runtime-office`). Headings
map to the document's own Heading 1–3 styles, lists to its numbering, tables to
its default table style. A pending block is a block-level content control tagged
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
be ones the document server has; `runtime-office` ships the same font set.
Chromium's text layout is close to the editor's but not identical, so the check
catches clear overflow, not every pixel.

**`.pdf` ops:** `pages` (`add`, `remove`, `rotate`), `fill` (field values),
`add_field` (type, page, rect, name), `redact` (search text; applied on save and
irreversible), `annotate` (`free_text` or `stamp`, page, rect, text), and
`replace` of existing text through `RecognizeContent`, pending the font check
in "Verify first".

**Resumed tool calls.** A worker that resumes a session can repeat a tool call.
The worker records each applied `office_edit` in an `office_edits` table
(`tool_call_id`, row id, result); a repeated id returns the stored result
without applying anything. A crash between applying and recording can still
repeat one edit; this is a known gap.

### The agent's editor sessions (`runtime-office`)

A Node service with one headless Chromium and **one browser context per page**,
one page per document per chat session. Internal API, behind a bearer token
(`office.runtime_office_token`):

- `POST /pages` `{doc_key, session_id, config, document_server_url}` → page id
  (idempotent per document and session).
- `POST /pages/{id}/read`, `/edit`, `/flush`; `DELETE /pages/{id}`.
- `GET /health` → open pages and the cap.

The page hosts the editor with the agent's signed config and registers the
connector shim. Markdown is parsed and ops are compiled to Office JS API code in
`runtime-office`; guards run inside the same command as the edit. `/flush` waits
until the editor has no unsent changes. A page closes after 10 minutes without a
call, so the agent leaves the co-author list. A cap on open pages per replica
(set from the memory measured in "Verify first") turns excess opens into
"office service busy, retry". With one replica and an estimated 10 pages, that
is about 10 documents being edited by agents at once across the platform; the
cap and the replica count are revisited once the page memory is measured.

### Watching the agent work

- When an `office_edit` tool call arrives live (not during history replay), the
  SDK opens that file in the pane through `handleOpenFilePreview`, unless the
  user is looking at another file.
- `office_edit` joins `WORKSPACE_MUTATING_TOOLS`
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
  `POST /sessions/{id}/workspace/editor/mention` for the chat the user is in,
  which appends a `user.message` to that session ("[Comment on report.docx]
  Flavius: '…' on '…'", comment text framed as untrusted data, the thread id from
  `actionLink`) and wakes it.
- Before the model runs, the worker posts a short acknowledgement in the thread
  ("On it, I'll reply here when done"). The agent then acts, replies in the
  thread in one or two sentences, and resolves it.

### The `office` skill

A bundled skill with:

- The workflow: read before editing; for a new document, plant the outline as
  pending blocks first, then fill one section per `office_edit` call, with a
  short chat line between calls; a document open in the editor is edited only
  with the office tools, never with the sandbox skills; findings go in the
  document, and the chat ends with one line saying what the user can do (edit,
  comment).
- Writing rules: lead with the answer, specifics over adjectives, change only
  what was asked.
- `references/word.md`, `cell.md`, `slide.md` (format, design rules: one idea
  per slide, a type scale of four or five sizes, a hex palette, contrast, the
  vertical-space arithmetic, "too much? split the slide; nothing shrinks"),
  `pdf.md`.

## Deployment

### Document server

- **Images:** Euro-Office's orchestrated images (docservice and converter as
  separate containers, plus its nginx proxy and schema-init entrypoints), v9.3.5
  pinned **by digest**. Never `:latest`; it failed to start once (Euro-Office
  issue #381). The first plan confirms which image runs the proxy and the
  schema job.
- **`office-docs`:** a Deployment, one pod, two containers (proxy,
  docservice), one replica. Co-editing state lives in each docservice's memory
  and Euro-Office has no Redis store yet (server PRs #44/#46 open), so two pods
  serving one document could drop a save. Scaling it waits for that store or
  for document-key routing.
- **`office-converter`:** a Deployment with two replicas. Autoscaling
  waits until load shows the need.
- **Schema:** Euro-Office's `createdb.sql`, copied out of the image, applied once
  with `psql` to the `docs` database.
- **RabbitMQ:** one single-node StatefulSet per environment on an `emptyDir`
  volume (its contents are queued messages only), never on the `nfs` storage
  class.
- **Postgres:** a `docs` database and role on the shared cluster (`surogate-pg`
  in PROD, DEV and enterprise), created idempotently with `psql` through
  `kubectl exec` on the primary, like the Hub's database. Connect through
  `surogate-pg-rw`, not the PgBouncer pooler.
- **Cache bucket:** Euro-Office's `storage-s3` backend uses a new bucket
  (`surogate-office-cache` in PROD, `surogate-office-cache-dev` in DEV) with a
  bucket-scoped R2 token, path-style addressing and
  `useDirectStorageUrls: false` (browsers never fetch from the bucket); the
  `local.json` holding its keys is a Secret. On enterprise, a Garage bucket added to the
  kit's bucket loops.
- **Settings in a mounted `local.json`:** `autoAssembly` on (5 min interval),
  JWT on everything (browser configs, `/converter`, Command Service,
  callbacks), third-party plugins off if the connector works without them.
- **Network:** the document server reaches the runtime API by its public URL, so
  private-address filtering stays on in PROD (`ALLOW_PRIVATE_IP_ADDRESS` is set
  only in DEV, where the runtime runs on the workstation). Egress
  NetworkPolicies limit the document server and `runtime-office` to the runtime
  API, the document server, the cache bucket, Postgres and RabbitMQ; they are the
  first in the cluster.
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
replica; the page cap comes from the measured memory per page.

### Runtime configuration

A new optional `office:` section on `Settings`, `OfficeSettings` with
`env_prefix = "SUROGATES_OFFICE_"` and a `default_factory`. When `public_url` is
empty the editor route answers 503 and the office tools are not offered.

```yaml
office:
  public_url: https://office.surogate.ai                       # browsers load the editor from here
  internal_url: http://office-docs.office.svc
  callback_base_url: https://cloud.surogate.ai                  # document server → runtime API (public URL)
  runtime_office_url: http://runtime-office.surogates.svc.surogate.local:8080
  cache_bucket: surogate-office-cache
  jwt_secret: ...            # shared with the document server
  token_secret: ...          # runtime API and worker: file and callback tokens
  runtime_office_token: ...  # worker → runtime-office
```

### Delivery per environment

- **Chart:** a standalone chart, surogate-ops `charts/office`, installed as its own Helm release `office` in namespace `office` (like `browser-fleet` and `embedding-model`), with `values.yaml` for PROD and `values-dev.yaml` for DEV and secrets given at install time. The runtime chart `k8s/surogates-runtime` is stale and is not used.
- **PROD:** `helm upgrade --install office charts/office -n office --create-namespace -f <secrets>`, plus the DNS record, the R2 bucket and token, the `docs` database, and `runtime-office` (plan 3) added to master1's `restart-agent-pods.sh`. Order: database and bucket; then the release; then the runtime config.
- **DEV:** release `office` on node1 with `values-dev.yaml`, exposed under
  `office.densemax.local` with the mkcert certificate (running since 2026-10-13). The
  runtime and `runtime-office` run on the workstation; `runtime-office` loads the
  editor from the public DEV URL, and `callback_base_url` points at the
  workstation.
- **Enterprise:** a new stage in the `/work/eurolife` kit after `postgres` and
  `valkey`: the `docs` role and database, the Garage bucket, RabbitMQ, the
  document server, `runtime-office`, and mirrored images for air-gapped
  installs. Customer fonts go in through a mounted fonts folder.

### Operations signals

Save failures, conflicts, saves waiting for a computer, reaped rows and
`runtime-office` cap refusals are logged with session, path and document key.
Metrics wait until a dashboard needs them.

## UI (SDK)

- `WorkspaceFileDrawer` branches on the extension **before** it fetches the file:
  `.docx`, `.xlsx`, `.pptx`, `.pdf` and the read-only formats call
  `getWorkspaceEditorConfig` instead of `getWorkspaceFile` (which answers 415 for
  office files) and mount the editor: `api.js` loaded once from
  `document_server_url`, then `DocsAPI.DocEditor` with the config. The pdf.js
  viewer and the `pdfjs-dist` dependency (SDK and web) are deleted.
- The pane stays open while the user chats; it can expand to a large dialog for
  editing room. It shows the save state ("Saved", "Saving to <computer>…").
- New optional adapter methods `getWorkspaceEditorConfig` (with the app's
  `theme`, which is signed into the config) and `notifyWorkspaceEditorMention`,
  implemented in the surogates web adapter and the ops frontend adapter (with
  proxy routes copying the `/workspace/tree` pattern and adding the acting-user
  headers). A host without a document server, like the example app, leaves them
  out and office files show "Preview not available" with a Download link. ops
  picks them up with the next SDK release.
- Auto-open, the refresh hook and track-changes behaviour as described under
  "Watching the agent work". `office.conflict` reopens the original and links the
  copy; `office.save_failed` shows a notice.
- Surogate Desktop gets all of this through the web client. Its window policy
  confines only main-frame navigation, so the editor's iframe loads; the frame
  gets no permission grants (see Known gaps).

## Testing

- **Runtime API and the shared module, unit tests:** config and token signing;
  the acting-user header honoured only on service-account tokens; concurrent
  get-or-create on the partial index; every callback status; the pending-save
  writer's retries and request ids; `write_if` and the conflict copy for each
  workspace kind; the reaper.
- **`runtime-office` and the tools, integration tests:** a CI job that starts a
  pinned Euro-Office container and covers open, each view, every op type, guard
  refusal, undo of a failed batch, slide overflow refusal, flush, and the
  turn-end save round trip.
- **Desktop:** the approval prefix asks once per document and lets later editor
  saves through.
- **SDK, component tests:** the drawer's editor branch, auto-open live but not on
  replay, save state, the adapter calls.
- **End to end, on DEV:** a user opens a `.docx` in the pane, asks the agent to
  add a section, sees it arrive under the agent's name, edits beside it, and the
  save lands. Run once in a plain chat, once in a cloud project (one history
  entry for the turn, authored correctly) and once in a computer folder in "Ask
  every time" mode.

## Plans

| # | Plan | Delivers | Needs |
|---|---|---|---|
| 1a | Editing for users in the cloud | DEV document server (chart templates, RabbitMQ, database, bucket); `OfficeSettings`; `surogates/office/`; the editor, file and callback routes; `office_sessions`; pending saves and `write_if` for cloud chats and cloud projects (users' saves to a project's real files are picked up as "you" at the next landing); the reaper; the acting-user headers; the drawer's editor branch; pdf.js removed. Starts with verify item 1. | none |
| 1b | Computer folders | Device `write_if` with `expected_revision`, request ids, the coordinator binding for computer projects, desktop approvals for editor saves. Starts with verify item 3. | 1a |
| 2 | Rollout | PROD per-template applies, DNS, bucket, database, NetworkPolicies; the enterprise kit stage. | 1a, 1b |
| 3 | Live agent editing for documents | `runtime-office`; agent configs signed by the worker; `office_read` and `office_edit` (docx); the turn-end save; `BucketHistory` landing editor saves with their author; refused sandbox writes to open documents; `office_edits`; auto-open; the `office` skill (word); the `read_file` note. Starts with verify items 2 and 4–10. | 1a |
| 4 | Sheets, slides and PDF | xlsx ops; HTML slides measured into native shapes; PDF ops; the skill's cell, slide and pdf references. Starts with verify item 12. | 3 |
| 5 | Comments that call the agent | Mentions, the notify route, the acknowledgement, reply and resolve. Starts with verify item 13. | 3 |

## Verify first

1. Landing keeps a real file's newer contents when the thread's copy of that
   file did not change, in the cloud and on a computer.
2. `BucketHistory` can land an editor save as one entry authored by "you", by the
   editing thread, or by both.
3. The project coordinator's folder binding on a computer can serve the editor's
   reads and writes while the project's threads run.
4. The editor uploads the agent's edits within the flush wait, and `forcesave`
   with `userdata` comes back on the callback that carries it.
5. Undo of a partially applied batch works from the connector.
6. Agent edits pick up "track changes for everyone" set by a user.
7. Memory per editor page in `runtime-office` (sets the cap).
8. Paragraph and table ids are the same for every co-editor while a document is
   open (stable targets).
9. Content controls show their placeholder text (pending blocks).
10. The connector works with third-party plugins disabled.
11. Surogate Desktop's window policy lets the editor iframe load. Confirmed
    2026-10-13: only main-frame navigation is confined.
12. PDF text edited through `RecognizeContent` keeps the original embedded fonts
    (decides the PDF `replace` op).
13. `onRequestSendNotify` carries the comment id in `actionLink`.

## Known gaps

- **One docservice replica** until Euro-Office ships a Redis store for
  co-editing state.
- **About 10 agent-edited documents at once** platform-wide until the page memory
  is measured and the cap and replicas are revisited.
- **On a computer, a project's history records the agent's editor edits as yours**,
  because the computer lands the save; the document's own tracked changes still
  show who wrote what.
- **Phones open documents read-only** until Euro-Office merges its mobile
  editing PRs (web-apps #312–315).
- **A resumed tool call can repeat one edit** if the worker dies between
  applying and recording it.
- **A save can still race** an outside write in the moment between the version
  check and the write (the S3 conditional is a HEAD before the write, not a
  native `If-Match`).
- **The slide overflow check is approximate**, because Chromium's text layout
  differs slightly from the editor's.
- **The agent lingers** in the co-author list up to 10 minutes after its last
  edit.
- **Computer folders need the computer online** to open a document, and files
  over 50 MiB cannot be opened there.
- **In Surogate Desktop the editor frame gets no permission grants**
  (`desktop/src/shell/window-policy.ts`), so copy and paste that needs the
  async Clipboard API fails there; keyboard copy and paste still work.
- **On a computer whose folder ignores case (macOS, Windows), a document opened
  under two spellings (`plan.docx`, `Plan.docx`) gets two editing sessions**: the
  second to save lands as a conflict copy, and a sandbox write under the other
  spelling is not refused.

## Deferred

- `office_run`, a raw Office JS API tool: model-written code steered by document
  or comment text could reach the network from the editor page.
- HTML preview and source editing.
- Retiring the `docx`, `xlsx` and `pptx` skills (decided later on
  Workspace-Bench results).
- Streaming text into the document as the model generates it.
- Desktop's own Library and History pages opening files in the editor.
- Version history inside the editor, design systems for decks, PDF to Excel or
  PowerPoint conversion.
