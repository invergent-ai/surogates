# keel-link

`surogates keel-link` is Keel's private door into the runtime. Keel, the case engine, calls it to start an agent's session for one step of a case, and then to answer it, ask it for a revision, cancel it and read its status. It also lists, reads and writes a case's files, and opens a case's document in the document editor for a person. It is a process of its own, on a port of its own, and it serves nothing else.

## What it serves

| Route | What it does |
|---|---|
| `POST /v1/keel/step-sessions?agent_id=<agent>` | Starts the agent's session for one step, or finds the one Keel's key already started. |
| `POST /v1/keel/step-sessions/{session_id}/messages` | Gives the step a person's answer to its raised hand, or a reviewer's request to revise its draft, and wakes the session. Each key is delivered once: `202 {"delivered": false}` when it was. |
| `POST /v1/keel/step-sessions/{session_id}/cancel` | Ends the step: removes its inputs from the session's config, pauses the session and interrupts its turn. `200 {"status": "paused"}`, also when it was paused already; each call interrupts the turn again. |
| `GET /v1/keel/step-sessions/{session_id}` | The session's `status` and `updated_at`. |
| `GET /v1/keel/cases/{case_id}/files` | The case's files, by path: `{"files": [{"path", "name", "size", "modified_at", "editing"}]}`. `editing` names who is in the file's editor now, by display name, sorted; someone the runtime has no name for is `Someone`, or `An agent` for an agent. A case no session has written to yet has `{"files": []}`. The platform's own folders are left out. |
| `GET /v1/keel/cases/{case_id}/files/content?path=<path>` | The file's bytes, as `application/octet-stream`, with their SHA-256 hex digest in `X-Content-SHA256`. A file over `SUROGATES_KEEL_MAX_FILE_BYTES` is not read. |
| `PUT /v1/keel/cases/{case_id}/files/content?path=<path>` | Writes the body at the path, over any file there. `200 {"name", "uri", "sha256", "size"}`, where `uri` is `case-file://<case id>/<path>`: the value a step's file field holds. |
| `POST /v1/keel/cases/{case_id}/editor` | Takes `{"org_id", "path", "user_id", "access", "theme"}` and answers `200 {"document_server_url", "config"}`: one person's editor config for the document at the path. See [A case's document in the editor](#a-cases-document-in-the-editor). |
| `GET /health`, `GET /health/ready` | The same liveness and readiness probes as the API. |

It has no JWT authentication, no web UI and no OpenAPI schema. Each route checks the link key instead. The public API never serves `/v1/keel/`.

A step session is created with the id Keel gives it, on the `api` channel, owned by the org's `case-type:<case type>` service account, which nobody signs in as. Every session of the case carries the case's `case:<case id>` memory and workspace boundary. The step's inputs pass the prompt-injection screen before anything is written, and the session starts with one message that sets out the step: its inputs, as data, its outcomes and its checks.

Errors answer `{"detail": {"code", "message"}}`, and a message never repeats the case's data:

| Status | Code | When |
|---|---|---|
| 401 | `bad-link-key` | The bearer is not the link key, or no link key is set. |
| 404 | `unknown-agent` | No such agent, or it belongs to another org. |
| 404 | `unknown-session` | No such session, or it is not a step's own session. |
| 404 | `unknown-file` | No file at the path in the case's workspace. |
| 404 | `unknown-user` | The editor was asked for a user who does not belong to the org. |
| 409 | `agent-stopped` | The agent is stopped. |
| 409 | `key-in-use` | The key, or the session id, names another step's session. |
| 409 | `step-finished` | A message for a step that is over: its inputs were redacted. |
| 409 | `file-open` | A write over a file that is open in the document editor. |
| 409 | `document-closing` | The document's last save is still landing; ask for the editor again in a moment. |
| 413 | `too-large` | A file to write or read over `SUROGATES_KEEL_MAX_FILE_BYTES`. |
| 415 | `not-a-document` | The editor was asked for a file that is not an office document. |
| 422 | `inputs-blocked` | The prompt-injection screen blocked the step's inputs. |
| 422 | `invalid-path` | A file's path Keel may not name: see [A case's files](#a-cases-files). |
| 422 | `invalid-request` | The body does not validate, or a case id is not a UUID. |
| 500 | `internal-error` | The database failed. Only the error's class name is logged. |
| 503 | `office-off` | The editor was asked for, and no document server is configured. |
| 503 | `unavailable` | The agent's runtime config could not be read, twice; or the object store refused or did not answer a case's files. Only the error's class name and the case id are logged. |

## A step's inputs

Once a step is over, cancelled among others, its inputs are removed from the session's config and the contract is marked `inputs_redacted`. A step marked so takes no more messages. The redaction covers the config only: the step's first message, which sets out the inputs, stays in the session's event log, which no generic route serves for a case's sessions.

## A case's files

A case's files are the workspace every step session of the case shares: the `case:<case id>` boundary's, in the session bucket (`SUROGATES_STORAGE_BUCKET`), under the installation's key prefix. keel-link reaches it with no session, so Keel can add a file before the case's first step, and the step sessions read what Keel wrote.

A path is relative to the workspace and `/`-separated; `.` and empty parts are dropped, and nothing else is changed. A path is `422 invalid-path` when:

- it is empty or absolute, or holds a `..` part, a NUL or a backslash;
- its first folder is one of the platform's own: `_artifacts`, `_whiteboard`, `.threads` or `_history`. These hold the platform's state, not a person's files, and the listing leaves them out too;
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

## Keep it internal

Run keel-link where only Keel can reach it: an internal service, never a public ingress or load balancer. Whoever holds the link key can start sessions on any agent of any org, read and write any case's files, and open them in the editor as any user.

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

A report Keel would refuse whole is never sent, and the model is told what to fix: one over 256,000 bytes of JSON, one holding NaN or an infinity, sources that are not lists of objects, or a tool call id over 200 characters. Once a report has ended the step, a later report in the same wake is answered by the worker, without Keel.

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
