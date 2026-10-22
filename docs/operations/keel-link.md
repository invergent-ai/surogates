# keel-link

`surogates keel-link` is Keel's private door into the runtime. Keel, the case engine, calls it to start an agent's session for one step of a case, and then to answer it, ask it for a revision, cancel it and read its status. It is a process of its own, on a port of its own, and it serves nothing else.

## What it serves

| Route | What it does |
|---|---|
| `POST /v1/keel/step-sessions?agent_id=<agent>` | Starts the agent's session for one step, or finds the one Keel's key already started. |
| `POST /v1/keel/step-sessions/{session_id}/messages` | Gives the step a person's answer to its raised hand, or a reviewer's request to revise its draft, and wakes the session. Each key is delivered once: `202 {"delivered": false}` when it was. |
| `POST /v1/keel/step-sessions/{session_id}/cancel` | Ends the step: removes its inputs from the session's config, pauses the session and interrupts its turn. `200 {"status": "paused"}`, also when it was paused already; each call interrupts the turn again. |
| `GET /v1/keel/step-sessions/{session_id}` | The session's `status` and `updated_at`. |
| `GET /health`, `GET /health/ready` | The same liveness and readiness probes as the API. |

It has no JWT authentication, no web UI and no OpenAPI schema. Each route checks the link key instead. The public API never serves `/v1/keel/`.

A step session is created with the id Keel gives it, on the `api` channel, owned by the org's `case-type:<case type>` service account, which nobody signs in as. Every session of the case carries the case's `case:<case id>` memory and workspace boundary. The step's inputs pass the prompt-injection screen before anything is written, and the session starts with one message that sets out the step: its inputs, as data, its outcomes and its checks.

Errors answer `{"detail": {"code", "message"}}`, and a message never repeats the case's data:

| Status | Code | When |
|---|---|---|
| 401 | `bad-link-key` | The bearer is not the link key, or no link key is set. |
| 404 | `unknown-agent` | No such agent, or it belongs to another org. |
| 404 | `unknown-session` | No such session, or it is not a step's own session. |
| 409 | `agent-stopped` | The agent is stopped. |
| 409 | `key-in-use` | The key, or the session id, names another step's session. |
| 409 | `step-finished` | A message for a step that is over: its inputs were redacted. |
| 422 | `inputs-blocked` | The prompt-injection screen blocked the step's inputs. |
| 422 | `invalid-request` | The body does not validate. |
| 500 | `internal-error` | The database failed. Only the error's class name is logged. |
| 503 | `unavailable` | The agent's runtime config could not be read, twice. |

## A step's inputs

Once a step is over, cancelled among others, its inputs are removed from the session's config and the contract is marked `inputs_redacted`. A step marked so takes no more messages. The redaction covers the config only: the step's first message, which sets out the inputs, stays in the session's event log, which no generic route serves for a case's sessions.

## Keep it internal

Run keel-link where only Keel can reach it: an internal service, never a public ingress or load balancer. Whoever holds the link key can start sessions on any agent of any org.

keel-link reads the same state as the API (database, Redis, storage, the platform API), so it takes the same settings, plus its own:

| Setting | Held by | What it is |
|---|---|---|
| `SUROGATES_KEEL_LINK_KEY` | keel-link alone | The bearer Keel shows keel-link. |
| `SUROGATES_KEEL_URL` | the worker | The base URL of Keel's door, where the worker reports how a step ended, and where an intake agent starts cases. |
| `SUROGATES_KEEL_DOOR_KEY` | the worker alone | The bearer the worker shows Keel's door. |

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

A case type can name agents as its intake agents: front doors, such as a WhatsApp agent, that talk with people and start their cases. At each wake of a session that works on no case, the worker asks Keel's door which case types the session's agent may start, and keeps the answer a minute per org and agent. While the list is not empty, the session is offered `start_case`: it takes only the listed case types, and its description names each one with the fields it requires. No session of a case is offered it. A worker without both settings, or one that cannot reach Keel, offers no `start_case` until it next asks.

Each call asks Keel to start one case, keyed `intake:<session id>:<tool call id>`, so the same call sent again finds the case it started. Keel also finds the case a session already started with the same data, and refuses a session more cases than it allows.

| Keel's answer | The model reads |
|---|---|
| `201` or `200` | `Case <number> started (<case type>).` |
| `403`, `409` or `422` | Keel's message and code, as an error to act on. |
| No answer, `408`, `429`, or a `5xx` other than `503 harness-off` | Keel could not be reached: call `start_case` again. |
| Anything else | Keel refused the request, with its status. |

A start Keel would refuse whole is never sent: one over 256,000 bytes of JSON, one holding NaN or an infinity, or a tool call id too long for the 200-character key. An intake session is no step: Keel is never told how its turns end.

## Step sessions wait behind people

Every wake of a step session, and of each session a step starts, is queued at priority 1. Interactive work is queued at 0 and phone calls at -1, and a lower score is taken first. A case's work therefore waits while people are waiting on the agent.
