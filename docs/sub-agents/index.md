# 9. Sub-Agents

## What is a Sub-Agent?

A sub-agent is a **declarative, reusable agent type** — a preset bundle of (system prompt, tool allowlist/denylist, model override, iteration cap, governance policy profile) that a coordinator session can apply to a freshly spawned child session by name.

Where a [skill](../skills/index.md) is a reusable *prompt* and an [expert](../experts/index.md) is a reusable *fine-tuned model*, a sub-agent is a reusable *role*. It lets an admin say "code-reviewer" once and have every spawn inherit the same persona, tools, and governance envelope.

```
Coordinator session                  Child session (sub-agent)
   |                                     |
   |  spawn_worker(goal="review auth",   |
   |               agent_type="code-     |
   |               reviewer")            |
   |------------------------------------>|
   |                                     |  harness.wake()
   |                                     |    resolves agent_type="code-reviewer"
   |                                     |    applies: system_prompt, tool filter,
   |                                     |             model, max_iterations,
   |                                     |             policy_profile
   |                                     |  runs full LLM loop
   |                                     |
   |  worker.complete (result inline)    |
   |<------------------------------------|
```

The base LLM always decides when to spawn. No transparent interception — the coordinator LLM picks a sub-agent from the catalog it's given in the system prompt, exactly as it chooses between tools.

## Sub-Agents vs. Skills vs. Experts

| | Skill | Sub-agent | Expert |
|---|---|---|---|
| **Asset file** | `SKILL.md` | `AGENT.md` | `SKILL.md` (`type: expert`) |
| **What it bundles** | Prompt | Prompt + tool filter + model + iteration cap + policy profile | Prompt + fine-tuned SLM endpoint |
| **Runs in** | Inlined into the parent session's prompt | A new child session (full harness loop) | A bounded mini-loop the base LLM delegates to |
| **Invoked by** | The user typing `/<name>` or the LLM noticing the trigger | The coordinator LLM calling `spawn_worker(agent_type=...)` / `delegate_task(agent_type=...)` | The base LLM calling `consult_expert(name=...)` |
| **Context isolation** | None (same session) | Full (separate event log, parent_id link) | Bounded mini-loop inside the parent session |
| **Model** | Inherited from the session | Configurable per sub-agent | The fine-tuned SLM endpoint |
| **Governance** | Tenant-wide | Tenant-wide + optional narrowing policy profile | Tenant-wide |

A task suits a sub-agent when it (a) benefits from a fresh context window, (b) needs its own tool envelope, or (c) should run in parallel with the parent.

## Design Principles

1. **Child sessions share the tenant.** Sub-agents inherit the parent's skills, MCP servers, experts, tenant memory, and configured agent bucket. Only the preset (prompt, tool filter, model, iteration cap, policy profile) is scoped per sub-agent.

2. **Every tool call is governed.** The same `GovernanceGate` the parent runs through also guards the child. An optional `policy_profile` narrows (intersects allowed, unions denied) on top of the base policy; profiles never widen.

3. **Spawn is explicit.** The coordinator LLM sees the catalog in its system prompt and explicitly invokes `spawn_worker(agent_type=...)`. No implicit upgrades, no transparent interception.

4. **Children survive crashes.** A sub-agent is a normal `Session` with a `parent_id` — it has its own event log, lease, and cursor, so a worker crash mid-child replays exactly like a crash mid-root.

5. **Admin and user layers stack.** Agents merge from four layers with increasing precedence: platform FS, user bucket files, org DB overlay, user DB overlay. Admin DB overrides are final.

## Lifecycle Summary

```
1. Define      AGENT.md in user bucket or admin DB overlay
2. Discover    Coordinator system prompt lists "# Available Sub-Agents"
3. Spawn       LLM calls spawn_worker(goal, agent_type="code-reviewer")
4. Resolve     Harness wake-time resolver loads the preset
5. Run         Child runs full agent loop with scoped config
6. Notify      On complete/fail, worker.complete event flows to parent
7. Observe     "Running" panel shows live children; /v1/sessions/{id}/tree for ancestry
```

## 1. Define the Sub-Agent

Create an `AGENT.md` file with YAML frontmatter. The body becomes the child session's system prompt.

```markdown
---
name: code-reviewer
description: Reviews code for correctness and security
tools: [read_file, search_files, terminal]
disallowed_tools: [write_file, patch]
model: claude-sonnet-4-6
max_iterations: 20
policy_profile: read_only
category: review
tags: [security, quality]
---

You are a senior code reviewer. Examine changes for:

- Correctness bugs and unhandled edge cases
- Security issues (injection, auth, secrets)
- Performance regressions
- Style drift from surrounding code

Return findings grouped as Critical / Important / Minor.
Include specific line numbers and suggested fixes.
```

Place it in one of the layered locations below, or create via the REST API / Web UI.

### Frontmatter Reference

| Field | Required | Default | Description |
|---|---|---|---|
| `name` | Yes | -- | Sub-agent name. Referenced by coordinators as `agent_type="<name>"` |
| `description` | Yes | -- | One-line summary shown to the coordinator LLM when it picks an agent to spawn |
| `tools` | No | `null` | Allowlist of tool names. When set, the child sees exactly these tools |
| `disallowed_tools` | No | `null` | Denylist — applied as a subtraction from the inherited toolset |
| `model` | No | inherited | LLM model override for the child session |
| `max_iterations` | No | `null` | Hard cap on child loop iterations. Clamped at the worker ceiling (30) |
| `policy_profile` | No | `null` | Named governance profile that narrows the tenant base policy for this child |
| `category` | No | `null` | Freeform grouping, used for directory organization |
| `tags` | No | `[]` | Metadata labels |
| `enabled` | No | `true` | When `false`, the agent is filtered out of the catalog and cannot be spawned |

Unknown frontmatter keys are logged as warnings so typos like `disallow_tools` (missing `ed`) surface visibly instead of silently running an unconstrained agent.

## 2. Tenant Asset Layout

Sub-agents merge from four layers, lowest → highest precedence:

| Layer | Location | Managed by | Editable via |
|---|---|---|---|
| Platform | `/etc/surogates/agents/<name>/AGENT.md` | Platform operator (container image) | Immutable at runtime |
| User bucket | `tenant-{org_id}/users/{user_id}/agents/<name>/AGENT.md` | End user | `POST /v1/agents` and the Web UI |
| Org DB overlay | `agents` table, `user_id IS NULL` | Org admin | SQL / admin tooling (no public REST endpoint) |
| User DB overlay | `agents` table, `user_id = <uid>` | Org admin | SQL / admin tooling |

DB overlays always win over filesystem layers — end users cannot override an org admin's decision by dropping a file in their bucket.

The layout inside a tenant bucket:

```
tenant-{org_id}/
  shared/
    agents/
      <name>/
        AGENT.md
  users/{user_id}/
    agents/
      <name>/
        AGENT.md
```

Categories nest one level deeper, like skills:

```
tenant-{org_id}/shared/agents/
  review/
    code-reviewer/
      AGENT.md
    security-auditor/
      AGENT.md
```

## 3. Create a Sub-Agent

### From the Web UI

Navigate to the **Sub-agents** page in the sidebar. Click **New**, fill the form (name, optional category, AGENT.md content pre-populated with a template), and save. The new agent lands in your user bucket and is immediately visible to your coordinator sessions.

### Via the REST API

```bash
curl -X POST http://localhost:8000/v1/agents \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "$(jq -n \
    --arg name 'code-reviewer' \
    --arg content "$(cat code-reviewer.md)" \
    --arg category 'review' \
    '{name: $name, content: $content, category: $category}')"
```

Response:
```json
{
  "success": true,
  "message": "Sub-agent 'code-reviewer' created.",
  "category": "review"
}
```

The `name` in the request body **must match** the `name:` field inside the AGENT.md frontmatter — the server rejects mismatches with `422` because a mismatch would produce a ghost agent whose storage path disagrees with its catalog listing.

## 4. Spawn a Sub-Agent

Once defined, the agent appears in the coordinator session's system prompt:

```
# Available Sub-Agents
Pass `agent_type=<name>` to `spawn_worker` or `delegate_task` to use one of
these pre-configured sub-agent types.

- **code-reviewer** — Reviews code for correctness and security
  Tools: read_file, search_files, terminal
  Model: claude-sonnet-4-6
```

The coordinator LLM spawns it with:

```
spawn_worker(
    goal="Review the auth middleware diff in src/auth.py",
    agent_type="code-reviewer"
)
```

or synchronously via:

```
delegate_task(
    goal="Review this PR and return a summary",
    agent_type="code-reviewer"
)
```

### Precedence Rules

Explicit arguments on `spawn_worker` / `delegate_task` always win over the agent def's presets:

| Argument | If explicit | If omitted |
|---|---|---|
| `tools` | Overrides `agent_def.tools` entirely | Falls back to `agent_def.tools` |
| `model` | Overrides `agent_def.model` | Falls back to `agent_def.model` |
| `max_iterations` | (not a spawn arg) | Taken from `agent_def.max_iterations`, clamped at worker ceiling (30) |

`disallowed_tools` is always additive — the agent def's denylist is unioned on top of the default worker exclusions.

Unknown or disabled `agent_type` values return a clear JSON error from the spawn tool; no child session is created.

## 5. Child Session Lifecycle

When a coordinator calls `spawn_worker(agent_type=...)`, the following happens:

1. **Resolution.** The handler calls `resolve_agent_by_name(name, tenant)` — the shared helper that both spawn-time and wake-time resolution use, so the rules cannot drift.
2. **Child creation.** A new `Session` row is inserted with `parent_id` set to the coordinator, and `config.agent_type` stamped with the resolved name.
3. **Hydration.** The agent def's `tools` / `disallowed_tools` / `max_iterations` / `policy_profile` are merged into `config` alongside any explicit spawn arguments.
4. **Enqueue.** The child's session id is pushed onto the Redis work queue.
5. **Event.** A `worker.spawned` event fires in the parent so the UI sees the child immediately.

Any worker can then pick up the child:

1. **Wake.** `AgentHarness.wake()` replays the child's event log.
2. **Re-resolve.** The wake-time resolver calls `resolve_agent_def(session, tenant)` — the same function family as step 1. This lets an admin hot-reload an agent def without re-spawning children.
3. **Apply.** `apply_agent_def_to_session()` hydrates the session's config non-destructively: explicit fields win, agent def values fill unset slots, `max_iterations` is clamped to the ceiling to prevent webhook-created sessions from granting themselves oversized budgets.
4. **Prompt.** The `PromptBuilder` replaces the org's default identity section with the agent's system prompt body. Non-platform agents flow through injection sanitization first.
5. **Loop.** The harness runs the full LLM loop with the scoped tool filter, model, and iteration budget.

On completion or failure, the harness emits `worker.complete` / `worker.failed` into the **parent's** event log (truncated result attached, ≤10 KB) and re-enqueues the parent so it wakes up and sees the result in its next turn.

## 6. Governance Policy Profiles

A sub-agent can declare an optional `policy_profile` that narrows the tenant's base governance policy for the duration of the child session. Profile composition is always tightening — a profile can never widen what the base permits.

Place profiles at `agents/policies/<name>.yaml` in either the platform volume or the org shared bucket:

```yaml
# /etc/surogates/agents/policies/read_only.yaml
allowed_tools:
  - read_file
  - search_files
  - list_files
  - web_search
denied_tools:
  - write_file
  - patch
  - terminal
egress:
  default_action: deny
  rules:
    - domain: docs.internal.example
      ports: [443]
      action: allow
```

Composition semantics:

- **Allowed** = `base.allowed ∩ profile.allowed` (intersection — profile cannot add tools the base forbids)
- **Denied** = `base.denied ∪ profile.denied` (union — profile cannot ungate tools the base denies)
- **Egress default** = stricter of the two (`deny` always beats `allow`; unknown values fall back to `deny`)
- **Egress rules** = base rules + profile rules (rules stack; profile cannot delete base rules)
- **Enabled** = inherited from base; a disabled base stays disabled regardless of the profile

The composed gate is **frozen** when produced — profiles cannot be mutated mid-session.

## 7. Inheritance

What the child **shares** with the parent (no scoping, no duplication):

- **Skills** — the child sees the same tenant skill catalog the parent does.
- **MCP servers** — a single connection pool per tenant, reused across all sessions.
- **Experts** — the base LLM inside a sub-agent can still call `consult_expert` if it's in the allowed tool set.
- **Tenant memory** — `MEMORY.md` / `USER.md` are tenant-scoped, not session-scoped.
- **Session path** — the parent's session workspace (`sessions/{parent_id}/`) is **not** inherited by default; children get their own path in the configured agent bucket. Coordinators can opt in to sharing by passing `workspace_path` through the spawn config.

What the child **overrides**:

- System prompt (identity section only — memory, skills index, context files are still injected)
- Tool allowlist / denylist
- Model
- Iteration cap
- Governance policy profile

## 8. Observing Sub-Agents

### Session Tree Endpoints

The full parent / child / grandchild ancestry of a session is exposed via SQL views and REST:

- `GET /v1/sessions/{id}/tree` — recursive descendants (up to 200 nodes), each carrying `agent_type`, `status`, and counters. Backed by the `v_session_tree` view.
- `GET /v1/sessions/{id}/children` — one-level direct children, keyed by `parent_id`.

Both endpoints authorize on the root session's tenant and agent_id, so a descendant that somehow belonged to a different tenant would not leak.

### "Running" Panel (Web UI)

When viewing a session with sub-agents, the sidebar shows a live **Running** panel that polls the tree endpoint every 4 seconds while children are active (30s when everything has settled). Each row displays the agent type badge, status, message and tool-call counters, and a hover-revealed **Stop** button that interrupts the child via `POST /v1/sessions/{id}/pause`.

### Events

Sub-agent lifecycle surfaces as events in the parent's log:

| Event type | Emitted when | Data |
|---|---|---|
| `worker.spawned` | Coordinator creates a child | `{worker_id, goal, agent_type}` |
| `worker.complete` | Child finishes normally | `{worker_id, result}` (truncated to 10 KB) |
| `worker.failed` | Child raises or exceeds budget | `{worker_id, error}` |

Use the session event log to reconstruct any completed sub-agent interaction — the child's own event stream is still the source of truth for everything that happened inside it.

## 9. Web UI

**Library page** (`/agents`, sidebar → Sub-agents)

- Lists all visible sub-agents grouped by source: **Built-in** (platform filesystem), **Organization** (org bucket + DB overlay), **My sub-agents** (user bucket + DB overlay).
- Search box filters by name, description, category, or model.
- Detail view shows the full AGENT.md body (rendered as markdown), configuration panel (model, max_iterations, policy_profile, allowed/disallowed tools), category, and tags.
- User-scoped agents get **Edit** and **Delete** buttons; platform and org agents are read-only from the UI (admin tooling handles org changes).
- **New** opens a pre-filled AGENT.md template in a dialog.

**Running panel** (in the chat sidebar)

Shows the active session's live sub-agent tree, polls every 4s while any child is running (30s when idle), and surfaces Stop buttons on each active child.

## 10. REST API Reference

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/v1/agents` | List all sub-agent types visible to the current tenant |
| `GET` | `/v1/agents/{name}` | View full AGENT.md content, tools, model, policy profile |
| `POST` | `/v1/agents` | Create a user-scoped sub-agent in the tenant bucket |
| `PUT` | `/v1/agents/{name}` | Replace a user-scoped agent's AGENT.md content |
| `DELETE` | `/v1/agents/{name}` | Remove a user-scoped agent from the bucket |
| `GET` | `/v1/sessions/{id}/tree` | Recursive descendants of a session (up to 200 nodes) |
| `GET` | `/v1/sessions/{id}/children` | Direct children of a session (one level) |

See [Appendix B: REST API Reference](../appendices/api-reference.md#sub-agents) for full request / response schemas.

## 11. Worked Example

End-to-end flow for a coordinator spawning two sub-agents in parallel:

```
1. User (in a coordinator session):
   "Review the PR at branch feat/auth and also benchmark its startup time"

2. Coordinator LLM decides to fan out:
   spawn_worker(goal="Review feat/auth for correctness and security",
                agent_type="code-reviewer")
   spawn_worker(goal="Measure startup time vs. main on feat/auth",
                agent_type="perf-runner")

3. Two children created with their own event logs, both enqueued.
   Parent's event log now contains two `worker.spawned` events.
   "Running" panel shows both children with "active" status.

4. code-reviewer child wakes, runs with:
   tools=[read_file, search_files, terminal]
   disallowed_tools=[write_file, patch]
   model=claude-sonnet-4-6
   max_iterations=20
   policy_profile=read_only

   perf-runner child wakes with its own preset (different model,
   different tools, no policy_profile).

5. Both children complete.
   worker.complete events flow into the parent's event log with
   each child's final response attached (≤10 KB).

6. Coordinator LLM synthesises both results for the user.
```

Sub-agents are the composability primitive for problems that decompose into independent subtasks with divergent tool/model/governance needs — multi-step research, parallel code review, fan-out experiments, anything where a fresh context window and a tighter governance envelope beat doing the work inline.
