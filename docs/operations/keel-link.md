# keel-link

`surogates keel-link` is Keel's private door into the runtime. Keel, the case engine, calls it to start an agent's session for one step of a case, and then to answer it, ask it for a revision, send it a plain message, cancel it, and read its status, its events and its progress. It also lists, reads and writes a case's files, opens a case's document in the document editor for a person, and invites staff into an organisation. It is a process of its own, on a port of its own, and it serves nothing else.

## What it serves

| Route | What it does |
|---|---|
| `POST /v1/keel/step-sessions?agent_id=<agent>` | Starts the agent's session for one step, or finds the one Keel's key already started. |
| `POST /v1/keel/step-sessions/{session_id}/messages` | Gives the step a person's answer to its raised hand, a reviewer's request to revise its draft, or a plain message from whoever works on the case, and wakes the session. Each key is delivered once: `202 {"delivered": false}` when it was. A plain message also answers `resting`, and wakes no step that rests on its report. See [A message to a step](#a-message-to-a-step). |
| `POST /v1/keel/step-sessions/{session_id}/cancel` | Ends the step: removes its inputs from the session's config, pauses the session and interrupts its turn. `200 {"status": "paused"}`, also when it was paused already; each call interrupts the turn again. |
| `GET /v1/keel/step-sessions/{session_id}` | The session's `status` and `updated_at`. |
| `GET /v1/keel/step-sessions/{session_id}/events?after=<id>&limit=<n>` | A page of the session's events after the cursor `after`, without `llm.delta`: `200 {"events": [{"id", "type", "data", "created_at"}], "has_more"}`. See [Watching a step session](#watching-a-step-session). |
| `GET /v1/keel/step-sessions/{session_id}/events/stream?after=<id>` | The session's events after the cursor `after`, then each new one as it is written, as server-sent events. See [Watching a step session](#watching-a-step-session). |
| `GET /v1/keel/step-sessions/{session_id}/progress` | The session's status, its todo list as its agent last wrote it, and its last activity: `200 {"status", "todos", "todos_at", "last_activity"}`. See [Progress](#progress). |
| `GET /v1/keel/cases/{case_id}/files` | The case's files, by path: `{"files": [{"path", "name", "size", "modified_at", "editing"}]}`. `editing` names who is in the file's editor now, by display name, sorted; someone the runtime has no name for is `Someone`, or `An agent` for an agent. A case no session has written to yet has `{"files": []}`. The platform's own folders are left out. |
| `GET /v1/keel/cases/{case_id}/files/content?path=<path>` | The file's bytes, as `application/octet-stream`, with their SHA-256 hex digest in `X-Content-SHA256`. A file over `SUROGATES_KEEL_MAX_FILE_BYTES` is not read. |
| `PUT /v1/keel/cases/{case_id}/files/content?path=<path>` | Writes the body at the path, over any file there. `200 {"name", "uri", "sha256", "size"}`, where `uri` is `case-file://<case id>/<path>`: the value a step's file field holds. |
| `POST /v1/keel/cases/{case_id}/editor` | Takes `{"org_id", "path", "user_id", "access", "theme"}` and answers `200 {"document_server_url", "config"}`: one person's editor config for the document at the path. See [A case's document in the editor](#a-cases-document-in-the-editor). |
| `POST /v1/keel/orgs/{org_id}/members` | Takes `{"email", "display_name"}` and answers `200 {"user_id", "created", "joined", "sign_in", "set_password_token", "expires_at"}`: the org's user with that email, found, joined from another org or created. See [Inviting staff](#inviting-staff). |
| `GET /health`, `GET /health/ready` | The same liveness and readiness probes as the API. |

It has no JWT authentication, no web UI and no OpenAPI schema. Each route checks the link key instead. The public API never serves `/v1/keel/`.

A step session is created with the id Keel gives it, on the `api` channel, owned by the org's `case-type:<case type>` service account, which nobody signs in as. Every session of the case carries the case's `case:<case id>` memory and workspace boundary. The step's inputs pass the prompt-injection screen before anything is written, and the session starts with one message that sets out the step: its inputs, as data, its outcomes and its checks.

Errors answer `{"detail": {"code", "message"}}`, and a message never repeats the case's data, an email or a token:

| Status | Code | When |
|---|---|---|
| 401 | `bad-link-key` | The bearer is not the link key, or no link key is set. |
| 404 | `unknown-agent` | No such agent, or it belongs to another org. |
| 404 | `unknown-session` | No such session, or it is not a step's own session. |
| 404 | `unknown-file` | No file at the path in the case's workspace. |
| 404 | `unknown-user` | The editor was asked for a user who does not belong to the org. |
| 404 | `unknown-org` | An invitation into an org that does not exist. |
| 409 | `agent-stopped` | The agent is stopped. |
| 409 | `key-in-use` | The key, or the session id, names another step's session. |
| 409 | `step-finished` | A message for a step that is over: its inputs were redacted. |
| 409 | `file-open` | A write over a file that is open in the document editor. |
| 409 | `document-closing` | The document's last save is still landing; ask for the editor again in a moment. |
| 409 | `ambiguous-email` | An invitation for an email that two or more users of other orgs have: none is chosen. |
| 409 | `other-firebase-project` | An invitation for an account that signs in with a Firebase project the org does not sign in with. |
| 413 | `too-large` | A file to write or read over `SUROGATES_KEEL_MAX_FILE_BYTES`. |
| 415 | `not-a-document` | The editor was asked for a file that is not an office document. |
| 422 | `inputs-blocked` | The prompt-injection screen blocked the step's inputs. |
| 422 | `invalid-path` | A file's path Keel may not name: see [A case's files](#a-cases-files). |
| 422 | `invalid-request` | The body does not validate (an invitation's email that is not one among them), a case id or an org id is not a UUID, or a step session's events were asked for with a cursor or a page size out of range. |
| 500 | `internal-error` | The database failed, other than while a step session's events or progress were read. Only the error's class name is logged. |
| 503 | `office-off` | The editor was asked for, and no document server is configured. |
| 503 | `unavailable` | The agent's runtime config could not be read, twice; the object store refused or did not answer a case's files; an invitation could not read the org's Firebase config; or the database failed while a step session's events or progress were read. Only the error's class name, and the case id, the org id or the session id, are logged. |

## A step's inputs

Once a step is over, cancelled among others, its inputs are removed from the session's config and the contract is marked `inputs_redacted`. A step marked so takes no more messages. The redaction covers the config only: the step's first message, which sets out the inputs, stays in the session's event log, which only keel-link serves, to Keel.

## A message to a step

`POST /v1/keel/step-sessions/{session_id}/messages` takes three kinds of message. `answer` is a person's answer to the step's raised hand, and `revision` a reviewer's request to revise its draft: each takes a `note`, and gives the step back. `message` is a plain word to the agent working the step, from a person or from another agent. It neither answers a raised hand nor reviews a draft.

A plain message's body is `{"kind": "message", "text", "sender", "idempotency_key"}`:

- `text` is 1 to 20000 characters;
- `sender` is the display name of whoever sends it, 1 to 200 printable characters, or null;
- `idempotency_key` is 1 to 200 characters;
- `note` must be null or absent.

An answer or a revision takes no `sender`, and a key the route does not know is refused: either is `422 invalid-request`.

The agent reads `<sender> sends you a message about this step: <text>`, or `Someone working on the case sends you a message about this step: <text>` without a sender. Then, after a blank line: `It does not answer a raised hand or review a draft. Take it into account, and go on with the step.` It reads it as it reads an answer: a turn under way at its next request to the model, otherwise the session's next turn. In the session's events, the message is a `user.message` whose `data` holds `content`, the text the agent reads, `synthetic: "keel_message"` and `keel_key`, the idempotency key.

The answer is `202 {"delivered", "resting"}`. Each key is delivered once: `delivered` is false when the key was delivered already, and nothing is then written or woken.

A message wakes a step whose session is at rest, as an answer wakes it, except a step that rests on its report. A step rests on its report when its last report ended it for this round and it waits for a person: a raised hand, or a draft under review. Such a step is not woken, and keeps its report's mark. Its message stays unread until Keel's next answer or revision wakes the step, and that turn reads the message first. `resting` is true then, and false otherwise; a repeated call answers it as the step stands at that call.

A message never gives the step back. An answer or a revision ends the rest a report began, and the step then reports to Keel again. A message does neither, and a message that reached the step since a report began does not keep the report's mark from being written. If the turn that made the report reads the message, a further report in that turn is answered by the worker and never reaches Keel.

A step that is over takes no message: `409 step-finished`, as for an answer. The route logs neither a message's text nor its sender.

## Watching a step session

Keel reads a step session's events, by page and live, and its progress, to show what the agent does on the step. The runtime keeps each session's events as an append-only log, and an event never changes once written:

- `id` is an integer that only grows, unique across all sessions;
- `type` is the event's type, and `data` its payload as the runtime wrote it, a JSON object;
- `created_at` is when it was written, in ISO 8601 with its offset: UTC, `+00:00`.

The runtime adds event types and payload keys over time, so a reader keeps the types it knows and passes over the rest.

A cursor is the `id` of the last event read. `after` is a cursor: an integer from 0 to 9223372036854775807, the largest id the database holds, written in digits alone. It is 0 by default, before the first event. Any other `after` is `422 invalid-request`.

### By page

`GET /v1/keel/step-sessions/{session_id}/events?after=&limit=` answers `200 {"events": [{"id", "type", "data", "created_at"}], "has_more"}`: the session's events whose `id` is greater than `after`, in `id` order, at most `limit` of them. `limit` is 1 to 500, and 200 by default; any other is `422 invalid-request`. `has_more` is true when more events follow the page: Keel reads on with `after` set to the page's last `id`.

A page never holds `llm.delta` events. Each is a piece of a model response still streaming, and the `llm.response` after it carries the whole response.

### Live

`GET /v1/keel/step-sessions/{session_id}/events/stream?after=` takes `after` as a page does. The link key, the session and `after` are checked before the stream opens, and refused with the JSON errors above.

The stream is server-sent events, framed as the runtime's own session stream frames them when it watches a session. In order:

1. `: connected`, a comment.
2. `event: stream.start`, whose `id` is `after` and whose `data` is `{}`.
3. Every event with an `id` greater than `after`, in `id` order, without `llm.delta`, each as `id: <event id>`, `event: <type>`, `data: <the event's data, as JSON>`.
4. `event: stream.live` with `data: {}` and no `id`, once: from here on, what comes happens now.
5. Each new event as it is written, `llm.delta` included, framed as in 3.

While no frame goes out for 15 seconds, the stream sends a `: keepalive` comment.

The stream ends:

- with `event: stream.timeout` and `data: {"reason": "max_duration_exceeded"}`, after 300 seconds of waiting for events (a stream that always has an event to send runs longer);
- with `event: session.done` and `data: {"reason": "archived", "status": "archived"}` once the session is archived;
- with `event: session.done` and `data: {"reason": "session_not_found"}` once the session is gone.

A session at rest (`completed`, `paused` or `failed`) does not end the stream: a step rests between its turns, and Keel's next message wakes it. A failure while the stream is open closes it without a frame.

To go on after an end, or after a dropped connection, Keel opens the stream again with `after` set to the last event `id` it received. Every event then reaches Keel exactly once.

A stream counts as no one reading the chat: the session's check-ins and completions still reach the inbox, as they do while nobody reads it. Neither route logs an event's data.

### Progress

`GET /v1/keel/step-sessions/{session_id}/progress` answers `200 {"status", "todos", "todos_at", "last_activity"}`:

- `status` is the session's status, as `GET /v1/keel/step-sessions/{session_id}` gives it: `active`, `completed`, `paused`, `failed` or another the runtime adds.
- `todos` is the session's todo list, as its agent last wrote it with its `todo` tool: a list of `{"id", "content", "status"}` in the agent's order, whose `status` is `pending`, `in_progress`, `completed` or `cancelled`. It is null when the agent never wrote one, and `[]` when it emptied it. `todos_at` is when the list was last written, or null.
- `last_activity` is the session's newest event that is not an `llm.delta`, as `{"event_id", "type", "at", "tool"}`. `tool` is the tool's name when the event is a `tool.call` or a `tool.result`, and null otherwise. `last_activity` is null for a session with no events.

`todos_at` and `at` are in UTC, `+00:00`, as an event's `created_at` is. Only the step's own session is read: a helper the step started keeps its own list, which progress does not give. Stored data out of its shape never fails the read: a todo item out of its shape is left out, a stored list that is not one reads as `todos` null, and a newest event out of its shape as `last_activity` null. Each is logged by the session id and the kind of error alone. The route logs neither the list nor an event's data.

## A case's files

A case's files are the workspace every step session of the case shares: the `case:<case id>` boundary's, in the session bucket (`SUROGATES_STORAGE_BUCKET`), under the installation's key prefix. keel-link reaches it with no session, so Keel can add a file before the case's first step, and the step sessions read what Keel wrote.

A path is relative to the workspace and `/`-separated; `.` and empty parts are dropped, and nothing else is changed. A path is `422 invalid-path` when:

- it is empty or absolute, or holds a `..` part, a NUL or a backslash;
- its first folder is one of the platform's own: `_artifacts`, `_whiteboard`, `.threads` or `_history`. These hold the platform's state, not a person's files, and the listing leaves them out too;
- a part of it, once the `.` parts are dropped, starts with `.`: a hidden file or folder, at the top or deeper. A step session stages its skills under `.skills` in the case's workspace, and a person never sees a skill, so the listing leaves these out too;
- its first folder is `root` or `workspace`. A step's output takes either off the path it names, so it would read another file;
- its object key, with the workspace's prefix, is over the 1024 bytes an S3 store takes.

The listing names only files these routes can read by the path it gives.

A write takes up to `SUROGATES_KEEL_MAX_FILE_BYTES`, and makes the bucket when it is not there yet. It is refused while the file is open in the document editor: the editor's next save would land over a file it never saw. A read is copied to a scratch file first, so its digest is sent before its bytes. A read of a file over `SUROGATES_KEEL_MAX_FILE_BYTES` is `413 too-large`, copied no further. None of these routes logs a path or a file's bytes.

## A case's document in the editor

`POST /v1/keel/cases/{case_id}/editor` gives Keel the editor config of one person for one document of the case's files, to open in the document server at `document_server_url`. The body is `{"org_id", "path", "user_id", "access", "theme"}`: `access` is `edit`, `comment` or `view`, and `theme` is `light` (the default) or `dark`. The path is checked as the file routes check it.

The person is the runtime user `user_id`, who must belong to the org `org_id`. `org_id` scopes only the user: the runtime does not check that the case belongs to that org, and the link key is trusted for any case. The editor shows them under their display name, with the editor user id `user:<user_id>`: the same as when they open a file in an agent's chat.

| `access` | The editor |
|---|---|
| `edit` | Opens in edit mode, with editing, reviewing and commenting on. |
| `comment` | Opens in edit mode, with editing and reviewing off and commenting on. |
| `view` | Opens read-only, without comments. |

The document server converts older formats (`.doc`, `.odt`, `.rtf`, `.xls`, `.ods`, `.ppt`, `.odp`) to show them, so these open read-only, without comments, whatever the access.

Everyone who opens the file shares its one editing session, whatever their access, and so does a step session of the case that has the file open. The document belongs to the case: it is read from and saved into the case's files, and stays open while anyone is in it, with or without a chat. While it is open, the listing's `editing` names who is in it, and a write over it is refused. Mentions are not offered: the config names nobody to mention. Like the file routes, it logs no path.

## Inviting staff

`POST /v1/keel/orgs/{org_id}/members` takes `{"email", "display_name"}` and gives Keel the org's user with that email. The email is compared without case.

- **Found.** A user of the org, home or member, has the email: `created` and `joined` are false.
- **Joined.** Exactly one user of other orgs has it: they become a member of the org, and `joined` is true. Two or more is `409 ambiguous-email`, and nothing is written.
- **Created.** Nobody has it: the account is created in the org, with that display name and the email trimmed and lowercased, and `created` is true. In an org with Firebase sign-in it is a Firebase account of the org's project without a Firebase identity yet: its first sign-in through the org's Firebase exchange links it by verified email. Otherwise it is a password account without a password.

An invitation never changes an existing account: not its password, its way of signing in, nor its display name. Joining adds only the membership.

`sign_in` is `firebase` for an account of the org's Firebase project, and `password` otherwise. An account of another Firebase project has no way into the org, and is refused with `409 other-firebase-project`, as is a Firebase account invited into an org without Firebase. When the org's Firebase config cannot be read, the invitation is `503 unavailable`: a password account is never created in an org that signs in with Firebase.

For a password account without a password whose home is the org, `set_password_token` is a new token and `expires_at` when it lapses, 7 days on; otherwise both are null. An account whose home is another org gets no token: its password is its home org's to set, since one password signs in to every org the account belongs to. Keel gives the token to the person, who sets their password through the runtime's public `POST /v1/auth/orgs/{org_id}/set-password`; see [Invited staff](../multi-tenancy/index.md#invited-staff). The token works once, and not after the password changed. The answer carries `Cache-Control: no-store`, and the token is never logged or stored. Neither is the email.

A person joined from another org who has no password yet gets no link from the org that joined them. Their home org invites them again: they are found there, and that invitation gives a fresh link. A re-invitation never revokes a link: an earlier one stays valid until a password is set or its 7 days pass.

## Keep it internal

Run keel-link where only Keel can reach it: an internal service, never a public ingress or load balancer. Whoever holds the link key can start sessions on any agent of any org, send any step's agent a message, read every step session's events and progress (its inputs and all its tools read among them), read and write any case's files, open them in the editor as any user, and invite anyone into any org, with the token that sets a new account's password.

keel-link reads the same state as the API (database, Redis, storage, the platform API), so it takes the same settings, plus its own:

| Setting | Held by | What it is |
|---|---|---|
| `SUROGATES_KEEL_LINK_KEY` | keel-link alone | The bearer Keel shows keel-link. |
| `SUROGATES_KEEL_URL` | the worker | The base URL of Keel's door, where the worker reports how a step ended, and where an intake agent starts cases. |
| `SUROGATES_KEEL_DOOR_KEY` | the worker alone | The bearer the worker shows Keel's door. |
| `SUROGATES_KEEL_MAX_FILE_BYTES` | keel-link | The largest file Keel may write to a case's workspace, in bytes: 52428800 (50 MiB) by default. |

keel-link listens on `SUROGATES_API_HOST`, port `SUROGATES_KEEL_LINK_PORT` (8090 by default). Give each process only its own key. Neither key is logged or shown in a settings repr. With the settings empty the integration is off: keel-link answers every call `401 bad-link-key`.

## How a step reports back

Only a step's own session is offered `complete_step` and `raise_hand`. The worker sends each to Keel's door at `SUROGATES_KEEL_URL`, with `SUROGATES_KEEL_DOOR_KEY`, and Keel's answer decides what the session does:

| Keel's answer | The session |
|---|---|
| `200`, or `409 not-running` | Its turn ends, and the session rests until Keel sends it something. A completed step's inputs are removed from its config. |
| Another `409` | A rejection: the model reads Keel's code, message and the attempts left, then fixes its report and calls again, or raises a hand. |
| No answer, `408`, `429`, or a `5xx` other than `503 harness-off` | The model calls again. A repeated call gets Keel's first answer. |
| Anything else | Keel refused the report for good: the worker tells Keel the step failed, removes the inputs, and ends the turn. People take the step. |

A raised hand carries its `question`, its `choices` and its `evidence`: at most 50 facts, each `{"label", "value", "source"?: {"file", "page"?}}`. `label` is 1 to 200 characters and `value` 1 to 500, counted once stripped; `page` is a whole number from 1; a null `source` or `page` is read as absent. `file` must be one the case's files show: the model names it in `/workspace`, and the report carries it as the case's files list it, relative to the case's workspace. A path outside the workspace, or in the platform's own folders, is refused. Whether the file exists is not checked.

A report Keel would refuse whole is never sent, and the model is told what to fix: one over 256,000 bytes of JSON, one holding NaN or an infinity, sources that are not lists of objects, a raised hand's evidence outside its shape, or a tool call id over 200 characters. A refused fact is named by its index, never by its path. Once a report has ended the step, a later report in the same wake is answered by the worker, without Keel.

A turn of the step that ends in words, with no report, is told once to report, and goes on. When it still ends without a report ending the step, the worker tells Keel `no-outcome`, with the code of how the turn ended and the step's usage, and the inputs stay. A turn that fails, and a session the worker gives up on because its wakes kept crashing or never recovered, is told to Keel as `failed`, with the failure's code and never its text, and the inputs are removed. A report that ended the step for this round (`completed`, `drafted`, `hand-raised`, or `stopped` when Keel refused it for good) is marked on the session until Keel's next answer or revision to the step clears it. So is a turn with no outcome that Keel takes as a raised hand. A mark is never written over an answer or a revision that reached the step after its report began. A marked step is never nudged, and no later end of its turns is told to Keel: a crash never fails a draft under review or a raised hand, nor removes the inputs they resume with. The same holds for a step whose inputs were already removed. Keel being unreachable never keeps a turn from ending: the worker logs it by session id.

A worker without both settings has no link to Keel: every report removes the step's inputs and ends the turn, and Keel learns of it only from the session's status.

## An intake agent starts cases

A case type can name agents as its intake agents: front doors, such as a WhatsApp agent, that talk with people and start their cases. At each wake of a conversation's own session, one that works on no case and was not started by another session, the worker asks Keel's door which case types the session's agent may start, and keeps the answer a minute per org and agent. The wakes that need an agent's list while it is being asked share that one ask, and a Keel that has not answered in 2 seconds grants nothing for the minute: the ask never holds a wake longer. While the list is not empty, the session is offered `start_case`: it takes only the listed case types, and its description names each one with the fields it requires. No session of a case, and no helper a conversation started, is offered it. A worker without both settings asks nothing and never offers `start_case`; one that cannot reach Keel offers none until it next asks.

Each call asks Keel to start one case for the person the conversation is with, the sender of its latest message, or for no user when that is a service account. It is keyed `intake:<session id>:<tool call id>`, so the same call sent again finds the case it started. Keel also finds the case a session already started with the same data, and refuses a session more cases than it allows.

| Keel's answer | The model reads |
|---|---|
| `201` or `200` | `Case <number> started (<case type>).` |
| `403`, `409` or `422` | Keel's message and code, as an error to act on. |
| No answer, `408`, `429`, or a `5xx` other than `503 harness-off` | Keel could not be reached: call `start_case` again. |
| Anything else | Keel refused the request, with its status. |

A start Keel would refuse whole is never sent: one over 256,000 bytes of JSON, one holding NaN or an infinity, a tool call id too long for the 200-character key, or one from a session with no channel. An intake session is no step: Keel is never told how its turns end.

## Step sessions wait behind people

Every wake of a step session, and of each session a step starts, is queued at priority 1. Interactive work is queued at 0 and phone calls at -1, and a lower score is taken first. A case's work therefore waits while people are waiting on the agent.
