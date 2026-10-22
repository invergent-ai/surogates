# keel-link

`surogates keel-link` is Keel's private door into the runtime. Keel, the case engine, calls it to start an agent's session for one step of a case, and then to answer it, ask it for a revision, cancel it and read its status. It is a process of its own, on a port of its own, and it serves nothing else.

## What it serves

| Route | What it does |
|---|---|
| `POST /v1/keel/step-sessions?agent_id=<agent>` | Starts the agent's session for one step, or finds the one Keel's key already started. |
| `POST /v1/keel/step-sessions/{session_id}/messages` | Gives the step a person's answer to its raised hand, or a reviewer's request to revise its draft, and wakes the session. Each key is delivered once: `202 {"delivered": false}` when it was. |
| `POST /v1/keel/step-sessions/{session_id}/cancel` | Pauses the session, interrupts its turn and removes the step's inputs from its config. `200 {"status": "paused"}`, also when it was paused already. |
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
| 422 | `inputs-blocked` | The prompt-injection screen blocked the step's inputs. |
| 422 | `invalid-request` | The body does not validate. |
| 500 | `internal-error` | The database failed. Only the error's class name is logged. |
| 503 | `unavailable` | The agent's runtime config could not be read, twice. |

## Keep it internal

Run keel-link where only Keel can reach it: an internal service, never a public ingress or load balancer. Whoever holds the link key can start sessions on any agent of any org.

keel-link reads the same state as the API (database, Redis, storage, the platform API), so it takes the same settings, plus its own:

| Setting | Held by | What it is |
|---|---|---|
| `SUROGATES_KEEL_LINK_KEY` | keel-link alone | The bearer Keel shows keel-link. |
| `SUROGATES_KEEL_URL` | the worker | The base URL of Keel's door, where the worker reports how a step ended. |
| `SUROGATES_KEEL_DOOR_KEY` | the worker alone | The bearer the worker shows Keel's door. |

keel-link listens on `SUROGATES_API_HOST`, port `SUROGATES_KEEL_LINK_PORT` (8090 by default). Give each process only its own key. Neither key is logged or shown in a settings repr. With the settings empty the integration is off: keel-link answers every call `401 bad-link-key`.

## Step sessions wait behind people

Every wake of a step session, and of each session a step starts, is queued at priority 1. Interactive work is queued at 0 and phone calls at -1, and a lower score is taken first. A case's work therefore waits while people are waiting on the agent.
