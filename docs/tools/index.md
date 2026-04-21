# 7. Tools

Tools are capabilities that the agent can invoke during a session. Every tool call passes through governance before execution.

## Overview

Surogates comes with 14 builtin tools. Additional tools can be added via MCP servers (see [MCP Integration](../mcp-integration/index.md)).

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

Ask the user a clarifying question and wait for a response.

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
