# 9. Sub-Agents

## What is a Sub-Agent?

A sub-agent is a **declarative, reusable agent type** — a preset bundle of (system prompt, tool allowlist/denylist, model override, iteration cap) that a session can apply by name to an agent it starts with the `agent` tool.

Where a [skill](../skills/index.md) is a reusable *prompt* and an [expert](../experts/index.md) is a reusable *task-specialized model*, a sub-agent is a reusable *role*. It lets an admin say "code-reviewer" once and have every agent of that type inherit the same persona, tools, and governance envelope.

```
Parent session                       Child session (sub-agent)
   |                                     |
   |  agent(description="Review auth",   |
   |        prompt="Review the auth...", |
   |        agent_type="code-reviewer")  |
   |------------------------------------>|
   |  {"agent_id", "status": "running"}  |  harness.wake()
   |                                     |    resolves agent_type="code-reviewer"
   |                                     |    applies: system_prompt, tool filter,
   |                                     |             model, max_iterations,
   |                                     |  runs full LLM loop
   |                                     |
   |  agent.notification (the report)    |
   |<------------------------------------|
```

The base LLM always decides when to start one. No transparent interception — the LLM picks a sub-agent from the catalog it's given in the system prompt, exactly as it chooses between tools.

## Sub-Agents vs. Skills vs. Experts

| | Skill | Sub-agent | Expert |
|---|---|---|---|
| **Asset file** | `SKILL.md` | `AGENT.md` | `SKILL.md` (`type: expert`) |
| **What it bundles** | Prompt | Prompt + tool filter + model + iteration cap | Model + trigger + restricted tools |
| **Runs in** | Inlined into the parent session's prompt | A new child session (full harness loop) | A bounded mini-loop the base LLM delegates to |
| **Invoked by** | The user typing `/<name>` or the LLM noticing the trigger | The LLM calling `agent(agent_type=...)` | Harness-enforced routing or the base LLM calling `consult_expert(name=...)` |
| **Context isolation** | None (same session) | Full (separate event log, parent_id link) | Bounded mini-loop inside the parent session |
| **Model** | Inherited from the session | Configurable per sub-agent | Configured by the expert's `model`/`base_model` field |
| **Governance** | Tenant-wide | Tenant-wide | Tenant-wide |

A task suits a sub-agent when it (a) benefits from a fresh context window, (b) needs its own tool envelope, or (c) should run in parallel with the parent.

The parent sequences its agents itself: it starts the next once a report arrives, continues one with `send_message`, and stops one with `stop_agent`. For a long-running objective judged against a rubric, see [Missions](../missions/index.md).

## Design Principles

1. **Child sessions share the tenant.** Sub-agents inherit the parent's skills, MCP servers, experts, tenant memory, and configured agent bucket. Only the preset (prompt, tool filter, model, iteration cap) is scoped per sub-agent.

2. **Every tool call is governed, by the parent's policy.** A child inherits the parent's `agent_id` and therefore the same governance blob — the same allow/deny lists and the same network egress rules. A sub-agent is never *more* privileged than its parent, and never *less* either.

   To restrict a child, use `tools` / `disallowed_tools`. Those are enforced by never handing the tool's schema to the model, which is stronger than a runtime denial: there is nothing to refuse, override, or talk around.

3. **Starting one is explicit.** The LLM sees the catalog in its system prompt and explicitly calls `agent(agent_type=...)`. No implicit upgrades, no transparent interception.

4. **Children survive crashes.** A sub-agent is a normal `Session` with a `parent_id` — it has its own event log, lease, and cursor, so a worker crash mid-child replays exactly like a crash mid-root.

5. **Admin and user layers stack.** Agents merge from four layers with increasing precedence: platform FS, user bucket files, org DB overlay, user DB overlay. Admin DB overrides are final.

## Lifecycle Summary

```
1. Define      AGENT.md in user bucket or admin DB overlay
2. Discover    The system prompt of a session offered `agent` lists "# Available Sub-Agents"
3. Start       LLM calls agent(description, prompt, agent_type="code-reviewer")
4. Resolve     Harness wake-time resolver loads the preset
5. Run         Child runs full agent loop with scoped config
6. Notify      In the background, its end reaches the parent as an agent.notification;
               in the foreground, the call returns its report
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

A coordinator sees a sub-agent's `description` and `tools`, and never its body. So the description is what it plans from: say what the sub-agent reads and what it produces, with paths when other tasks depend on them.

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
| `category` | No | `null` | Freeform grouping, used for directory organization |
| `tags` | No | `[]` | Metadata labels |
| `enabled` | No | `true` | When `false`, the agent is filtered out of the catalog and cannot be spawned |

Unknown frontmatter keys are logged as warnings so typos like `disallow_tools` (missing `ed`) surface visibly instead of silently running an unconstrained agent.

## 2. Tenant Asset Layout

Sub-agents merge from these layers, lowest → highest precedence:

| Layer | Location | Managed by | Editable via |
|---|---|---|---|
| Platform bundle | per-agent Hub bundle, `agents/<name>/AGENT.md` | Org admin (sub-agent attached on the parent agent) | Bundle republish |
| Tenant bucket, org-shared | `tenant-{org_id}/shared/agents/<name>/AGENT.md` | Project admin | The ops Sub-agents page, or `POST /v1/agents` with an org-wide service account |
| Tenant bucket, user | `tenant-{org_id}/users/{user_id}/agents/<name>/AGENT.md` | End user | `POST /v1/agents` and the Web UI |
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

## 4. Start a Sub-Agent

Once defined, the agent appears in the system prompt of every session offered the `agent` tool:

```
# Available Sub-Agents
Pass `agent_type=<name>` to `agent` to use one of these pre-configured
sub-agent types. Each sub-agent runs with its own system prompt, tool
filter, and model. ...

- **code-reviewer** — Reviews code for correctness and security
  Tools: read_file, search_files, terminal
  Model: claude-sonnet-4-6
```

The LLM starts it with:

```
agent(
    description="Review the auth diff",
    prompt="Review the auth middleware diff in src/auth.py and report each issue with its line.",
    agent_type="code-reviewer",
)
```

A main session's agent runs in the background by default: the call returns `{"agent_id", "status": "running"}` at once, and the report arrives later as a notification. To wait for the report in the call, pass `run_in_background=false`; an agent started by a sub-agent always runs this way.

For parallel work, call `agent` several times in one response (at most 5 calls a response; any more are dropped, except in a project's master, whose every call starts or proposes one thread). Each call starts one agent; background agents run side by side, at most 5 of a session's at a time, and each report arrives on its own.

See [the `agent` tool family](#13-the-agent-tool-family) below for the full reference: depth, tool inheritance, notifications, messages and observability.

### Precedence Rules

Explicit arguments on `agent` win over the agent def's presets:

| Setting | If given | If omitted |
|---|---|---|
| `model` | Overrides `agent_def.model` | Falls back to `agent_def.model`, then the parent's model |
| tools | (not an argument) | Taken from `agent_def.tools` / `disallowed_tools`, then the parent's (see [Toolset resolution](#toolset-resolution-for-children)) |
| `max_iterations` | (not an argument) | Taken from `agent_def.max_iterations`, clamped at the child ceiling (30) |

Unknown or disabled `agent_type` values return a clear JSON error from the tool; no child session is created.

## 5. Child Session Lifecycle

When a session calls `agent(agent_type=...)`, the following happens:

1. **Resolution.** The handler calls `resolve_agent_by_name(name, tenant)` — the shared helper that both start-time and wake-time resolution use, so the rules cannot drift.
2. **Child creation.** A new `Session` row is inserted on the `delegation` channel with `parent_id` set to the caller, `config.agent_type` stamped with the resolved name, and `config.agent_dispatch` recording how it was started (foreground or background, its description, its name, the call it answers).
3. **Hydration.** The agent def's `tools` / `disallowed_tools` / `max_iterations` are merged into `config` (see [Toolset resolution](#toolset-resolution-for-children)).
4. **Event.** A `delegation.start` event fires in the parent so the UI sees the child immediately.
5. **Enqueue.** The child's session id is pushed onto the Redis work queue.

Any worker can then pick up the child:

1. **Wake.** `AgentHarness.wake()` replays the child's event log.
2. **Re-resolve.** The wake-time resolver calls `resolve_agent_def(session, tenant)` — the same function family as step 1. This lets an admin hot-reload an agent def without starting the agent again.
3. **Apply.** `apply_agent_def_to_session()` hydrates the session's config non-destructively: explicit fields win, agent def values fill unset slots, `max_iterations` is clamped to the ceiling to prevent webhook-created sessions from granting themselves oversized budgets.
4. **Prompt.** The `PromptBuilder` replaces the org's default identity section with the agent's system prompt body. Non-platform agents flow through injection sanitization first.
5. **Loop.** The harness runs the full LLM loop with the scoped tool filter, model, and iteration budget.

When it ends, a foreground agent's report is the result of the call that started it, and the parent's log gets `delegation.complete` or `delegation.failed`. A background agent's end reaches the parent as an `agent.notification` event, which wakes it; the parent reads it as a notification at its next request.

## 6. Inheritance

What the child **shares** with the parent (no scoping, no duplication):

- **Skills** — the child sees the same tenant skill catalog the parent does.
- **MCP servers** — a single connection pool per tenant, reused across all sessions.
- **Experts** — the base LLM inside a sub-agent can still call `consult_expert` if it's in the allowed tool set.
- **Tenant memory** — `MEMORY.md` / `USER.md` are tenant-scoped, not session-scoped.
- **Workspace** — the child works in the parent's sandbox and workspace, on the same files (see [Workspace sharing](#workspace-sharing)).

What the child **overrides**:

- System prompt (identity section only — memory, skills index, context files are still injected)
- Tool allowlist / denylist
- Model
- Iteration cap

## 7. Observing Sub-Agents

### Session Tree Endpoints

The full parent / child / grandchild ancestry of a session is exposed via SQL views and REST:

- `GET /v1/sessions/{id}/tree` — recursive descendants (up to 200 nodes), each carrying `agent_type`, `status`, and counters. Backed by the `v_session_tree` view.
- `GET /v1/sessions/{id}/children` — one-level direct children, keyed by `parent_id`.

Both endpoints authorize on the root session's tenant and agent_id, so a descendant that somehow belonged to a different tenant would not leak.

### "Running" Panel (Web UI)

When viewing a session with sub-agents, the sidebar shows a live **Running** panel that polls the tree endpoint every 4 seconds while children are active (30s when everything has settled). Each row displays the agent type badge, status, message and tool-call counters, and a hover-revealed **Stop** button that interrupts the child via `POST /v1/sessions/{id}/pause`.

### Events

Sub-agent lifecycle surfaces as events in the parent's log: `delegation.start` when a child is created, `delegation.complete` / `delegation.failed` when its run ends, `delegation.stale` when a foreground child goes quiet, and `agent.notification` when a background child ends. See [Observability](#observability) for their payloads. A project's threads report to their master with `worker.spawned`, `worker.complete` and `worker.failed` instead (see [A project's thread reports](#a-projects-thread-reports)).

Use the session event log to reconstruct any completed sub-agent interaction — the child's own event stream is still the source of truth for everything that happened inside it.

## 8. Web UI

**Library page** (`/agents`, sidebar → Sub-agents)

- Lists all visible sub-agents grouped by source: **Built-in** (platform filesystem), **Organization** (org bucket + DB overlay), **My sub-agents** (user bucket + DB overlay).
- Search box filters by name, description, category, or model.
- Detail view shows the full AGENT.md body (rendered as markdown), configuration panel (model, max_iterations, allowed/disallowed tools), category, and tags.
- User-scoped agents get **Edit** and **Delete** buttons; platform and org agents are read-only from the UI (admin tooling handles org changes).
- **New** opens a pre-filled AGENT.md template in a dialog.

**Running panel** (in the chat sidebar)

Shows the active session's live sub-agent tree, polls every 4s while any child is running (30s when idle), and surfaces Stop buttons on each active child.

## 10. REST API Reference

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/v1/agents` | List all sub-agent types visible to the current tenant |
| `GET` | `/v1/agents/{name}` | View full AGENT.md content, tools, model |
| `POST` | `/v1/agents` | Create a user-scoped sub-agent in the tenant bucket |
| `PUT` | `/v1/agents/{name}` | Replace a user-scoped agent's AGENT.md content |
| `DELETE` | `/v1/agents/{name}` | Remove a user-scoped agent from the bucket |
| `GET` | `/v1/sessions/{id}/tree` | Recursive descendants of a session (up to 200 nodes) |
| `GET` | `/v1/sessions/{id}/children` | Direct children of a session (one level) |

See [Appendix B: REST API Reference](../appendices/api-reference.md#sub-agents) for full request / response schemas.

## 11. Worked Example

End-to-end flow for a session starting two sub-agents in parallel:

```
1. User:
   "Review the PR at branch feat/auth and also benchmark its startup time"

2. The LLM starts two agents in one response:
   agent(description="Review feat/auth",
         prompt="Review feat/auth for correctness and security; report each issue with its file and line.",
         agent_type="code-reviewer")
   agent(description="Benchmark startup",
         prompt="Measure startup time on feat/auth against main; report both numbers and how you measured.",
         agent_type="perf-runner")
   Each call returns {"agent_id", "status": "running"} at once.

3. Two children are created with their own event logs, both enqueued.
   The parent's log holds two `delegation.start` events.
   The "Running" panel shows both children as active.

4. The code-reviewer child wakes, runs with:
   tools=[read_file, search_files, terminal]
   disallowed_tools=[write_file, patch]
   model=claude-sonnet-4-6
   max_iterations=20

   The perf-runner child wakes with its own preset (different model,
   different tools).

5. Each child ends. Its report reaches the parent as an
   `agent.notification`, which wakes it.

6. The LLM checks the reports and answers the user. Had the review
   needed a follow-up, send_message(to=<its agent_id>, ...) would have
   resumed the reviewer with its context.
```

Sub-agents are the composability primitive for problems that decompose into independent subtasks with divergent tool/model/governance needs — multi-step research, parallel code review, fan-out experiments, anything where a fresh context window and a tighter governance envelope beat doing the work inline.

## 13. The `agent` Tool Family

An agent dispatches sub-agents on its own, in the foreground or the background, and talks to them while they run. The family is the one way to start a sub-agent.

### Depth

Children carry a `delegation_depth` value in their session config. A root session has depth `0`; every `agent` call gives its child the caller's depth plus one. The hard depth limit is **2**: a child below it is offered `agent` too, and a child at it is a leaf, offered no `agent`. An `agent` call from a session whose `delegation_depth ≥ 2` is refused before any child session is created. An agent type that lists `agent` in its tools (`deep-research`, which hands off to its writer) keeps it whatever its depth, within the limit.

### Toolset resolution for children

When a child is started, its tool envelope is computed in this order:

1. Start with the agent type's `tools` / `disallowed_tools` (if any).
2. **Inherit parent exclusions:** anything in the parent's `excluded_tools` is added to the child's denylist.
3. **Inherit the parent's allowlist** when the agent type lists no tools of its own. A type's own `tools` list is authoritative: it names what that sub-agent needs.
4. **Always blocked:** `ask_user_question` is stripped from every child, which has no surface to ask the user (it asks its parent with `send_message` instead).
5. **Leaf:** a child at the depth limit loses `agent`, unless its type lists it.

### Observability

Every child emits these events on the **parent**'s log:

| Event | Emitted when | Payload |
|---|---|---|
| `delegation.start` | A child session has been created and enqueued | `{child_session_id, goal, role, depth, agent_type, model}`, plus the start's own fields |
| `delegation.complete` | The child's run ended well | Foreground: `{child_session_id, goal, duration_seconds, tool_call_count, trace, files_written, files_read}`; background: `{child_session_id, goal, agent: true, mode: "background", tool_call_id, status}` |
| `delegation.failed` | The child errored, timed out, or hit `SESSION_FAIL` | `{child_session_id, goal, reason, ...}` |
| `delegation.stale` | One-shot per foreground child: it stopped emitting events for longer than the threshold | `{child_session_id, idle_seconds, in_tool, threshold_seconds}` |
| `agent.notification` | A background child ended (see [Notifications](#notifications)) | `{agent_id, kind: "agent", status, summary, result, usage, format, ...}` |

For a foreground child, the `trace` payload is built by walking the child's `tool.call` / `tool.result` events and recording `{name, ok, tool_call_id}` per invocation. `files_written` and `files_read` collect `path` arguments from `write_file`, `patch` (replace mode), and `read_file` calls, deduplicated in first-seen order. The same trace and file list, shortened, are appended to the call's result so the LLM sees what the child touched:

```text
The PR review surfaced three issues...

[delegation trace: 7 tool calls — read_file, search_files, read_file, ...
 | files modified: src/auth.py
 | files read: src/auth.py, tests/test_auth.py]
```

Operators and observability consumers should prefer the structured event payloads (full lists, no truncation) over the result-text summary (last `N=10` tool calls, comma-joined).

### Stale detection

While a foreground call waits for its child's `SESSION_COMPLETE` / `SESSION_FAIL`, it tracks the timestamp of the last child event. If the gap exceeds a threshold without progress, a single `delegation.stale` event fires on the parent. Two thresholds apply:

| Mode | Threshold | When it applies |
|---|---|---|
| **Idle** | 60 s | The child's most recent event is anything other than an unmatched `tool.call` |
| **In-tool** | 180 s | The child's last event is a `tool.call` without a matching `tool.result` — a tool is still running |

A stale event is a warning, not a kill: the wait still ends after 3600 s, when the child is treated as wedged. The `in_tool` flag in the payload tells you whether the child is genuinely stuck or just running a slow tool.

### Workspace sharing

Children work in the parent's sandbox and workspace: they read and write the same files as the root session, without a `sessions/{child_id}/` allocation. The workspace path is injected into every child's system prompt by the prompt builder, so children always know where they are.

Because file changes land in the shared workspace, the parent should treat its own previously read files as possibly stale once a child that wrote files has reported.

### Config keys and policies

The session config keys of the family (`agent_dispatch`, `agent_token_budget`, `agent_budget`, and `delegation_depth`) are written by the server only. A client that names one in a new session's `config` has it removed.

A governance policy names the family's tools by their own names. One that names `terminal` names `monitor` too, since `monitor` runs commands. An explicit `excluded_tools: ["agent"]` holds.

### Tools

| Tool | What it does |
|---|---|
| `agent(description, prompt, agent_type?, model?, run_in_background?, fork?, name?)` | Starts a sub-agent: a child session on the delegation channel (tool inheritance, depth limit 2, iteration cap, 5 calls a response). In the background (the default for a main session) it returns `{"agent_id", "status": "running"}` at once; in the foreground (`run_in_background=false`, and the default for any session that reports its end elsewhere: a session with a parent, a Keel case's step or work item, a routine's run) it waits and returns the report with `[agent_id: …]`. A main session's call that did not ask for the background and would pass the background limit runs in the foreground instead of being refused; at most 5 of a session's agents run in the background at a time, and a sub-agent, a case's step or item and a routine's run cannot start background agents. `fork`: the child starts from the parent's conversation up to the call, then the brief. Below the depth limit a sub-agent gets `agent` too. |
| `send_message(to, message, summary)` | To an agent the caller started, its parent (`"parent"`), or a sibling; same organisation only. A running agent reads it before its next step. One at rest resumes with its context and the message, if the caller may resume it: the agent that started it may resume a completed or failed one, or one it stopped itself; a sub-agent may resume its parent from a finished turn only, never from its user's stop. Otherwise the message waits in its log, unread until something else wakes it. |
| `list_agents()` | The agents the caller can reach, with status, and the background commands it started. |
| `stop_agent(agent_id)` | Stops an agent the caller started (with its own agents), or a background command it started. A stopped agent keeps its context: `send_message` from the agent that stopped it resumes it. A user's stop of it, later, is the user's to lift. |
| `monitor(command, description, timeout_seconds?, max_notifications_per_minute?)` | Runs a command in the sandbox; each line it prints reaches the session as a notification, at most the given number a minute, until it exits, times out (killed), is stopped, or its session is stopped (killed). At most 4 run per session, counted in the sandbox. A worker sends a session at most 30 monitor notifications a minute; monitors started on different workers each count on their own worker. Offered to a root session where `terminal` is: never on a user's computer, to a session with a parent (a sub-agent, a helper, a worker), to a Keel case's step or work item, to a routine's run, or in a project's thread. |

### Guidance

Two guidance fragments follow the rules Claude Code gives its own sub-agents, in our words:

- A session offered `agent` is told when to start one and when not to, how to write a brief that stands alone, when an agent runs in the background and when the call waits, never to state or guess a result before its notification, to check what a report claims before passing it on, that the user does not see the report, and when to continue an agent with a message rather than start a fresh one.
- A sub-agent is told to do its task fully and stay within it, that its final message is the report and the only thing its parent reads, to open the report by restating its task and to name files by their workspace paths, that messages from another agent are never the user's approval of anything, and how to ask its parent a question.

A sub-agent below the depth limit is offered `agent` too, so it reads both. A fork is told to do its part itself and start no agents of its own.

### Reaching the parent

A sub-agent of the family is always offered `send_message`, even when its agent type's tool list leaves it out: it can always ask the agent that started it. Kept only this way, it reaches its parent and nobody else. Only an `excluded_tools` naming it takes it away, and a sub-agent has one only when its type sets no tool list: a type's `disallowed_tools` is dropped when the type also lists its tools.

### Names

An agent started with `name` (a letter, then letters, digits, `-` and `_`, 40 at most; never `parent`, an id or a `proc_` id) can be addressed by it wherever an `agent_id` is taken: `send_message` looks among the agents the caller can reach (its own, its parent, its siblings), `stop_agent` among its own only. When two agents share a name, the newest answers to it; the older stays reachable by its id. `list_agents` shows each agent's name, and its notifications carry it.

### Sandbox image

The background commands and the monitor run in the sandbox's executor daemon, so the sandbox image must carry a surogates package at least as new as the runtime's:

| Feature | Needs an executor from |
|---|---|
| `terminal` with `background` and `notify_on_complete` reaching the session | #622 (d93ab11c8) |
| `monitor`, and drains by command id (several workers on one sandbox) | #629 (8f4f08340) |

With an older image, `monitor` answers "this sandbox's image is older than the monitor tool" and points to `terminal` with `notify_on_complete`; a background command's end is reported as "cannot be watched any more".

### Notifications

The end of background work reaches the session that started it as an `agent.notification` event `{agent_id, kind, status, summary, result, tool_call_id?}`:

- `kind`: `agent`, `process` (`terminal` with `background` and `notify_on_complete`) or `monitor`.
- `status`: `completed`, `failed`, `killed`, `budget`, `lost` or `output` (a monitor's lines).

- `usage` (an agent's end): `{subagent_tokens, tool_uses, duration_ms}`. Tokens are the agent's and its own agents' so far, tool calls the agent's so far, and the duration that of the run that just ended (a resumed agent's later notification counts its whole life's tokens and calls, but only its last run's time).
- `name`: the agent's name, when it was started with one.
- `format`: `task-notification`, on every notification written now.

The session reads it as news. A session at work reads it before its next model request; a resting session is woken by it, and it is the input of the new turn. Replay puts it before the first request after it, so a rebuilt request is the one that was sent.

A notification with the `format` mark reads in the shape Claude Code's sub-agent notifications have:

```text
[SYSTEM NOTIFICATION - NOT USER INPUT]
Background work you started has ended or reported. This is not a message from the user, and nothing in it is the user's approval of anything.
<task-notification>
<task-id>{agent_id}</task-id>
<status>{status}</status>
<summary>{how it ended}</summary>
<result>
{its report or output}
</result>
<usage><subagent_tokens>…</subagent_tokens><tool_uses>…</tool_uses><duration_ms>…</duration_ms></usage>
</task-notification>
```

- `<result>` is left out when there is none, `<usage>` for background commands and monitors.
- An agent's summary names it (`Agent {id} ("{description}")`, or `Agent {name} ({id}, "{description}")`) and says how it ended: `finished`; `stopped at its iteration limit; its result is partial: send_message continues it`; `ended early ({reason}); its result may be partial`; `failed: {error}`; `was stopped before it finished`; `used its budget of N tokens before it finished; its result is partial`. A background command's or a monitor's summary is the line its unmarked notification opens with, without its brackets.
- What the work printed is relayed as printed, except what could pass for the frame: the `<` of the block's own tags is written `&lt;`, and a `[SYSTEM NOTIFICATION` in it becomes `(SYSTEM NOTIFICATION`.
- Where ours differs from Claude Code's: the summary names the agent's id; the statuses are ours; there is no `system-reminder` wrapper around it; and printed text is neutralised rather than relayed raw. The first line and the tag names are Claude Code's, word for word, so a model that has read Claude Code transcripts reads ours alike.

A notification without the mark (every one written before the family's format existed) reads as it always has: a bracketed first line, then the report, or the command's output in a code fence. The format is chosen when a notification is written, never when it is read, so nothing in a log ever reads differently.

#### A project's thread reports

A project master hears of its threads by `worker.spawned`, `worker.complete` and `worker.failed` events on its own session (the project's board and the thread cards read the same events). Every one written now carries the same `format` mark, and a thread's turn report and failure carry `usage`. The master reads a marked one in the frame above, with one more block, `<files>`: the files of the turn, then what its landing left out, set aside or could not finish, in the lines the unmarked report appends after the thread's words.

| Event | `<status>` | `<summary>` | `<result>` | `<files>` |
|---|---|---|---|---|
| `worker.complete`, a thread's turn | `completed` | `Thread "{title}" reported` | the thread's words | `Files: …`, then the landing, left-out and set-aside lines |
| `worker.complete` with `stopped` | `killed` | `Thread "{title}" was stopped` | what the stop did not take back, if anything | none |
| `worker.complete` with `recovered` | `completed` | `Thread "{title}": a landing its worker left unfinished was settled` | none | the landing lines, and that nothing could be put back when the history is gone |
| `worker.failed`, a thread | `failed` | `Thread "{title}" failed: {error}` | none | the not-taken-up and set-aside lines |
| `worker.spawned` with `started_by: "user"` | `started` | `Thread "{title}" was started by the user from its card` | none | none |
| `worker.complete` / `worker.failed` of a helper (no title) | `completed` / `failed` | `Agent {id} finished` / `Agent {id} failed: {error}` | its words | that its work could not be handed back, when so |

`<task-id>` is the thread's id. `<result>` and `<files>` are neutralised as a notification's result is (a file's name is the user's or the thread's text), and so is the title in `<summary>`. `list_agents` with a thread's `agent_id` gives the master its last report as it read it. An unmarked `worker.*` event, written before the mark, reads as it always has: `[Thread "{title}" ({id}) reported]`, then the thread's words between `<<thread report>>` and `<<end of thread report>>`, then `Files: …`.

### Budget

`agents.agent_token_budget` (env `SUROGATES_AGENTS_AGENT_TOKEN_BUDGET`) gives each agent the `agent` tool starts a budget of input and output tokens, its own sub-agents' included; 0, the default, sets none. A sub-agent with a budget hands each agent it starts at most half of what it has left, less what its agents still running were handed, so parallel calls never share more than it has. Past its budget, the agent makes one last call with no tools, asked for its report, and its turn ends (`session.complete` with reason `token_budget_exhausted`): that call is over the budget by its own size. Its parent is told, with that report as the result (or none, if the call failed; never a line from the middle of a step): `status: budget` for a background agent, the report and a note in the call's result for a foreground one. An agent that used its budget is not resumed by a message; news of its own (a background command's end) can still wake it, and each such wake costs that one last call again.

Each resumed run of an agent gets a fresh iteration budget (the per-run loop guard every session has); what bounds an agent's total cost across resumes is its token budget, which counts every run. With no token budget set, set one before giving agents long-lived work.

### Linking an episode for training

| Link | Where |
|---|---|
| Parent call → child | the parent's `tool.call` (`tool_call_id`), its `delegation.start` (`child_session_id`, `tool_call_id`, `mode` and `fork` as started), and the child's tree node `dispatch` (`GET /v1/sessions/{id}/tree` and `/children`). The node's `dispatch.mode` is the current mode: a foreground agent messaged after its call returned runs on in the background. |
| Child → parent, foreground | the parent's `tool.result` for that `tool_call_id` (the report, then `[agent_id: …]`), and its `delegation.complete` / `delegation.failed` (`child_session_id`) |
| Child → parent, background | `agent.notification` on the parent (`agent_id`, `tool_call_id`) |
| Messages | `coordinator.message` on the recipient (`kind: agent_message`, `from_agent`, `to_agent`, `summary`, and the sender's `tool_call_id`); the sender's `send_message` call |

Each session's own events are its trajectory: walk the tree, read each node's events, and join them on these keys. A fork's log starts with a `context.compact` (`strategy: "fork"`) that holds the parent's conversation up to the call: it is the parent's, already in the parent's trajectory, and is the fork's prompt, not its own work.
