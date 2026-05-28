# 7. Tools

Tools are capabilities that the agent can invoke during a session. Every tool call passes through governance before execution.

## Overview

Surogates comes with builtin tools for memory, skills, delegation, web access,
vision, artifacts, file operations, shell execution, and scheduling. Additional
tools can be added via MCP servers (see [MCP Integration](../mcp-integration/index.md)).

Tools run in one of three locations:

| Location | Description | Examples |
|---|---|---|
| **Worker** | Runs inside the worker process. No workspace sandbox needed. | memory, web search, skills, browser, session search, delegation, cron scheduling, loop control |
| **Sandbox** | Runs inside the session's isolated sandbox pod. | terminal, file operations, code execution |
| **MCP Proxy** | Forwarded to an external MCP server via the proxy. | Any tool registered by an MCP server |

## Builtin Tools Reference

### `terminal` -- Shell Command Execution

Executes shell commands in the sandbox.

| Parameter | Type | Description |
|---|---|---|
| `command` | string | Shell command to execute |
| `workdir` | string | Working directory (default: `/workspace`) |
| `timeout` | integer | Timeout in seconds (default: 180) |

Output is truncated at 50,000 characters. ANSI escape codes are stripped. Background execution is supported.

### `read_file` -- Read File Contents

| Parameter | Type | Description |
|---|---|---|
| `path` | string | File path to read |
| `offset` | integer | Start line (optional) |
| `limit` | integer | Max lines to read (optional) |

Handles plain text plus `.pdf`, `.docx`, `.xlsx`, `.pptx` (parsed to
markdown via `markitdown`) and image files (described by the worker's
vision model, with the analysis reshaped into a `read_file` envelope).
Document parses are cached on disk under `/tmp/surogates-read-cache/`
so paginated reads of the same file are free; image analyses are
cached in worker memory.  Agents should call `read_file(path)`
directly for these formats rather than pre-extracting with subprocess
tools.  Set `READ_IMAGE_CACHE_DISABLED=1` to bypass the image cache.

For attachments uploaded with the user message itself (via the chat UI),
the harness now parses files under 2 MB at send time and inlines the
parsed markdown directly into the user message — the agent receives
the content without making an extra `read_file` call. Files larger
than 2 MB, files in unsupported formats, and files that fail to parse
fall back to the previous behaviour: a system note tells the agent
the file is in the workspace and `read_file` is the way to access it.

### `write_file` -- Write File Contents

| Parameter | Type | Description |
|---|---|---|
| `path` | string | File path to write |
| `content` | string | File contents |

Writes to sensitive paths (SSH keys, shell rc files, credential files) are denied.

### `patch` -- Apply Patches

| Parameter | Type | Description |
|---|---|---|
| `path` | string | File to patch |
| `diff` | string | Unified diff or V4A patch |

Supports both standard unified diff and V4A patch format. Includes fuzzy matching for context lines.

### `search_files` -- Search File Contents

| Parameter | Type | Description |
|---|---|---|
| `pattern` | string | Search pattern (regex) |
| `path` | string | Directory to search (default: `/workspace`) |
| `include` | string | File glob pattern (optional) |

### `list_files` -- List Directory Contents

| Parameter | Type | Description |
|---|---|---|
| `path` | string | Directory to list |
| `recursive` | boolean | Include subdirectories (default: false) |

### `execute_code` -- Programmatic Code Execution

Executes code programmatically, enabling agents to call tools from within generated code.

| Parameter | Type | Description |
|---|---|---|
| `language` | string | Programming language |
| `code` | string | Code to execute |

### `web_search` -- Web Search

| Parameter | Type | Description |
|---|---|---|
| `query` | string | Search query |
| `max_results` | integer | Maximum results (default: 5) |

Backends: Tavily and Exa. Respects `robots.txt` and rate limits.

### `web_extract` -- Extract Web Page Content

| Parameter | Type | Description |
|---|---|---|
| `url` | string | URL to extract content from |

### `web_crawl` -- Crawl Multiple URLs

| Parameter | Type | Description |
|---|---|---|
| `urls` | array | List of URLs to crawl |

### `vision_analyze` -- Image Analysis

Analyzes a workspace image, safe remote image URL, or `data:image` URL with the active vision-capable session model.

| Parameter | Type | Description |
|---|---|---|
| `image` | string | Workspace-relative path, HTTPS image URL, or `data:image` base64 URL |
| `question` | string | What to inspect or answer about the image |
| `detail` | string | `auto`, `low`, or `high` provider detail hint |

### `session_search` -- Conversation History Search

| Parameter | Type | Description |
|---|---|---|
| `query` | string | Search query |
| `session_id` | string | Session to search (optional, defaults to current) |

Searches the session's event history using full-text search with ranking and result summarization.

### `skills_list` / `skill_view` -- Skill Operations

`skills_list` returns names and descriptions of available skills. `skill_view` returns the full content of a specific skill.

### `skill_manage` -- Skill CRUD

Create, update, and delete skills. Includes validation (name, frontmatter, content size).

### `memory` -- Memory Operations

| Parameter | Type | Description |
|---|---|---|
| `action` | string | `add`, `replace`, or `remove` |
| `target` | string | `memory` (MEMORY.md) or `user` (USER.md) |
| `text` | string | Content to save |
| `old_text` | string | Text to replace/remove (for `replace`/`remove`) |

### `todo` -- Task Tracking

Per-session todo list for tracking progress on multi-step tasks.

### `cron_create` / `cron_delete` / `cron_list` -- Scheduled Sessions

Manage user-owned scheduled prompts. These are the tool equivalents behind
cron-backed work such as reminders and fixed polling workflows. Schedules are
stored in PostgreSQL, scoped to the current `org_id`, `user_id`, and `agent_id`,
then picked up by that agent's worker and enqueued as fresh
`channel="scheduled"` sessions.

For user-facing slash commands, see [Commands](../commands/index.md).

`cron_create` schedules a prompt or slash command.

| Parameter | Type | Description |
|---|---|---|
| `cron` | string | Five-field cron expression, for example `*/10 * * * *` |
| `prompt` | string | Prompt or slash command to run when the schedule fires |
| `recurring` | boolean | Whether to keep running after the first fire (default: `true`) |
| `durable` | boolean | Accepted for Claude/Kairos compatibility; schedules are DB-backed in Surogates |
| `name` | string | Optional display name |
| `timezone` | string | IANA timezone for cron interpretation (default: `UTC`) |

`cron_delete` cancels a schedule by `id`.

| Parameter | Type | Description |
|---|---|---|
| `id` | string | Scheduled session ID |

`cron_list` lists active schedules for the current user and agent.

Scheduled prompts are user-owned only. Service-account, anonymous, or
system-only sessions cannot create user schedules. Prompts are scanned before
persistence for prompt-injection markers, invisible Unicode, secret-exfiltration
patterns, and destructive command patterns.

### `loop_wait` -- Dynamic Loop Control

Sets the next delay for a dynamic `/loop` run, or declares the loop finished.
This tool is exposed only inside scheduled sessions that were created by
`/loop <prompt>` without a fixed interval. It is not a general scheduling API;
normal sessions should use `cron_create` or the `/loop` command.

| Parameter | Type | Description |
|---|---|---|
| `delay_seconds` | integer | Seconds to wait before the next run. Values are clamped to 60 through 3600. Ignored when `completed` is true |
| `reason` | string | Brief explanation for the selected delay, or for finishing the loop when `completed` is true |
| `completed` | boolean | Optional. When `true`, the schedule's status flips to `completed` and no further runs are scheduled. Use this when the loop's task is done and there is no future work to wait for |

After a dynamic loop run completes, the schedule remains active and the next
`channel="scheduled"` session fires at `now + delay_seconds`. If the agent does
not call `loop_wait`, Surogates applies a 10-minute fallback delay. Dynamic
loops auto-expire after 7 days.

Calling `loop_wait` is terminal for the current dynamic loop run: after the
tool succeeds, the scheduled child session is completed instead of continuing
to call more tools. Dynamic loop child sessions do not expose `cron_create`,
`cron_list`, or `cron_delete`, which prevents a loop iteration from creating
nested schedules. Users can manage dynamic loops from normal sessions with
`/loop list`, `/loop cancel <id>`, `cron_list`, and `cron_delete`.

### `loop_complete` -- Fixed-Cron Loop Termination

Marks the current `/loop` schedule finished from inside a fixed-cron run.
This tool is exposed only inside scheduled sessions that were created by
`/loop <interval> <prompt>` with a cron-style interval. Dynamic loops use
`loop_wait` with `completed: true` instead.

| Parameter | Type | Description |
|---|---|---|
| `reason` | string | Brief reason for completing the loop -- typically the stop condition that was reached |

A fixed-cron `/loop` keeps firing on its cron expression until `expires_at`
(default 3 days) or until something explicitly cancels it. Phrasings in the
prompt like `stop after 5 entries` describe a stop condition the agent must
check on every wake -- they are not enforced by the runtime. When the
condition is met, calling `loop_complete` flips the schedule to
`status=completed` and clears `next_run_at`, so the schedule never wakes
again. The reason is stored on the schedule row (`schedule.last_completed_reason`)
for audit.

Fixed-cron loop child sessions do not expose `cron_create`, `cron_list`, or
`cron_delete`, so `loop_complete` is the only in-session way to stop the
schedule. As a fallback, the user can always run `/loop cancel <id>` from a
normal session.

### `delegate_task` / `spawn_worker` -- Sub-Agent Delegation

Spawn a child session for parallel or scoped work.  Pass an optional `agent_type=<name>` to apply a pre-configured [sub-agent](../sub-agents/index.md) preset (system prompt, tool filter, model, iteration cap, policy profile).  `delegate_task` blocks until the child completes; `spawn_worker` returns immediately with a worker ID and the result flows back as a `worker.complete` event in the parent's log.

### Subagent Task Layer Tools

The task layer wraps `spawn_worker` with durable, DAG-aware, retry-with-history semantics. See [Tasks](../tasks/index.md) for the full conceptual chapter (when to use, state machine, dispatcher tick, event vocabulary). Six tools register into the same `core` toolset:

| Tool | Available to | Purpose |
|---|---|---|
| `spawn_task` | Coordinator agents | Create a durable task; optionally with DAG `parents=[...]` |
| `unblock_task` | Coordinator agents | Resume a blocked task with optional context |
| `cancel_task` | Coordinator agents | Abort a non-terminal task |
| `worker_complete` | Workers running for a task | Mark own attempt done with structured handoff |
| `worker_block` | Workers running for a task | Self-pause without consuming a retry |
| `worker_context` | Workers running for a task | Read own task + parents + prior attempts |

The three "self-tools" are gated by `Session.task_id is not None`; plain chat and `spawn_worker` children never see them. Children spawned via either `spawn_worker` or `spawn_task` cannot recursively spawn tasks (the coordinator-side tools are in `WORKER_EXCLUDED_TOOLS`).

#### `spawn_task` -- Durable Subagent Task

Create a Task row that survives parent crash, supports fan-in dependencies, and retries on transient failure. Eager-spawns the child Session when no parents are pending; otherwise stays in `todo` until the dispatcher tick promotes it.

| Parameter | Type | Description |
|---|---|---|
| `goal` | string | Complete, self-contained description of the task. Subagents do not see the parent's conversation. |
| `context` | string | Optional structured context appended as a `## Context` block in the worker's first user message |
| `agent_type` | string | Optional pre-configured sub-agent type name (see [Sub-Agents](../sub-agents/index.md)) |
| `parents` | array | Task ids this task depends on. Stays `todo` until every parent reaches `done`. Cancelled/failed parents do **not** promote children -- orchestrate explicitly. |
| `max_attempts` | integer | Retry budget (default 3). Transitions to `failed` after this many consecutive crash/timeout attempts. |

Returns `{"task_id": str, "status": "todo" | "ready" | "running"}`. Status is `running` (with `worker_id`) when the task was eagerly spawned, `todo` when waiting on parents, `ready` when the dispatcher tick beat the eager-spawn path and already claimed it.

#### `unblock_task` -- Resume a Blocked Task

| Parameter | Type | Description |
|---|---|---|
| `task_id` | string | Task to unblock |
| `additional_context` | string | Optional new context appended to the task; surfaced as part of the next attempt's initial user message |

Only the spawning parent session may unblock its own children. Status must be `blocked`; transitions back to `ready` for re-claim on the next tick.

#### `cancel_task` -- Abort a Non-Terminal Task

| Parameter | Type | Description |
|---|---|---|
| `task_id` | string | Task to cancel |
| `reason` | string | Optional human-readable reason |

Only the spawning parent session may cancel. Status must be non-terminal (`todo` / `ready` / `running` / `blocked`). If `running`, publishes to `INTERRUPT_CHANNEL_PREFIX<worker_id>` (the same channel `stop_worker` uses) so the in-flight Session exits cleanly.

#### `worker_complete` -- Explicit Structured Handoff

Available only when running for a task. Marks the task `done` and writes the structured handoff fields. Prefer this over the natural-completion path when you want machine-readable output for downstream automation.

| Parameter | Type | Description |
|---|---|---|
| `summary` | string | 1-3 sentence human-readable handoff. Becomes `task.result` and the `result` field on the `worker.complete` event delivered to the parent. |
| `metadata` | object | Free-form JSON dict. Common keys: `changed_files`, `tests_run`, `tests_passed`, `decisions`, `findings`, `approved`. Becomes `task.result_metadata` and the `metadata` field on the parent's `worker.complete` event. |

Plain workers that complete naturally (without calling this tool) get their auto-extracted LLM final response as `result` and no `metadata` -- the existing `spawn_worker` contract is unchanged.

#### `worker_block` -- Self-Pause for Context

Available only when running for a task. Pauses the current attempt without consuming a retry budget (blocking is a deliberate pause, not a failure).

| Parameter | Type | Description |
|---|---|---|
| `reason` | string | One-sentence reason naming the specific decision needed. Surfaced in the `task.blocked` event the parent receives. |

Emits a `task.blocked` event to the spawning parent and publishes an interrupt on the worker's own session channel so the harness exits cleanly. The task stays in `blocked` until someone calls `unblock_task` -- the spawning parent agent or a future human-facing dashboard control.

#### `worker_context` -- Read Own Task Context

Available only when running for a task. Returns a JSON object with the calling worker's task, its parent tasks (with their completed results and metadata), and prior attempt summaries linked via `sessions.task_id`. Use this on retry to read the full detail of every prior attempt -- the new attempt's USER_MESSAGE already includes a brief summary, but `worker_context` exposes the structured form.

No parameters. Returns:

```json
{
  "task": {"id", "goal", "context", "status", "attempt_count", "max_attempts", "agent_def_name", "blocked_reason"},
  "parents": [{"id", "goal", "status", "result", "result_metadata"}, ...],
  "prior_attempts": [{"session_id", "outcome", "summary"? | "blocked_reason"?}, ...]
}
```

Prior attempts are classified as `"completed"` (worker finished, summary present), `"blocked"` (called `worker_block`, reason present), or `"crashed"` (no completion event -- timeout / hard-kill / OOM).

### `clarify` -- Interactive Clarification

Ask the user one or more structured clarifying questions and block until they submit all answers.  The web channel renders the call as a tabbed widget; each tab is one question with labeled radio choices and an optional "Other" free-form field.

| Parameter | Type | Description |
|---|---|---|
| `questions` | array | 1 to 5 questions, each rendered as a tab |
| `questions[].prompt` | string | The question text |
| `questions[].choices` | array | Up to 4 `{label, description?}` options. Omit for an open-ended question. |
| `questions[].allow_other` | boolean | Whether the widget appends an "Other" option with a text field (default `true`) |

The user picks an answer per tab and submits the batch.  The tool returns JSON with either `{"cancelled": false, "responses": [{question, answer, is_other}, ...]}` or `{"cancelled": true, "reason": "..."}` when the user stops the chat instead of answering (session paused) or the 30-minute wait cap is hit.

**Round-trip.**  The tool call is emitted as a normal `tool.call` event carrying the `questions` spec.  Submission goes to `POST /v1/sessions/{id}/clarify/{tool_call_id}/respond`, which appends a `clarify.response` event.  The worker's clarify handler polls the event log for the matching `tool_call_id`, renewing the session lease while it waits, and returns the answers to the LLM.  Session replay re-locks the widget from the same `clarify.response` event.

Do **not** use this tool for simple yes/no confirmation of dangerous commands — the terminal tool already prompts for those.  Prefer a reasonable default when the decision is low-stakes.

### `consult_expert` -- Expert Delegation

Delegate a subtask to a configured task-specialized expert model. The `expert` value must be one of the active expert names listed in the system prompt's `# Available Experts` section. See [Experts](../experts/index.md).

| Parameter | Type | Description |
|---|---|---|
| `expert` | string | Name of the expert to consult |
| `task` | string | What the expert should do |
| `context` | string | Relevant context (optional) |

### Browser Tools

Browser tools give the agent a session-scoped Chromium browser for interactive
web tasks. They are harness-local tools backed by a separate browser
container/pod, not by the execution sandbox. The browser mounts the same
session workspace at `/workspace`, so downloads and saved screenshots are
visible in the workspace. See [Browser Use](../browser-use/index.md) for
lifecycle, live view, user control handoff, deployment, and security details.

### `browser_navigate` -- Browser Navigation

Navigate to a URL and return the final URL and page title.

| Parameter | Type | Description |
|---|---|---|
| `url` | string | URL to navigate to |
| `wait_until` | string | Optional wait mode: `load`, `domcontentloaded`, or `networkidle` |

Other browser tools include `browser_get_state`, `browser_click`,
`browser_type`, `browser_press_key`, `browser_scroll`, `browser_drag`,
`browser_wait`, `browser_screenshot`, and `browser_close`.

### `create_artifact` -- Inline Chat Artifact

Render a named, versioned artifact inline in the chat thread: a Chart.js chart, a table, a standalone markdown document, a sandboxed HTML preview, or an SVG image. The LLM calls this tool whenever the user wants to **see and interact with** output in the chat rather than save it to disk (which is what `write_file` is for).

| Parameter | Type | Description |
|---|---|---|
| `name` | string | Short, descriptive title (1-120 chars, shown as the artifact's header) |
| `kind` | string | One of `markdown`, `table`, `chart`, `html`, `svg` |
| `spec` | object | Kind-specific content (see below). Never flatten these keys to the top level. |

**Kind-specific `spec` fields:**

| Kind | Required | Optional | Notes |
|---|---|---|---|
| `markdown` | `content` (string) | — | Rendered with the chat's Streamdown pipeline (code fences, math, mermaid). |
| `table` | `columns` (array of strings), `rows` (array of objects keyed by column name) | `caption` | Scrolls horizontally on wide data. Capped at 50 columns × 2000 rows. |
| `chart` | `chart_js` (object) | `caption` | A complete Chart.js config object with `type`, `data`, and optional `options`. Keep data inline and self-contained. |
| `html` | `html` (string) | `caption` | Rendered in an iframe with `sandbox="allow-scripts"` — no same-origin, no forms, no top-nav. Scripts run but can't reach the parent page. |
| `svg` | `svg` (string) | `caption` | Rendered via `<img src="data:image/svg+xml;utf8,…">` — `<script>` inside SVG does **not** execute (browser image-mode). |

**Storage and retrieval.** The artifact's metadata is recorded as an `artifact.created` event on the session log (no payload — keeps the event stream small). The payload itself is persisted under the session path at `_artifacts/{artifact_id}/v{N}.json`. The `_` prefix marks the directory as server-internal, so it is hidden from the workspace file browser and blocked from read/write/delete via the workspace API. The chat UI fetches the payload on demand through `GET /v1/sessions/{id}/artifacts/{artifact_id}`.

**Limits.** 500 KB per artifact, 200 artifacts per session. A `caption` is optional for all kinds except markdown.

**One artifact per response.** The harness enforces this softly via prompt guidance, not at the tool level. If an assistant turn ends with a ` ```svg ` or ` ```html ` fence in its content (some smaller models prefer fences over tool calls), the harness auto-promotes the first such fence into a real artifact so the user sees the rendered output regardless.

**Frontend rendering.** Each kind has a dedicated renderer under `web/src/components/chat/artifacts/`. Artifacts appear in the timeline at the point the tool ran, inside a bordered card with copy and download actions. The chart renderer is lazy-loaded so Chart.js only ships when a chart appears.
