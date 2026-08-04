# 3. Architecture

## System Components

Surogates follows the three-component model: decouple the brain from the hands, and both from the session log.

```
+-----------------------------------------------------------------+
|                     Channel Adapters                             |
|  Web SPA   |   Slack   |   Telegram   |   API (service account) |
+---------------+-------+---------+---------+------------+--------+
                |
+---------------v-------------------------------------------------+
|                      API Gateway                                 |
|         (FastAPI, JWT auth, tenant routing)                      |
+---------------------+-------------------------------------------+
                      |
             +--------v--------+
             |  Orchestrator   |  wake(session_id)
             |  (Redis queue)  |  retry on failure
             +--------+--------+
                      |
        +-------------+-------------+
        |             |             |
   +----v----+   +----v----+  +----v----+
   | Harness |   | Harness |  | Harness |  stateless workers
   |  (brain)|   |  (brain)|  |  (brain)|  any can serve any session
   +----+----+   +----+----+  +----+----+
        |             |             |
   +----v-------------v-------------v----+
   |            Tool Router               |
   |  harness-local | sandbox | MCP proxy |
   +---+------------+-------------+------+
       |            |             |
  +----v---+  +-----v-----+  +---v----+
  | Memory |  |  Sandbox   |  |  MCP   |
  | Skills |  | (K8s pod / |  | Proxy  |
  | Search |  |  process)  |  | (vault)|
  +--------+  +-----------+  +--------+
       |            |             |
   +---v------------v-------------v------+
   |          Session Store               |
   |   (PostgreSQL append-only event log) |
   +--------------------------------------+
```

### API Server (Control Plane)

The API server is the trusted control plane. It serves HTTP to the frontend, manages sessions, and exposes REST APIs for skills, memory, and workspace files.

| Aspect | Detail |
|---|---|
| Framework | FastAPI |
| Auth | JWT (HS256), short-lived access tokens, refresh tokens |
| Storage access | Tenant and workspace S3 (all `tenant-*` and `agent-*` buckets) |
| Database access | Full (sessions, events, tenants, credentials) |
| Serves | Web chat SPA static files, REST API, SSE event streams |

The API server is the **only component the frontend talks to**. It is also the only component with tenant-wide storage access.

### Worker (Brain)

Workers are long-running pods that pick sessions from the Redis work queue and run the `AgentHarness` -- the core LLM loop.

| Aspect | Detail |
|---|---|
| Concurrency | Up to 50 sessions per worker (semaphore-bounded) |
| State | Stateless -- all state is in PostgreSQL/Redis |
| Storage access | None (tenant operations go through API server via HTTP) |
| Database access | Sessions, events, leases (read + write) |
| Sandbox management | Creates/destroys sandbox pods via K8s API |

Workers never access tenant Garage buckets directly. Harness tools that need tenant data (skills, memory) call the API server via `HarnessAPIClient`.

### Sandbox (Hands)

Sandboxes are ephemeral execution environments for untrusted tool commands. One sandbox per session, lazily provisioned on first use, destroyed when the session ends.

| Aspect | Detail |
|---|---|
| Dev mode | `ProcessSandbox` -- subprocess in temp directory |
| Production | `K8sSandbox` -- dedicated K8s pod per session |
| Storage access | Session-scoped S3 path only (`{agent_bucket}/sessions/{session_id}/`) |
| Network | Restricted by NetworkPolicy -- only MCP proxy reachable |
| Lifetime | `activeDeadlineSeconds: 3600` (K8s kills orphans) |

The sandbox runs the full `surogates` Python package. A persistent `tool-executor` daemon (`surogates.sandbox.executor_server`) loads `ToolRegistry` once at pod startup, then serves tool calls over HTTP on the pod IP; each call forks a child process that runs the real Python handler and returns the JSON result. The worker authenticates with a per-sandbox bearer token, and a mount-gated readiness probe ensures pod-Ready means "registry warm + workspace mounted".

## Data Flow: Message In -> LLM Loop -> Response Out

### Web Channel

```
1. Browser SPA: POST /v1/sessions/{id}/messages (with JWT)
2. API Server: validate JWT -> resolve tenant -> emit user.message event -> enqueue to Redis
3. Browser SPA: GET /v1/sessions/{id}/events?after=N (SSE) -> subscribes for real-time events
4. Worker: dequeue -> wake(session_id) -> acquire lease -> replay events -> LLM loop
5. Worker: LLM responds -> tool calls dispatched -> events emitted to PostgreSQL
6. Delivery: materialize SSE-visible events into delivery_outbox; Redis nudges live subscribers
7. Browser: SSE relay delivers events; missed events replayable from PostgreSQL
```

### Messaging Channel (Slack)

```
1. Platform event arrives at channel-adapter pod
2. Adapter: normalize message -> resolve tenant via channel_identities -> POST internal API
3. Same flow as web (steps 2-7 above)
4. Response delivery: adapter claims pending delivery_outbox rows for its channel
5. Adapter formats payload -> sends via platform API -> marks row delivered
```

### API Channel (Programmatic)

```
1. Pipeline: POST /v1/api/prompts with a service-account token (surg_sk_...)
2. API Server: resolve service account -> create session (channel="api",
   user_id=NULL) -> emit user.message event -> enqueue to Redis -> 202
3. Worker: dequeue -> wake(session_id) -> harness loop -> events emitted
4. Pipeline: reads results back from the `events` table keyed by session_id
   (no streaming, no SSE). `sessions.status` indicates completion.
```

API-channel sessions never appear in the delivery outbox -- pipelines pull
directly from PostgreSQL. See [Channels / API](../channels/api.md) for the
request/response schema and idempotency semantics.

### Crash Recovery

```
1. Worker crashes mid-session
2. Lease expires after TTL (60 seconds)
3. Orchestrator detects failure -> emits harness.crash event -> re-enqueues session
4. New worker picks up -> wake(session_id) -> replay events from cursor
5. Session continues from where it left off
```

## Saga: Automatic Rollback for Multi-Step Operations

When the agent performs a sequence of state-changing tool calls (writing files, running commands, calling external APIs), a failure partway through can leave things inconsistent. The saga system solves this by tracking each step and automatically rolling back in reverse order if something goes wrong.

```
Forward execution:
  Step 1: write_file("config.yaml")   --> committed (checkpoint saved)
  Step 2: write_file("main.py")       --> committed (checkpoint saved)
  Step 3: terminal("python test.py")  --> FAILED

Automatic compensation (reverse order):
  Step 2: restore checkpoint           --> main.py reverted
  Step 1: restore checkpoint           --> config.yaml reverted
```

Two compensation strategies are used depending on the tool type:
- **Builtin tools** (file writes, patches, commands): filesystem checkpoints are restored automatically.
- **MCP tools** (external services): a declared undo tool is called (e.g., `delete_ticket` to undo `create_ticket`).

Saga is opt-in (`saga.enabled: true` in config). When active, tool calls are forced sequential to ensure deterministic ordering for rollback. Read-only tools (search, list, view) are excluded from tracking. Saga state is reconstructed from the event log on crash recovery.

See [Governance and Security](../governance-and-security/index.md#saga-multi-step-tool-chains-with-automatic-rollback) for configuration and details.

## Sub-Agents: Child Sessions as First-Class Agents

A sub-agent is a **child session spawned by a coordinator** that runs through the same harness loop with a scoped preset (system prompt, tool filter, model override, iteration cap, governance policy profile). Every sub-agent is a real `Session` row with its own event log, lease, and cursor — crash recovery and observability work identically to root sessions.

```
Coordinator session                         Child session (sub-agent)
   |                                             |
   |  spawn_worker(goal, agent_type=             |
   |               "code-reviewer")              |
   |  creates Session(parent_id=coord.id,        |
   |                  config.agent_type=...)     |
   |  enqueues child_id to Redis                 |
   |  emits worker.spawned event                 |
   |-------------------------------------------->|
   |                                             |  any worker dequeues
   |                                             |  wake() resolves agent_type
   |                                             |  applies preset to session.config
   |                                             |  runs full LLM loop
   |                                             |
   |  worker.complete event (into parent log)    |
   |<--------------------------------------------|
   |  re-enqueue parent so it wakes              |
```

Children share everything about the tenant — skills, MCP servers, experts, memory — with the parent. The sub-agent's preset only scopes the per-session identity: which tools are visible to the child's LLM, which model responds, how many iterations it may run, and which governance profile narrows its allowed tool surface.

| Dimension | Scoping |
|---|---|
| Tenant (org_id, user_id) | Inherited from parent |
| Skill catalog | Shared (tenant-wide) |
| MCP server pool | Shared (tenant-wide) |
| Memory (MEMORY.md / USER.md) | Shared (tenant-wide) |
| System prompt identity | Scoped per sub-agent |
| Tool allowlist / denylist | Scoped per sub-agent |
| Model | Scoped per sub-agent |
| Iteration budget | Scoped per sub-agent (clamped at 30) |
| Governance policy profile | Scoped per sub-agent (narrows tenant base) |
| Event log | Scoped per child (own Session row) |
| Session storage path | Scoped per child (own `sessions/{child_id}/`) |

### Resolution

The shared helper `resolve_agent_by_name(name, tenant)` runs in two places:

1. **Spawn time** — `spawn_worker` / `delegate_task` handlers use it to validate `agent_type=<name>` and reject unknown or disabled types with a clear JSON error (no child created).
2. **Wake time** — the harness re-resolves when a child's session picks up, so an admin can update an AGENT.md and the new version applies on the next wake without re-spawning.

Both paths go through the same function to prevent drift.

### Session Ancestry

A session's `parent_id` column threads the whole coordinator → worker → sub-worker chain. The `v_session_tree` recursive SQL view exposes the full descendant graph with root, depth, and ancestor path, and powers the `GET /v1/sessions/{id}/tree` + `/children` endpoints that drive the "Running" panel in the web UI.

### Sub-Agent Governance

A sub-agent carries **the same policy as its parent**. The child inherits the
parent's `agent_id`, and the gate is built from that agent's runtime-config
`governance` blob — so the allow/deny lists and the network egress rules are
identical on both. A sub-agent is never more privileged than its parent, and
never less.

To restrict what a child can do, use the agent definition's `tools` /
`disallowed_tools`. Those are enforced by never handing the tool's schema to
the model, which is stronger than a runtime denial: there is nothing to
refuse, override, or talk around.

`with_profile` composition (allow-lists intersect, deny-lists union, strictest
egress default wins) is still how the *agent-level* policy is layered onto the
platform floor. It is not applied per sub-agent.

### Why This Design?

**Why not shared PVC?** PVCs cannot be dynamically mounted to running pods. A shared PVC gives every sandbox access to every tenant's data. Application-level path enforcement is not a security boundary.

**Why not one bucket per tenant?** The sandbox runs untrusted LLM-generated code. If it has credentials for the full tenant bucket, a prompt injection can access other sessions' data.

**Why not database for skills/memory?** Skills have binary supporting files. Platform skills live in Hub repos and are versioned via tags (`v1`, `v2`, ...); the system bundle is published by `surogate-ops seed-builtin-skills`, the per-agent bundle is populated by the ops bundle publisher. Workspace files are large and binary. The file-shaped layout keeps assets human-readable and versionable.

See [Storage](../storage/index.md) for detailed bucket layout and lifecycle.
