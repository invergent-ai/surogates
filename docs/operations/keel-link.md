# keel-link

`surogates keel-link` is Keel's private door into the runtime. Keel, the case engine, calls it to start an agent's session for one step of a case, and then to answer it, ask it for a revision, send it a plain message, give it a comment that mentions its agent, cancel it, and read its status, its events and its progress. It starts an agent's item session too, on a work item a case gives the agent as it gives a person one, and then sends it a plain message, cancels it, and reads its status, its events and its progress. It starts a person's case agent session, gives it the person's messages, stops its turn, and reads its status and its events. It also groups an agent's raised hands by reason, and drafts a passage of policy from agreeing answers, with the agent's own model. It also lists, reads and writes a case's files, and keeps the version of every file it hands Keel; runs reruns and trials, an agent's step again; opens a case's document in the document editor for a person, invites staff into an organisation, and gives Keel the mail addresses of the org's users it names. It is a process of its own, on a port of its own, and it serves nothing else.

## What it serves

| Route | What it does |
|---|---|
| `POST /v1/keel/step-sessions?agent_id=<agent>` | Starts the agent's session for one step, or finds the one Keel's key already started: `201` made, `200` found, each `{"session_id", "service_account_id", "bundle_version", "created"}`. `bundle_version` is the agent's bundle version as its runtime config names it at this call, null when it has none or names none Keel would take. |
| `POST /v1/keel/step-sessions/{session_id}/messages` | Gives the step a person's answer to its raised hand, a reviewer's request to revise its draft, or a plain message from whoever works on the case, and wakes the session. Each key is delivered once: `202 {"delivered": false}` when it was. A plain message also answers `resting`, and wakes no step that rests on its report. See [A message to a step](#a-message-to-a-step). |
| `POST /v1/keel/step-sessions/{session_id}/mentions` | Gives a draft step's agent a comment in one of the case's documents that mentions it, and wakes the session, a draft under review too: `202 {}`. Each key is written once. See [A comment that mentions the step's agent](#a-comment-that-mentions-the-steps-agent). |
| `POST /v1/keel/step-sessions/{session_id}/cancel` | Ends the step: removes its inputs from the session's config, pauses the session and interrupts its turn. `200 {"status": "paused"}`, also when it was paused already; each call interrupts the turn again. |
| `GET /v1/keel/step-sessions/{session_id}` | The session's `status` and `updated_at`. |
| `GET /v1/keel/step-sessions/{session_id}/events?after=<id>&limit=<n>` | A page of the session's events after the cursor `after`, without `llm.delta`: `200 {"events": [{"id", "type", "data", "created_at"}], "has_more"}`. See [Watching a step session](#watching-a-step-session). |
| `GET /v1/keel/step-sessions/{session_id}/events/stream?after=<id>` | The session's events after the cursor `after`, then each new one as it is written, as server-sent events. See [Watching a step session](#watching-a-step-session). |
| `GET /v1/keel/step-sessions/{session_id}/progress` | The session's status, its todo list as its agent last wrote it, and its last activity: `200 {"status", "todos", "todos_at", "last_activity"}`. See [Progress](#progress). |
| `POST /v1/keel/item-sessions?agent_id=<agent>` | Starts the agent's session on one work item, or finds the one Keel's key already started: `201 {"session_id", "bundle_version"}` made, `200 {"session_id", "bundle_version"}` found, `bundle_version` as for a step. See [An agent's work item](#an-agents-work-item). |
| `GET /v1/keel/item-sessions/{session_id}` | The item session's `session_id` and `status`. |
| `POST /v1/keel/item-sessions/{session_id}/cancel` | Ends the item as a step's cancel ends a step: `200 {"status": "paused"}`, also when it was paused already. |
| `POST /v1/keel/item-sessions/{session_id}/messages` | Gives the item a plain message from whoever works on the case: `202 {"status": "queued"}` when it wakes the session, `202 {"status": "resting"}` when the item rests on its report or is over and keeps it unread. Each key is written once. See [A message to an item](#a-message-to-an-item). |
| `GET /v1/keel/item-sessions/{session_id}/events?after=<id>&limit=<n>` | A page of the item session's events, as a step session's page. See [Watching a step session](#watching-a-step-session). |
| `GET /v1/keel/item-sessions/{session_id}/events/stream?after=<id>` | The item session's events after the cursor `after`, then live, as a step session's stream. See [Watching a step session](#watching-a-step-session). |
| `GET /v1/keel/item-sessions/{session_id}/progress` | The item session's status, todo list and last activity, as a step session's progress. See [Progress](#progress). |
| `POST /v1/keel/case-agent-sessions?agent_id=<agent>` | Starts one person's case agent on a case, or finds the one Keel's key already started: `201 {"session_id"}` made, `200 {"session_id"}` found. See [A person's case agent](#a-persons-case-agent). |
| `POST /v1/keel/case-agent-sessions/{session_id}/messages` | Gives the case agent its person's message and wakes it: `202 {"status": "queued"}`. Each key is written once. See [A person's message](#a-persons-message). |
| `POST /v1/keel/case-agent-sessions/{session_id}/cancel` | Interrupts the case agent's turn and pauses its session: `200 {"status": "paused"}`, also when it was paused already. The person's next message wakes it again. |
| `GET /v1/keel/case-agent-sessions/{session_id}` | The case agent session's `session_id` and `status`. |
| `GET /v1/keel/case-agent-sessions/{session_id}/events?after=<id>&limit=<n>` | A page of the case agent session's events, as a step session's page. See [Watching a step session](#watching-a-step-session). |
| `GET /v1/keel/case-agent-sessions/{session_id}/events/stream?after=<id>` | The case agent session's events after the cursor `after`, then live, as a step session's stream. See [Watching a step session](#watching-a-step-session). |
| `POST /v1/keel/hand-reasons?agent_id=<agent>` | Takes `{"org_id", "hands": [{"id", "question", "choices", "answer"}], "vocabulary": [str]}` (1 to 25 hands) and answers `200 {"labels": [{"id", "reason", "answer_gist", "policy_silent"}], "usage": {"input_tokens", "output_tokens", "cost_usd"}}`: a reason in policy terms for each hand the model labelled usably, and the gist of its answer. A hand with no label was not labelled this time. See [Raised hands, grouped](#raised-hands-grouped). |
| `POST /v1/keel/policy-text?agent_id=<agent>` | Takes `{"org_id", "reason", "gist", "hands": [{"question", "answer"}]}` (1 to 50 hands) and answers `200 {"text", "usage"}`: one passage of written policy in Markdown, 1 to 6,000 characters, that states the rule the answers applied, and what the call used (as for hand labelling). See [A policy passage](#a-policy-passage). |
| `GET /v1/keel/cases/{case_id}/files` | The case's files, by path: `{"files": [{"path", "name", "size", "modified_at", "editing"}]}`. `editing` names who is in the file's editor now, by display name, sorted; someone the runtime has no name for is `Someone`, or `An agent` for an agent. A case no session has written to yet has `{"files": []}`. The platform's own folders are left out. |
| `GET /v1/keel/cases/{case_id}/files/content?path=<path>` | The file's bytes, as `application/octet-stream`, with their SHA-256 hex digest in `X-Content-SHA256`. A file over `SUROGATES_KEEL_MAX_FILE_BYTES` is not read. |
| `PUT /v1/keel/cases/{case_id}/files/content?path=<path>` | Writes the body at the path, over any file there. `200 {"name", "uri", "sha256", "size"}`, where `uri` is `case-file://<case id>/<path>`: the value a step's file field holds. |
| `GET /v1/keel/cases/{case_id}/versions/{sha256}` | A kept version of a file of the case, by its hash, as `application/octet-stream`, with the hash in `X-Content-SHA256`. `404 unknown-version` when the case holds none; a version over `SUROGATES_KEEL_MAX_FILE_BYTES` is not read. See [A case's file versions](#a-cases-file-versions). |
| `POST /v1/keel/cases/{case_id}/versions/missing` | Takes `{"sha256": [hash]}` (1 to 500 hashes) and answers `200 {"missing": [hash]}`: each hash the case holds no version of, once, in the order asked. |
| `DELETE /v1/keel/cases/{case_id}/versions` | Deletes every version the case holds, and no file of its workspace: `200 {"deleted": <count>}`, `0` for a case that has none. |
| `POST /v1/keel/cases/{case_id}/editor` | Takes `{"org_id", "path", "user_id", "access", "theme", "mentionable"}` and answers `200 {"document_server_url", "config", "mentionable_users"}`: one person's editor config for the document at the path, and the agents it offers to mention. See [A case's document in the editor](#a-cases-document-in-the-editor). |
| `POST /v1/keel/reruns?agent_id=<agent>` | Starts a rerun or a trial of the agent's step, in a case of its own id, on the files of a past case as they were: `201` made, `200` found by its key, each `{"session_id", "service_account_id", "bundle_version", "created"}`. See [Reruns and trials](#reruns-and-trials). |
| `PUT /v1/keel/reruns/{session_id}/files/content?org_id=<org>&path=<path>` | Writes the body at the path in a trial's workspace, before or after the trial is created: `200 {"name", "uri", "sha256", "size"}`, as a case's file write answers. |
| `POST /v1/keel/reruns/{session_id}/start` | Writes a rerun's first message and queues it: `202 {"started": bool}`, true when this call wrote it. `409 step-finished` once the rerun was cancelled. |
| `GET /v1/keel/reruns/{session_id}` | A rerun's `{"status", "updated_at"}`, as a step session's status. |
| `GET /v1/keel/reruns/{session_id}/events?after=<id>&limit=<n>` | A page of the rerun's events, as a step session's page. See [Watching a step session](#watching-a-step-session). |
| `GET /v1/keel/reruns/{session_id}/events/stream?after=<id>` | The rerun's events after the cursor, then live, as a step session's stream. |
| `GET /v1/keel/reruns/{session_id}/progress` | The rerun's status, todo list and last activity, as a step session's progress. See [Progress](#progress). |
| `POST /v1/keel/reruns/{session_id}/cancel` | Ends the rerun as a step's cancel ends a step: `200 {"status": "paused"}`, also when it was paused already. |
| `DELETE /v1/keel/reruns/{session_id}` | Deletes a rerun or a trial whole: `200 {"sessions", "objects"}`, the sessions deleted and the boundary objects deleted, zeros for an id that names nothing. See [Purging a rerun](#purging-a-rerun). |
| `POST /v1/keel/orgs/{org_id}/members` | Takes `{"email", "display_name"}` and answers `200 {"user_id", "created", "joined", "sign_in", "set_password_token", "expires_at"}`: the org's user with that email, found, joined from another org or created. See [Inviting staff](#inviting-staff). |
| `POST /v1/keel/orgs/{org_id}/emails` | Takes `{"user_ids": [id]}` (1 to 100 user ids) and answers `200 {"emails": {"<user id>": "<address>"}}`: the address of each named user of the org who has one that mail can reach. Every other id is left out. See [Staff addresses](#staff-addresses). |
| `GET /health`, `GET /health/ready` | The same liveness and readiness probes as the API. |

It has no JWT authentication, no web UI and no OpenAPI schema. Each route checks the link key instead. The public API never serves `/v1/keel/`.

A step session is created with the id Keel gives it, on the `api` channel, owned by the org's `case-type:<case type>` service account, which nobody signs in as. Every session of the case carries the case's `case:<case id>` memory and workspace boundary. The step's inputs pass the prompt-injection screen before anything is written, and the session starts with one message that sets out the step: its inputs, as data, its outcomes and its checks.

Errors answer `{"detail": {"code", "message"}}`, and a message never repeats the case's data, an email or a token:

| Status | Code | When |
|---|---|---|
| 401 | `bad-link-key` | The bearer is not the link key, or no link key is set. |
| 404 | `unknown-agent` | No such agent, or it belongs to another org. |
| 404 | `unknown-session` | No such session, or it is not the own session of a step (never a rerun's), for a step route, of a work item, for an item route, of a person's case agent, for a case agent route, or of a rerun, for a rerun route. |
| 404 | `unknown-file` | No file at the path in the case's workspace, or the editor was asked for a case recorded under another org. |
| 404 | `unknown-version` | The case holds no version with the hash. |
| 404 | `unknown-user` | The editor was asked for a user who does not belong to the org. |
| 404 | `unknown-org` | An invitation into, or a request for the addresses of, an org that does not exist. |
| 409 | `agent-stopped` | The agent is stopped. Never for the analysis routes, which serve a stopped agent too. |
| 409 | `key-in-use` | The key, or the session id, names another session: another step's, item's, case agent's or rerun's, or one of another agent. |
| 409 | `not-a-member` | A case agent was asked for a person who is not a user of the org. |
| 409 | `rerun-running` | A purge of a rerun whose session is in a turn, or one of whose documents is open in the editor. Cancel it, then purge it later. |
| 409 | `step-finished` | A message or a mention for a step that is over: its inputs were redacted. |
| 409 | `no-office` | A mention for a step whose agent does not edit the case's documents: not a draft step's, or the runtime has no agent editing. |
| 409 | `case-exists` | A rerun's or a trial's id names a case, or another org's rerun or trial, or one of the other kind; or a step, an item or a case agent is started on a case id that is a rerun's or a trial's. |
| 409 | `file-open` | A write over a file that is open in the document editor. |
| 409 | `file-version-gone` | A rerun's file has no version in the case it comes from. The answer's `missing` lists the indexes of those files, and nothing was written. |
| 409 | `document-closing` | The document's last save is still landing; ask for the editor again in a moment. |
| 409 | `ambiguous-email` | An invitation for an email that two or more users of other orgs have: none is chosen. |
| 409 | `other-firebase-project` | An invitation for an account that signs in with a Firebase project the org does not sign in with. |
| 413 | `too-large` | A file to write or read over `SUROGATES_KEEL_MAX_FILE_BYTES`. |
| 415 | `not-a-document` | The editor was asked for a file that is not an office document, or a mention names a file that is not a document an agent edits. |
| 422 | `inputs-blocked` | The prompt-injection screen blocked the step's inputs, or the item's inputs, question, choices or evidence; or a policy passage's reason or gist, or every one of its hands. |
| 422 | `invalid-path` | A file's path Keel may not name, a rerun's `files` paths among them: see [A case's files](#a-cases-files). |
| 422 | `invalid-request` | The body does not validate (an invitation's email that is not one, an address request's `user_ids` that is not 1 to 100 UUIDs, among them), a case id or an org id is not a UUID, a version's hash is not 64 lowercase hex digits, or a session's events were asked for with a cursor or a page size out of range. |
| 500 | `internal-error` | The database failed, other than while a step, item, case agent or rerun session's events, status or progress were read, while a purge first read the rerun, or while an org's addresses were read. Only the error's class name is logged. |
| 502 | `model-failed` | An analysis route's model sent no completion: the provider failed, refused, or ran out of credit. A policy passage's model gave no valid object, or a text that is empty or over 6,000 characters. |
| 503 | `office-off` | The editor was asked for, and no document server is configured. |
| 503 | `unavailable` | The agent's runtime config could not be read, twice; an analysis route's agent names no model, or the vault holds no key for it, or the runtime has no vault; the object store refused or did not answer a case's files; an invitation could not read the org's Firebase config; or the database failed while a step, item, case agent or rerun session's events, status or progress were read, while a purge first read the rerun, or while an org's addresses were read. Only the error's class name, and the case id, the org id, the session id or, for an analysis route, the agent id, are logged. |
| 504 | `model-timeout` | An analysis route's model work took longer than 22 seconds. |

## A step's inputs

Once a step is over, cancelled among others, its inputs are removed from the session's config and the contract is marked `inputs_redacted`. A step marked so takes no more messages. The redaction covers the config only: the step's first message, which sets out the inputs, stays in the session's event log, which only keel-link serves, to Keel.

A draft step's agent edits the case's documents beside the person. keel-link creates a draft step's session with `file_pane` set, and the session keeps `office_read` and `office_edit` whatever the step's `tools` name, when the agent has them: registered where the runtime has agent editing, and allowed by its policy. It edits within the document server's capacity: when runtime-office holds as many documents open as it can, the tool answers `The office service has as many documents open as it can hold; try again in a minute.` No other session of a case gets the office tools: not a complete step's, a helper's, a work item's or a case agent's.

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

A message wakes a step whose session is at rest, as an answer wakes it, except a step that rests on its report. A step rests on its report when a report ended it for this round and the step is not over: it waits for a person, on a raised hand or a draft under review. Any other report that ended it but left its inputs in place rests it the same way. Such a step is not woken, and keeps its report's mark. Its message stays unread until Keel's next answer or revision wakes the step, and that turn reads the message first. `resting` is true then, and false otherwise; a repeated call answers it as the step stands at that call.

A message never gives the step back. An answer or a revision ends the rest a report began, and the step then reports to Keel again. A message does neither, and a message that reached the step since a report began does not keep the report's mark from being written. If the turn that made the report reads the message, a further report in that turn is answered by the worker and never reaches Keel.

A step that is over takes no message: `409 step-finished`, as for an answer. Nor does a message it took before then, and no turn has read, wake it once it is over. The route logs neither a message's text nor its sender.

## A comment that mentions the step's agent

`POST /v1/keel/step-sessions/{session_id}/mentions` gives a draft step's agent a comment in one of the case's documents that mentions it, as a chat's editor gives its agent one. Its body is `{"path", "thread", "comment", "author_id", "author_name", "idempotency_key"}`:

- `path` is the document's path in the case's files, 1 to 4096 characters;
- `thread` is the comment's thread, as the editor names it, matching `^[A-Za-z0-9{}_-]{1,64}$`;
- `comment` is the comment or the reply that mentions the agent, as written, 1 to 20000 characters;
- `author_id` is the runtime user who wrote it, a UUID;
- `author_name` is their display name, 1 to 200 printable characters, or null;
- `idempotency_key` is 1 to 200 characters.

A key the route does not know is refused, as is a body out of this shape: `422 invalid-request`. The path is then checked as the file routes check it, `422 invalid-path`, and a file that is not a document an agent edits, `.docx`, `.xlsx`, `.pptx` or `.pdf`, is `415 not-a-document`.

Only a draft step's agent can be mentioned, and only where the runtime has agent editing: a document server with runtime-office beside it. That agent edits the case's documents beside the person (see [A step's inputs](#a-steps-inputs)). Any other step, a complete step's among them, is `409 no-office`, and so is a draft step when the runtime has no agent editing.

The session gets the message a chat's mention becomes. In the session's events it is a `user.message` whose `data` holds:

- `content`, `[Comment on <file name>] <author>: «<comment>»`, where the agent's address in the comment reads `@` and the agent's display name, and the author is `Someone` for a null `author_name`;
- `source: "office_mention"`;
- `metadata`, `{"path", "thread", "kind", "author", "author_id", "comment"}`: the path as the case's files name it, `kind` the document's extension (`docx`, `xlsx`, `pptx` or `pdf`), `author_id` `user:<author_id>`, and the comment as sent;
- `synthetic: "keel_mention"` and `keel_key`, the idempotency key.

The agent reads it as it reads Keel's messages: a turn under way at its next request to the model, otherwise the session's next turn. Before the model reads it, the worker posts `On it, I'll reply here when done.` in the comment's thread, under the agent's name, so the person sees the agent took it. The agent then does the work with `office_read` and `office_edit`, answers in the thread with only the result, through `office_edit`'s `reply`, and resolves the thread.

A mention wakes the step whose session is at rest, a draft under review among them, and keeps its report's mark. A plain message waits for Keel's next word, but the person who wrote the comment waits in the document for the agent's answer in the thread. The mention does not give the step back: the draft stays the reviewer's, and a report the turn makes is answered by the worker and never reaches Keel.

The answer is `202 {}`. Each key is written once: a repeated key writes and wakes nothing, and answers `202 {}` as the first call did, also once the step is over. A new key to a step that is over is `409 step-finished`. The route logs neither the comment, its author nor the path.

## An agent's work item

A case gives an agent a work item as it gives a person one: a step to do, a vote to cast, or a raised hand to answer. `POST /v1/keel/item-sessions?agent_id=<agent>` starts the agent's session on it. Its body is `{"org_id", "case_type", "idempotency_key", "session_id", "contract"}`: `case_type` is 1 to 200 characters and must be the contract's, and `idempotency_key` is 1 to 200 characters. The answer is `201 {"session_id", "bundle_version"}` when the call made the session, and `200 {"session_id", "bundle_version"}` when Keel's key already holds it; `bundle_version` is read as a step's is. The key holds this item's session when that session has Keel's id, the agent, and the contract's `case_id` and `item_id`. Any other session under the key, or another session with Keel's id, is `409 key-in-use`.

### The item's contract

| Key | What it is |
|---|---|
| `case_id`, `case_number`, `case_type`, `type_version` | The case. |
| `item_id` | Keel's id for the item. |
| `kind` | `step`, `vote` or `raised_hand`. |
| `token`, `node`, `step` | Where in the case the item is; for a raised hand, the step that raised it. |
| `inputs` | The case's data the item reads. |
| `form`, `outcomes`, `files` | A step's or a vote's, and required for both: the form as JSON Schema, empty for a vote; the outcomes it ends with, each with the outputs it requires; and its file fields, each taking one file or a list. |
| `question`, `choices`, `evidence` | A raised hand's: its question, required, 1 to 2000 characters; at most 20 choices, of at most 200 characters each; and the facts behind it. |
| `tools`, `mcp_servers`, `knowledge` | As a step's: null keeps the agent's own work tools or MCP servers, and a list narrows them. `knowledge` is the full grant. |

Keys the runtime does not know are ignored. A contract out of this shape, such as a vote without its form or a raised hand without its question, is `422 invalid-request`, and nothing is made.

### Its owner and its boundaries

The item's session is made as a step's is: with the id Keel gives it, on the `api` channel, owned by the org's `case-type:<case type>` service account, inside the case's `case:<case id>` memory and workspace boundaries, and narrowed to the contract's MCP servers when it names them. Its wakes wait behind people, as a step's do. The contract is in the session's config under `case_item`, never `case_step`, and only keel-link sets it: every other way of creating a session drops it. A helper the session starts keeps the case's boundaries and scope, but not the contract: a helper is no item.

### Its kickoff

The item's inputs, and a raised hand's question, choices and evidence, pass the prompt-injection screen before anything is written: `422 inputs-blocked`. The session then starts with one message, marked `keel_item` and written once per key. Its first line names the case and the item:

- a step: `Case <number>, a <case type> case, gives you a work item: its step "<step>". Work on it now.`
- a vote: `Case <number>, a <case type> case, asks for your vote on "<step>". Work on it now.`
- a raised hand: `Case <number>, a <case type> case, asks you to answer a raised hand at its step "<step>". Work on it now.`

Then come the item's inputs, as data, in a JSON code fence. A raised hand adds its question, its choices and its evidence, the same way. A step or a vote adds its outcomes, with the outputs each requires, and its file fields. The last line says how the item ends: with `complete_item`, which finishes a step, casts a vote, or answers a raised hand with one of its choices or in words; or with `return_item` and a reason, which gives the item back to whoever gave it to the agent.

`POST /v1/keel/item-sessions/{session_id}/cancel` ends the item as a step's cancel ends a step: its inputs leave the session's config and are marked `inputs_redacted`, the session pauses, and every call interrupts its turn. `GET /v1/keel/item-sessions/{session_id}` answers `200 {"session_id", "status"}`.

### A message to an item

`POST /v1/keel/item-sessions/{session_id}/messages` gives the agent working an item a plain word from whoever works on the case, a person or another agent. Its body is `{"text", "sender", "idempotency_key"}`:

- `text` is 1 to 20000 characters;
- `sender` is the display name of whoever sends it, 1 to 200 printable characters, or null;
- `idempotency_key` is 1 to 200 characters.

Any other key, `kind` and `note` among them, is `422 invalid-request`: an item takes plain messages alone.

The agent reads `<sender> sends you a message about this work item: <text>`, or `Someone working on the case sends you a message about this work item: <text>` without a sender. Then, after a blank line: `Take it into account, and go on with the item.` In the session's events, the message is a `user.message` whose `data` holds `content`, the text the agent reads, `synthetic: "keel_message"` and `keel_key`, the idempotency key, as a step's plain message does.

The answer is `202 {"status"}`:

- `queued`: the message wakes the item. A session at rest resumes, with a `session.resume` before the message, and a turn under way reads it at its next request to the model. Either way the session is queued, behind people.
- `resting`: the item rests on its report, once `complete_item` or `return_item` reported it, or it is over, its inputs redacted, as after a cancel. The message is kept unread: nothing resumes, the session's status does not change, and nothing is queued.

Each key is written once. A repeated key writes nothing and wakes nothing, and answers as the item stands at that call. A message never gives the item back: a report's mark stays. Unlike a step, an item that is over does not refuse the message; it keeps it, unread. The route logs neither a message's text nor its sender.

## A person's case agent

A person's case agent is the agent that helps one person on one case: it answers their questions about the case and drafts into their form. `POST /v1/keel/case-agent-sessions?agent_id=<agent>` starts its session. Its body is `{"org_id", "case_id", "case_number", "person_id", "idempotency_key", "session_id"}`: `case_number` and `idempotency_key` are 1 to 200 characters, and keys the runtime does not know are ignored. The answer is `201 {"session_id"}` when the call made the session, and `200 {"session_id"}` when Keel's key already holds it. The key holds this case agent's session when that session has Keel's id, the agent, the case and the person. Any other session under the key, a step's or an item's among them, or another session with Keel's id, is `409 key-in-use`. The agent is checked as for a step: `404 unknown-agent`, `409 agent-stopped`. Nothing is written to the session or queued: the person's first message wakes it.

### Its owner and the person

`person_id` must be a user of the org `org_id`, a home user or a member: anyone else is `409 not-a-member`, and nothing is made. The session is not the person's. It is made with the id Keel gives it, on the `api` channel, with no user, owned by the org's `keel-case-agent` service account: one per org, of the same kind as the case types' accounts, which nobody signs in as and no admin route lists. Keel acts as the person when the agent reads the case. The session's config holds `case_agent`, `{"case_id", "case_number", "person_id"}`, and only keel-link sets it: every other way of creating a session drops it, and a helper never copies it.

### Its boundary and its workspace

The session carries the case's `case:<case id>` memory boundary, and nothing more of the case's. The boundary is memory-only, as an evaluation's is: the agent works in a scratch workspace of its own, the per-session one, never in the case's files, and reads those only through Keel. A session whose `case_agent` names another case than its boundary is no case agent: the case agent routes answer it `404 unknown-session`.

Its memory keeps no facts about people. The `memory` tool refuses the `user` target with `This session keeps no facts about people: save to the 'memory' target.`, and writes nothing; the `memory` target works as in any session of the case.

### Its priority and its tools

A person waits on a case agent, so its wakes queue at its channel's priority, 0 on `api`, beside people's work and ahead of every step and item session.

It is offered the agent's own tools that a case allows, less skills (`skills_list`, `skill_view`, `skill_step`), the terminal (`terminal`, `process`), file writing (`write_file`, `patch`), office (`office_read`, `office_edit`) and helpers (`delegate_task`, `spawn_worker`, `send_worker_message`, `stop_worker`). Its prompt names no skill. It gets the case tools in their place: `case_read`, `case_history`, `case_files`, `case_file_read` and `case_draft_form`, which read the case as the person, and draft into their form, through Keel's door (see [How it reads the case](#how-it-reads-the-case)). It gets no step or item tool. Unlike the step and item tools, an agent's policy can take the case tools away.

### Case sessions' guidance

Every case session, a step's, an item's or a case agent's, and every helper it starts, runs without the brainstorming gate: its prompt never sends the agent to design before it works, and its skill index leaves the brainstorming skill out. Each one's prompt carries the `case_session` guidance instead: the case's people never see the agent's skills, tools or instructions, so nothing they read, a reply, a question, a returned item or a comment in a document, names a skill, a tool, a procedure or those instructions. The case agent's prompt also carries the `case_agent` guidance: it cites each fact it uses with the `cite` label of the read that gave it, drafts into the person's form only when they ask, and never submits a form, decides a step or moves the case.

### How it reads the case

The case agent reads its case only through Keel's door, never the case's workspace. The worker sends each case tool to the door at `SUROGATES_KEEL_URL`, with `SUROGATES_KEEL_DOOR_KEY` as the bearer, under the session's id. Keel answers as the person, and logs each read as a view through the case agent:

| Tool | Door route | The model reads |
|---|---|---|
| `case_read` | `GET /api/harness/sessions/{session_id}/case` | Keel's answer: the case, its data field by field, and the steps open on it. |
| `case_history` | `GET /api/harness/sessions/{session_id}/case/history` | Keel's answer: the case's history, entry by entry. |
| `case_files` | `GET /api/harness/sessions/{session_id}/case/files` | Keel's answer: the case's files. |
| `case_file_read` | `GET /api/harness/sessions/{session_id}/case/file?path=<path>` | `{"path", "cite", "text", "truncated"}`: the file as text. |
| `case_draft_form` | `POST /api/harness/sessions/{session_id}/case/form-draft` | Keel's answer, `{"drafted": [field, …]}`, as it came. |

Each item Keel gives carries a `cite` label, which the agent cites. `case_draft_form` posts `{"token", "values", "sources"}`: the open step's token, the values by form field, and where they come from, a non-empty list of objects. A draft without all three is never sent, nor is one over 256,000 bytes of JSON or one holding NaN or an infinity. Keel drafts only into a step the person holds: any other token is `409 not-yours`.

`case_file_read` gives the model the file's text, never its bytes. Keel sends a file of at most 8 MiB: a larger one, `413 too-large`, reads `The file is larger than the 8 MiB Keel sends.` The worker converts a file by its suffix:

- A document (PDF; Word, Excel and PowerPoint, legacy and macro-enabled ones included; OpenDocument; RTF) is converted to text as `read_file` converts one.
- Any other file is read as UTF-8 text when it holds no NUL byte.
- Anything else is refused, by the file's name: `<name> is neither a document nor text: case_file_read cannot read it.` A document that cannot be converted reads `<name> could not be read as a document.`

Text longer than 200,000 characters is cut there, and `truncated` is true. Text whose result, as JSON, would run past 300,000 characters, as one dense with quotes or line breaks may, is cut shorter, so the result always comes whole, with `truncated` true. Keel's other answers:

| Keel's answer | The model reads |
|---|---|
| No answer, `408`, `429`, or a `5xx` other than `503 harness-off` | `Keel could not be reached; call <tool> again.` |
| Anything else | Keel's message, or `Keel refused the call (<status>).`, as an error with Keel's code, and Keel's `problems` when it gave them. |

No other session, a step's, an item's or a chat's, is offered the case tools, and one called from any of them reaches no Keel. A worker without both settings has no link to Keel: each case tool answers `This runtime has no link to Keel.`

### A person's message

`POST /v1/keel/case-agent-sessions/{session_id}/messages` gives the case agent its person's message. Its body is `{"text", "idempotency_key"}`: `text` is 1 to 20000 characters and `idempotency_key` 1 to 200. Any other key is `422 invalid-request`.

In the session's events, the message is a `user.message` whose `data` holds `content`, the person's text as they wrote it, `synthetic: "keel_person_message"` and `keel_key`, the idempotency key. The agent reads it as it reads a typed message: a turn under way at its next request to the model, otherwise the session's next turn. The marker keeps the text from ever running as a harness command, as an unmarked message starting with `/` would. The worker still counts it as a person's message: it wakes a session whose wakes kept crashing, as a typed message does.

The answer is `202 {"status": "queued"}`. A session at rest resumes, with a `session.resume` before the message, and the session is queued. Each key is written once: a repeated key writes nothing, wakes nothing and queues nothing, and answers the same.

`POST /v1/keel/case-agent-sessions/{session_id}/cancel` interrupts the agent's turn and pauses its session: `200 {"status": "paused"}`, also when it was paused already, and every call interrupts the turn again. Nothing leaves the session's config, and the person's next message wakes it. `GET /v1/keel/case-agent-sessions/{session_id}` answers `200 {"session_id", "status"}`. Its events are read as a step session's, by page and live; it has no progress route. The routes log neither a message's text nor the person.

## The designer agent's tools

The designer agent helps a builder change a project's definitions in a draft. The builder opens it on one draft, and Keel delegates that draft to the builder's chat with it. Its `keel_designer` tools work on the draft through Keel's door: the worker sends each to `SUROGATES_KEEL_URL`, with `SUROGATES_KEEL_DOOR_KEY` as the bearer, under the session's id. Keel finds the draft by the session's live delegation and acts on it as the builder. None publishes: publishing stays the builder's.

| Tool | Door route | The model reads |
|---|---|---|
| `draft_files` | `GET /api/harness/sessions/{session_id}/draft/files` | Keel's answer: `{"draft_id", "files": [{"path", "kind", "key"}]}`. |
| `draft_read` | `GET /api/harness/sessions/{session_id}/draft/file?path=<path>` | Keel's answer: `{"path", "content"}`, the file's JSON. |
| `draft_main` | `GET /api/harness/sessions/{session_id}/draft/main`, with `?path=<path>` only when the model gives one | Keel's answer, from the project's published definitions: without a path, `{"files": [path, …]}`; with one, `{"path", "content"}`. |
| `draft_save_file` | `PUT /api/harness/sessions/{session_id}/draft/file?path=<path>`, the file's JSON object as the body | Keel's answer: `{"head"}`, the draft's new head. A file Keel finds invalid is `422 invalid-file`, with its `problems`. |
| `draft_save_scenario` | `PUT /api/harness/sessions/{session_id}/draft/scenario?path=<path>`, the scenario's JSON object as the body | Keel's answer: `{"head"}`. |
| `draft_dry_run` | `POST /api/harness/sessions/{session_id}/draft/dry-run` | Keel's answer: the dry run's, with `ops_checked: false`. |

A read or a save without its path is never sent, nor is a save whose content is not a JSON object, or one holding NaN or an infinity. A save over 1 MiB of JSON is never sent either: it reads `The file is over the 1 MiB Keel takes: split it or shorten it.` A file whose answer, as JSON, would run past 300,000 characters comes back as `{"path", "content", "truncated": true}`, with `content` the file's JSON text, cut, so the result always comes whole. Keel's other answers:

| Keel's answer | The model reads |
|---|---|
| `404 no-delegation`: the session holds no live delegation | `No draft is open to this chat: ask the builder to open the designer on a draft.` |
| No answer, `408`, `429`, or a `5xx` other than `503 harness-off` | `Keel could not be reached; call <tool> again.` |
| Anything else | Keel's message, or `Keel refused the call (<status>).`, as an error with Keel's code, and Keel's `problems` when it gave them. |

An agent gets the designer's tools only through an allow-list naming them: its policy's `allowed_tools`. An agent with no policy, or a policy with only a deny-list, is offered none, and a session's own config never adds them. No session of a case is offered them, whatever its agent's policy, and one called from a case's session reaches no Keel. A worker without both settings has no link to Keel: each designer tool answers `This runtime has no link to Keel.`

## Raised hands, grouped

`POST /v1/keel/hand-reasons?agent_id=<agent>` has the agent's own model name what each of up to 25 raised hands turns on. Keel sends the hands an agent raised (the question, its choices, and the answer people gave once they did) with the reasons its project already uses, and stores what comes back. Nothing is stored here: no table, no session, no key.

- **Which model, and who pays.** The agent's summary slot, else its main slot as the runtime config names it; there is no session, so no tier swap. The call goes to the slot's `base_url`, with the slot's key read from the vault at org scope. For a platform agent that is the management plane's proxy, which bills the agent's project. A stopped agent is served too: grouping hands it raised before it stopped is not the agent working.
- **What it takes.** `org_id` (the agent must be the org's, else `404 unknown-agent`), 1 to 25 hands with distinct `id`s of 1 to 200 characters, a `question` of 1 to 2,000 characters, at most 20 `choices` of at most 200, an `answer` of 1 to 20,000 characters or null, and at most 200 `vocabulary` entries of 1 to 120 characters. Anything else is `422 invalid-request`.
- **Numbering.** The model sees each hand as `n` (1, 2, …) and answers by `n`; Keel's ids never reach it.
- **What a label is.** For each hand the model gives a `reason` and, when the hand has an answer, an `answer_gist`, each read on one line (white space runs become one space, one trailing full stop goes) and 1 to 120 characters, and whether the policy should have settled the hand (`policy_silent`). The first label for a number is the only one kept; a number that names no hand sent is dropped. A reason that equals a vocabulary entry without regard to case takes the entry's spelling. A hand with an answer needs a usable gist, or its label is dropped; a hand without one answers `answer_gist: null`. The labels follow the order of the hands.
- **The screen.** Each hand (question, each choice and answer) and each vocabulary entry passes the prompt-injection screen, each text as written and on its own, before the prompt is built, because hand text is case content and can carry instructions. A blocked hand or entry is left out of the call: a blocked hand gets no label, and the batch goes on. When every hand is blocked the answer is `200` with no labels and zero usage, and the model is not asked. The log says how many hands were blocked, never which.
- **Identifiers stay out.** A reason or a gist that holds an identifier of any hand of the request is dropped: a word of the hand's question, choices or answer (white space ends it), once punctuation and currency signs are stripped from its ends, or a run of letters, digits and `@ . , - / +` in it, once those signs and `_` are stripped from its ends, that is 4 characters or more and holds a digit or an `@` (a reference number, an e-mail address, an amount); or any run of 4 or more digits, compared without regard to case. Quotes, markdown signs, apostrophes and colons end a run, so `**AB-12-34**` and `jane.doe@example.com's` name what they hold, and a word such as `#482` or `INV#123` stands as written. A reason that is one of the vocabulary's is not checked; a gist always is. Names cannot be told apart from other words: only the prompt, which asks for general terms, keeps them out.
- **What the model reads of an answer.** Each answer is cut to its first 2,000 characters.
- **A hand with no entry was not labelled this time.** The model gave nothing usable for it. Keel asks again later.
- **Usage.** `usage` adds up the tokens of every completion the call received, the cache reads included; `cost_usd` is a decimal string with six places, estimated against the model that served the call, and null when no price is known for it. A call that reached no model answers zeros and `"0.000000"`.
- **Bounds and failures.** The model work, both attempts together, takes at most 22 seconds: past that the answer is `504 model-timeout`, before Keel's own wait ends. No completion at all is `502 model-failed`; completions that held no valid object are a `200` with no labels and their usage.
- **Logs.** A line names the agent, a count or an error's class, never a question, an answer, a reason or what the model said. The `openai` client library logs the body of each request at DEBUG: keep its logger at INFO or above.

## A policy passage

`POST /v1/keel/policy-text?agent_id=<agent>` has the agent's own model write one passage of written policy from the answers people gave a reason's raised hands, so the knowledge base can hold the rule they applied. Keel sends the reason, the gist most answers agree on and up to 50 questions with their answers; the passage is a draft a builder reviews.

- **The same model and bound as hand labelling:** the agent's summary slot, else its main slot, keyed at org scope, billed where the slot's endpoint meters it, a stopped agent served too, and at most 22 seconds for both attempts (`504 model-timeout` past that). `org_id` must be the agent's. `reason` and `gist` are 1 to 120 characters, each `question` 1 to 2,000 and each `answer` 1 to 20,000.
- **The screen.** A blocked `reason` or `gist`, or every hand blocked, is `422 inputs-blocked`. One blocked hand is left out of the call. Each answer reaches the model cut to its first 2,000 characters.
- **The passage.** It states the rule in general terms (when it applies, any exception the answers show), never quotes a question or an answer, and states only what the answers share when they differ. Every identifier of a hand of the request that the passage still holds (see [Raised hands, grouped](#raised-hands-grouped)) is replaced by `[omitted]`, without regard to case, and the passage's length is checked after that: 1 to 6,000 characters.
- **When there is none.** A model that gave no valid object, or a text that is empty or over 6,000 characters, is `502 model-failed`: a passage is all or nothing.
- **Logs and storage** are as for hand labelling: nothing is stored, and a line never holds a question, an answer or the passage.

## Watching a step session

Keel reads a step session's events, by page and live, and its progress, to show what the agent does on the step. The same reads serve an item session, at `GET /v1/keel/item-sessions/{session_id}/events`, `…/events/stream` and `…/progress`, and a case agent session, at `GET /v1/keel/case-agent-sessions/{session_id}/events` and `…/events/stream`: each answers as the step session's route below does, and is refused the same way. The runtime keeps each session's events as an append-only log, and an event never changes once written:

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

`todos_at` and `at` are in UTC, `+00:00`, as an event's `created_at` is. Only the step's or the item's own session is read: a helper it started keeps its own list, which progress does not give. Stored data out of its shape never fails the read: a todo item out of its shape is left out, a stored list that is not one reads as `todos` null, and a newest event out of its shape as `last_activity` null. Each is logged by the session id and the kind of error alone. The route logs neither the list nor an event's data.

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

Every digest keel-link or a step session hands Keel (a write's answer, a read's `X-Content-SHA256` header, an output's file value) names bytes the runtime keeps. They are written under the case's `versions/<sha256>`, beside the workspace in the case's boundary, before the digest leaves; once per case, however many paths hold the bytes. A version is never mounted in a sandbox and never listed as a case file. A write or a read whose version cannot be kept is `503 unavailable`, a write leaves no file, and a read sends no digest. An output file whose version cannot be kept fails the `complete_step` or `complete_item` call with an error the model reads, and nothing is sent to Keel; the model calls again.

## A case's file versions

A version is a file as it was when its digest was handed to Keel, kept as [A case's files](#a-cases-files) describes. A version can be read by its hash, Keel asks which of the hashes it holds are gone, and, when the case's retention ends, deletes all of the case's versions at once.

A hash is 64 lowercase hex digits; anything else is `422 invalid-request`. Reading one streams it as a file's read does, so a version over `SUROGATES_KEEL_MAX_FILE_BYTES` is `413 too-large`. The question about missing versions lists the case's versions once, however many hashes it names, and names each missing hash once, in the order Keel asked. A case nothing was written to has no versions, and the question names every hash. Deleting removes only the case's `versions/` folder: no file of the workspace, and nothing of another case. The object store refusing or not answering is `503 unavailable` on all three routes.

Retention is the case engine's. The runtime deletes no version of its own accord, and a version stays until Keel asks.

## A case's document in the editor

`POST /v1/keel/cases/{case_id}/editor` gives Keel the editor config of one person for one document of the case's files, to open in the document server at `document_server_url`. The body is `{"org_id", "path", "user_id", "access", "theme", "mentionable"}`: `access` is `edit`, `comment` or `view`, `theme` is `light` (the default) or `dark`, and `mentionable` is optional. The path is checked as the file routes check it.

The person is the runtime user `user_id`, who must belong to the org `org_id`. The editor shows them under their display name, with the editor user id `user:<user_id>`: the same as when they open a file in an agent's chat.

The runtime records the org a case belongs to the first time it is told: at the creation of a step, item or case agent session of the case, or at the editor, whichever comes first. The record sits beside the case's workspace, in `boundaries/case:<case_id>/org.json` in the session bucket, where no sandbox mounts it and no file route lists or reads it. A create never refuses on it: when the record cannot be written, the failure is logged by the case id and the error's class name, and the session starts all the same. The editor reads the record before anything else of the case: a case recorded under another org than `org_id` answers `404 unknown-file`, as a file that is not there, and a record the object store will not give is `503 unavailable`. A record that cannot be read as an org is written over.

| `access` | The editor |
|---|---|
| `edit` | Opens in edit mode, with editing, reviewing and commenting on. |
| `comment` | Opens in edit mode, with editing and reviewing off and commenting on. |
| `view` | Opens read-only, without comments. |

The document server converts older formats (`.doc`, `.odt`, `.rtf`, `.xls`, `.ods`, `.ppt`, `.odp`) to show them, so these open read-only, without comments, whatever the access.

Everyone who opens the file shares its one editing session, whatever their access, and so does a step session of the case that has the file open. The document belongs to the case: it is read from and saved into the case's files, and stays open while anyone is in it, with or without a chat. While it is open, the listing's `editing` names who is in it, and a write over it is refused. Like the file routes, it logs no path.

`mentionable` names the agents the person may mention in a comment, at most 20, each `{"id", "name"}`: `id` is the agent id, 1 to 200 characters, and `name` Keel's name for it, 1 to 200 printable characters. Keel names a draft step's agent, which answers in the comment's thread once Keel forwards the mention (see [A comment that mentions the step's agent](#a-comment-that-mentions-the-steps-agent)). The answer's `mentionable_users` lists them for the editor to offer, each `{"id": "agent:<id>", "name", "email"}`, where `email` is the runtime's address for the agent, made of its name: `Credit analyst` is `credit-analyst@agent.invalid`. No mail can reach an `.invalid` address. They are offered only where an agent answers a comment: the runtime has agent editing, the editor opens the document to edit or comment, and it is a Word document, a spreadsheet, a slide deck or a PDF. Anywhere else, and without `mentionable`, `mentionable_users` is `[]`.

## Inviting staff

`POST /v1/keel/orgs/{org_id}/members` takes `{"email", "display_name"}` and gives Keel the org's user with that email. The email is compared without case.

- **Found.** A user of the org, home or member, has the email: `created` and `joined` are false.
- **Joined.** Exactly one user of other orgs has it: they become a member of the org, and `joined` is true. Two or more is `409 ambiguous-email`, and nothing is written.
- **Created.** Nobody has it: the account is created in the org, with that display name and the email trimmed and lowercased, and `created` is true. In an org with Firebase sign-in it is a Firebase account of the org's project without a Firebase identity yet: its first sign-in through the org's Firebase exchange links it by verified email. Otherwise it is a password account without a password.

An invitation never changes an existing account: not its password, its way of signing in, nor its display name. Joining adds only the membership.

`sign_in` is `firebase` for an account of the org's Firebase project, and `password` otherwise. An account of another Firebase project has no way into the org, and is refused with `409 other-firebase-project`, as is a Firebase account invited into an org without Firebase. When the org's Firebase config cannot be read, the invitation is `503 unavailable`: a password account is never created in an org that signs in with Firebase.

For a password account without a password whose home is the org, `set_password_token` is a new token and `expires_at` when it lapses, 7 days on; otherwise both are null. An account whose home is another org gets no token: its password is its home org's to set, since one password signs in to every org the account belongs to. Keel gives the token to the person, who sets their password through the runtime's public `POST /v1/auth/orgs/{org_id}/set-password`; see [Invited staff](../multi-tenancy/index.md#invited-staff). The token works once, and not after the password changed. The answer carries `Cache-Control: no-store`, and the token is never logged or stored. Neither is the email.

A person joined from another org who has no password yet gets no link from the org that joined them. Their home org invites them again: they are found there, and that invitation gives a fresh link. A re-invitation never revokes a link: an earlier one stays valid until a password is set or its 7 days pass.

## Staff addresses

Keel keeps no address of its staff. It asks for the addresses when it sends mail: `POST /v1/keel/orgs/{org_id}/emails` takes `{"user_ids": [id, ...]}`, 1 to 100 ids a call, and answers `{"emails": {"<user id>": "<address>"}}`.

An id is answered when its user belongs to the org, as a home user or as a member, and has an address that mail can reach. The address is returned as stored, with its case. Each id is answered once, under its canonical form (lowercase and hyphenated), however it was sent; the bound of 100 counts the ids as sent.

These are left out of the answer, and are never an error: a user of another org, an id that names nobody, and an address mail cannot reach. The runtime stores stand-in addresses for users that have no mailbox: a channel user's (`<platform>-<id>@<org>.channels.surogate.local`) and a followed person's (`person-<id>@<org>.people.surogate.local`). It also uses `.invalid` addresses for agents. So an address under `surogate.local`, an address under `.invalid` and a value that is not `name@domain` are left out. An id that is not a UUID is `422 invalid-request`.

The answer carries `Cache-Control: no-store`. No address is logged: a database failure is `503 unavailable`, which Keel retries, and only the error's class name and the org id are logged.

## Reruns and trials

A rerun runs an agent's step again on the files a past step read, as they were. A trial runs a draft's step on inputs a builder typed. Either is a step session of a case that exists nowhere else: its id, which Keel gives, is the session's own and its case's, and its boundary `case:<session id>` holds the files it works on. Memory starts empty, in the agent's own storage under that boundary, and the agent runs as it is configured now: nothing pins the bundle it ran before. The session reports to Keel's door as any step session does, `POST /api/harness/sessions/{session id}/complete`, `raise-hand` and `ended`, with the same bodies; nothing in a report says it is a rerun, and Keel tells by the session id. A raised hand rests with the session's inputs until Keel cancels or purges it. The files a rerun's output names are kept as versions of its own case, `case-file://<session id>/<path>`, and stay readable through the version routes until the purge.

The session is fenced. No session of a rerun, a helper included, is offered `terminal`, `process` or a browser tool, whatever the contract's `tools` name; its MCP servers are off unless the create asks for them (`mcp`), in which case the contract's `mcp_servers` say which. A draft rerun keeps the office tools, which edit its own documents. A rerun's wakes, and its helpers', are queued at 2, behind every case's work, and a trial's at 0. The step routes answer `404 unknown-session` for a rerun's session.

`POST /v1/keel/reruns?agent_id=<agent>` takes `{"org_id", "case_type", "idempotency_key", "session_id", "kind", "contract", "files", "of_case_id", "mcp", "start"}`: `kind` is `rerun` or `trial`; `contract` is a step's contract, whose `case_id` is `session_id`; `files` are up to 200 `{"path", "sha256"}`, with distinct paths, and need `of_case_id`, the case whose versions they come from and never `session_id`; `mcp` and `start` default to false and true. A body out of this shape is `422 invalid-request`. It checks, in this order:

1. the agent: `404 unknown-agent`, `409 agent-stopped`;
2. each path as the case file routes check it, `422 invalid-path`;
3. the contract's inputs, which the prompt-injection screen reads, `422 inputs-blocked`;
4. that a session with the id does not exist: `409 key-in-use`;
5. the id's boundary folder: a case's, or another org's or kind's rerun's, is `409 case-exists`;
6. the files: each must have a version in `of_case_id`, a case recorded under the same org that is no rerun's, or the answer is `409 file-version-gone` with `missing`, the indexes into `files` that have none, and nothing is written.

Only then does it mark the boundary folder as the org's rerun of that kind, in the record beside the workspace, copy the files, a piece at a time, and create the session. A version that is gone by the time it is copied is `409 file-version-gone` too; the files already copied stay under the mark until Keel purges the rerun. Keel waits long for this call, and sends the same body again with the same key on a timeout: a repeat that finds the session answers `200` and copies nothing, and one after a create that died before its session copies again over the same paths. The session is queued when it is made, so its first report can reach Keel's door before Keel has the answer. A key held by another session, or by a rerun of the other kind, is `409 key-in-use`.

A trial's files may come first: `PUT /v1/keel/reruns/{session_id}/files/content` writes one, marking the folder as the org's trial's with the first, and keeps its version as a case's file write does. A folder that is a case's, another org's, or a rerun's is `409 case-exists`. A trial created with `start` false writes no first message, and queues nothing, until `POST /v1/keel/reruns/{session_id}/start`, which writes it once.

## Purging a rerun

`DELETE /v1/keel/reruns/{session_id}` deletes a rerun or a trial, which Keel keeps for a while and then lets go of. It answers `200 {"sessions", "objects"}`: how many sessions it deleted, and how many objects of the boundary folder. It deletes, in this order:

1. the rerun's memory objects, under the agent's storage prefix the session was created with;
2. every row of the rerun's session tree, in one transaction: the sessions, their events, the inbox items and outbox rows on them, their leases and cursors, the board notes they wrote, and the task rows they stand in. A session's title and a task's goal and result hold case text, so the rows go, not only the events;
3. the closed editor rows of the rerun's documents;
4. the folder's `workspace/`, then its `versions/`, then its record.

The record goes last. The mark in it is what tells a purge the folder is a rerun's, so a purge that failed part-way, when the store stopped answering after the workspace was gone, leaves it: `503 unavailable`, and a retry finds the mark and finishes. A purge of what is already gone answers zeros, so a retry of a finished purge does too.

A purge refuses what is not a rerun's. An id that names a session that is not a rerun's own session (a step's, or a helper's), or a folder recorded as a case's, is `404 unknown-session` and nothing is deleted. A rerun whose session, or a helper's, is in a turn is `409 rerun-running`, and so is one with a document open in the editor: a save landing after the purge would write under the folder again. Keel cancels the rerun first and purges it on a later pass. Each purge logs the rerun's id and the counts.

## Keep it internal

Run keel-link where only Keel can reach it: an internal service, never a public ingress or load balancer. Whoever holds the link key can start sessions on any agent of any org, start a case agent session for any person of any org, send any step's or item's agent, or any case agent, a message, read every step, item or case agent session's events and progress (its inputs and all its tools read among them), read and write any case's files, open them in the editor as any user, read the mail address of any user of any org, by id, and invite anyone into any org, with the token that sets a new account's password.

keel-link reads the same state as the API (database, Redis, storage, the platform API), so it takes the same settings, plus its own:

| Setting | Held by | What it is |
|---|---|---|
| `SUROGATES_KEEL_LINK_KEY` | keel-link alone | The bearer Keel shows keel-link. |
| `SUROGATES_KEEL_URL` | the worker | The base URL of Keel's door, where the worker reports how a step ended, where an intake agent starts cases, where a case agent reads its case, and where the designer agent works on its draft. |
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

A raised hand carries its `question`, its `choices` and its `evidence`: at most 50 facts, each `{"label", "value", "source"?: {"file", "page"?}}`. `label` is 1 to 200 characters and `value` 1 to 500, counted once stripped; `page` is a whole number from 1; a null `source` or `page` is read as absent. `file` must be one the case's files show: the model names it in `/workspace`, and the report carries it as the case's files list it, relative to the case's workspace. A path outside the workspace, in the platform's own folders, or with a hidden file or folder in it, is refused. Whether the file exists is not checked.

A file field of `complete_step` takes the same files. Each path names a file in `/workspace` that the case's files show, and the report carries the file's `{"name", "uri", "sha256"}`, with `uri` the `case-file://` value the case's files give it. A path in the platform's own folders, or with a hidden file or folder in it, is refused by the field's name, and its index in a list, never by its path. So is a path a step's output would read as another file. A path with no file behind it is refused too.

A report Keel would refuse whole is never sent, and the model is told what to fix: one over 256,000 bytes of JSON, one holding NaN or an infinity, sources that are not lists of objects, a raised hand's evidence outside its shape, or a tool call id over 200 characters. A refused fact is named by its index, never by its path. Once a report has ended the step, a later report in the same wake is answered by the worker, without Keel.

What each report carries. Besides what the model gave, `complete_step` and `raise_hand` carry:

- `pages_read`: the knowledge pages the case's sessions read, each once for each compile that wrote the text read, sorted, at most 500. Each entry is `{"kb_id", "path", "sha256", "compile_id"}`. `compile_id` is the knowledge base compile that last wrote the text of the page read: a later compile that leaves the page unchanged does not change it. It is null when the page was published before compiles kept versions, or the read was recorded before reads named their compile. A passage read is recorded as its page. The same page can be listed once under each compile id its reads named, and once under null.
- `usage`: `{"input_tokens", "output_tokens", "cache_read_tokens", "estimated_cost_usd"}`, what the step's session and every helper under it, at any depth, used so far. The tokens and the cost are those the sessions' rows count; the cache reads are those their model calls' responses name. Counts are at most 2^63−1, and the cost is a plain decimal in USD, never with an exponent. Each report carries the total so far, so a later one is never below an earlier one; Keel replaces what it kept, and does not add them. A helper still working after the last report spends what Keel is not told.
- `bundle_version`: the agent's bundle version that the session's latest wake ran with, null when no wake recorded one (an agent with no bundle, or a session whose wakes came before wakes recorded it), and at most 200 characters.

A report is never sent with a value Keel would refuse whole: a bundle version that is not 1 to 200 characters of text is sent as null, and a count outside its range or a cost that is negative or not finite is brought within it. A `complete_step` or `raise_hand` report cannot go without its `usage`: when the database cannot say what the work used, the call fails before it reaches Keel, and the model calls again. A bundle version the database cannot say is sent as null.

`ended` is a report too. It carries `bundle_version`, and `usage` whenever the worker can read it, `failed` included. When it cannot, the worker logs it by session id and the error's class, and the end goes without `usage`: Keel keeps the last usage it saw. Keel is told how a step ended whatever the database says of its use.

A turn of the step that ends in words, with no report, is told once to report, and goes on. When it still ends without a report ending the step, the worker tells Keel `no-outcome`, with the code of how the turn ended, and the inputs stay. A turn that fails, and a session the worker gives up on because its wakes kept crashing or never recovered, is told to Keel as `failed`, with the failure's code and never its text, and the inputs are removed. A report that ended the step for this round (`completed`, `drafted`, `hand-raised`, or `stopped` when Keel refused it for good) is marked on the session until Keel's next answer or revision to the step clears it. So is a turn with no outcome that Keel takes as a raised hand. A mark is never written over an answer or a revision that reached the step after its report began. A marked step is never nudged, and no later end of its turns is told to Keel: a crash never fails a draft under review or a raised hand, nor removes the inputs they resume with. The same holds for a step whose inputs were already removed. Keel being unreachable never keeps a turn from ending: the worker logs it by session id.

A worker without both settings has no link to Keel: every report removes the step's inputs and ends the turn, and Keel learns of it only from the session's status.

## How an item session reports back

Only a work item's own session is offered `complete_item` and `return_item`; it is offered neither step tool, and no other session, a step's or a helper's, is offered either item tool. An agent's policy cannot take them away. The worker sends each to the item routes of Keel's door, under the session, with `SUROGATES_KEEL_DOOR_KEY`:

- `complete_item` acts on the item. A step or a vote ends with one of its outcomes: the worker posts `{"outcome", "output", "sources", "call_id"}` to `/api/harness/sessions/{session_id}/item/complete`, checked as `complete_step`'s report is, file fields included. A raised hand takes exactly one of `choice`, which must be one of its choices, and `answer`, in words: the worker posts `{"choice"}` or `{"answer"}` with `"note"`, null when the model gives none, `"sources"`, a list of objects, `[]` when it gives none, and `"call_id"`. Either way the report also carries `pages_read`, `usage` and `bundle_version`, below.
- `return_item` gives the item back to whoever gave it, with the reason: the worker posts `{"reason", "call_id"}` to `/api/harness/sessions/{session_id}/item/return`, with `usage` and `bundle_version`, below. `reason` is 1 to 2000 characters, counted once stripped, and sent stripped.

`call_id` is the tool call's id. Keel's answer decides what the session does:

| Keel's answer | The model reads | The session |
|---|---|---|
| `200` | `Item ended: Keel recorded it.`, or `Item ended: the item went back.` | Its turn ends. The item's inputs are removed from its config, and the item is marked `completed` or `returned`. |
| `409 not-running` | `Item ended: the item is no longer this session's.` | Its turn ends. Nothing is marked. |
| Another `409` | Keel's code and message, as an error, with the attempts left when Keel counts them. | The model fixes its report and calls again, or returns the item. Nothing is marked. |
| No answer, `408`, `429`, or a `5xx` other than `503 harness-off` | `Keel could not be reached; call <tool> again.` | The model calls again. A repeated call gets Keel's first answer. |
| Anything else | `Item stopped: Keel refused this item's report (<status>); people take the item.` | The worker posts `{"status": "failed", "detail": "keel-<status>"}` to `…/item/ended`, with the `usage` and `bundle_version` the refused report carried, removes the inputs, marks the item `stopped`, and ends the turn. People take the item. |

A report Keel would refuse whole is never sent, as a step's is not: one over 256,000 bytes of JSON, one holding NaN or an infinity, sources out of their shape, a choice the raised hand does not offer, or a tool call id over 200 characters. Once a report has ended the item, a later report in the same wake is answered by the worker, without Keel. A worker without both settings has no link to Keel: every report removes the item's inputs, marks it `stopped`, and ends the turn.

What each report carries. Besides what the model gave:

- `item/complete` carries `pages_read` as a step's report does (`{"kb_id", "path", "sha256", "compile_id"}`): the knowledge pages the item's session and its helpers read, each once for each compile, sorted, at most 500, for a step, a vote and a raised hand's answer alike. `item/return` carries none.
- `item/complete` and `item/return` carry `usage`: `{"input_tokens", "output_tokens", "cache_read_tokens", "estimated_cost_usd"}`, what the item's session and every helper under it, at any depth, used so far. It is counted as a step's is: each report carries the total so far, and Keel replaces what it kept, and does not add them. A helper still working after the last report spends what Keel is not told.
- Both also carry `bundle_version`: the agent's bundle version that the session's latest wake ran with, null when no wake recorded one (an agent with no bundle, or a session whose wakes came before wakes recorded it), and at most 200 characters.

A report is never sent with a value Keel would refuse whole: a bundle version that is not 1 to 200 characters of text is sent as null, and a count outside its range or a cost that is negative or not finite is brought within it. A `complete_item` or `return_item` report cannot go without its `usage`: when the database cannot say what the work used, the call fails before it reaches Keel, and the model calls again. A bundle version the database cannot say is sent as null.

Each `item/ended` is a report too. It carries `bundle_version`, and `usage` whenever the worker can read it, `failed` included; the end that follows a refused report carries the usage that report carried. When the worker cannot read it, it logs it by session id and the error's class, and the end goes without `usage`: Keel keeps the last usage it saw. Keel is told how an item ended whatever the database says of its use.

A turn of the item that ends in words, with no report, is told once, by a message marked `keel_step_nudge`: `This session works one work item of a case. Finish it now: call complete_item, or return_item with your reason if you cannot.` The turn goes on. When it still ends without a report ending the item, the worker posts `{"status": "no-outcome", "detail"}` (with `usage` and `bundle_version`, above) to `…/item/ended`, with the code of how the turn ended as `detail`, and Keel gives the item back. Once Keel answers `200`, the item's inputs are removed and it is marked `returned`; otherwise both stay. A turn that fails, and a session the worker gives up on because its wakes kept crashing or never recovered, is posted as `{"status": "failed", "detail"}` (with `usage` and `bundle_version`, above), with the failure's code and never its text, and the inputs are removed whatever Keel answers. A marked item, or one whose inputs were already removed, is never nudged, and no later end of its turns is told to Keel. Neither is a helper of the item. A report of a response the worker drops before saving it never reaches Keel. Keel being unreachable never keeps a turn from ending: the worker logs it by session id. As with a step, the late report of a helper the session started never wakes it.

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

Every wake of a step session or an item session, and of each session either starts, is queued at priority 1. Interactive work is queued at 0 and phone calls at -1, and a lower score is taken first. A case's work therefore waits while people are waiting on the agent. A person's case agent does not wait: a person waits on it, so its wakes are queued at 0, as interactive work is. A rerun's wakes, and its helpers', are queued at 2, behind every case's work, because nobody waits on one; a trial's are queued at 0, as interactive work is, because a builder waits on it.
