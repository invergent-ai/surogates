# 7. Tools

Tools are capabilities that the agent can invoke during a session. Every tool call passes through governance before execution.

## Overview

Surogates comes with 15 builtin tools. Additional tools can be added via MCP servers (see [MCP Integration](../mcp-integration/index.md)).

Tools run in one of three locations:

| Location | Description | Examples |
|---|---|---|
| **Worker** | Runs inside the worker process. No sandbox needed. | memory, web search, skills, session search, delegation |
| **Sandbox** | Runs inside the session's isolated sandbox pod. | terminal, file operations, code execution, browser |
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

### `delegate_task` / `spawn_worker` -- Sub-Agent Delegation

Spawn a child session for parallel or scoped work.  Pass an optional `agent_type=<name>` to apply a pre-configured [sub-agent](../sub-agents/index.md) preset (system prompt, tool filter, model, iteration cap, policy profile).  `delegate_task` blocks until the child completes; `spawn_worker` returns immediately with a worker ID and the result flows back as a `worker.complete` event in the parent's log.

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

Delegate a subtask to a fine-tuned SLM expert. See [Experts](../experts/index.md).

| Parameter | Type | Description |
|---|---|---|
| `expert` | string | Name of the expert to consult |
| `task` | string | What the expert should do |
| `context` | string | Relevant context (optional) |

### `browser_navigate` -- Browser Navigation

Navigate to a URL and return page content.

| Parameter | Type | Description |
|---|---|---|
| `url` | string | URL to navigate to |

### `create_artifact` -- Inline Chat Artifact

Render a named, versioned artifact inline in the chat thread: a Vega-Lite chart, a table, a standalone markdown document, a sandboxed HTML preview, or an SVG image. The LLM calls this tool whenever the user wants to **see and interact with** output in the chat rather than save it to disk (which is what `write_file` is for).

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
| `chart` | `vega_lite` (object) | `caption` | A complete Vega-Lite spec. **Inline data only** — `data.url` is blocked (SSRF guard). |
| `html` | `html` (string) | `caption` | Rendered in an iframe with `sandbox="allow-scripts"` — no same-origin, no forms, no top-nav. Scripts run but can't reach the parent page. |
| `svg` | `svg` (string) | `caption` | Rendered via `<img src="data:image/svg+xml;utf8,…">` — `<script>` inside SVG does **not** execute (browser image-mode). |

**Storage and retrieval.** The artifact's metadata is recorded as an `artifact.created` event on the session log (no payload — keeps the event stream small). The payload itself is persisted in the session bucket at `_artifacts/{artifact_id}/v{N}.json`. The `_` prefix marks the directory as server-internal, so it is hidden from the workspace file browser and blocked from read/write/delete via the workspace API. The chat UI fetches the payload on demand through `GET /v1/sessions/{id}/artifacts/{artifact_id}`.

**Limits.** 500 KB per artifact, 200 artifacts per session. A `caption` is optional for all kinds except markdown.

**One artifact per response.** The harness enforces this softly via prompt guidance, not at the tool level. If an assistant turn ends with a ` ```svg ` or ` ```html ` fence in its content (some smaller models prefer fences over tool calls), the harness auto-promotes the first such fence into a real artifact so the user sees the rendered output regardless.

**Frontend rendering.** Each kind has a dedicated renderer under `web/src/components/chat/artifacts/`. Artifacts appear in the timeline at the point the tool ran, inside a bordered card with copy and download actions. The chart renderer is lazy-loaded so the Vega bundle only ships when a chart appears.
