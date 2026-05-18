# Surogates

Surogates is an open platform for running managed agents at scale. It is built
around a durable session log, stateless agent workers, isolated execution
sandboxes, and user-facing channels such as web chat, Slack, Telegram, website
widgets, and programmatic API sessions.

The core idea is simple: keep the agent's reasoning loop, execution
environment, and user interface decoupled so each part can fail, scale, and be
governed independently.

Read the full [documentation](docs/index.md).

![Surogates Web Chat UI](assets/webui.webp)

## What Surogates Provides

### Managed Agent Runtime

- Durable sessions backed by a PostgreSQL append-only event log.
- Stateless workers that can replay session state and recover after crashes.
- A Redis-backed orchestrator that wakes sessions and distributes work.
- Per-session leases so only one worker runs a session at a time.
- Server-Sent Events for real-time web updates and replay from the event log.
- Background jobs for session cleanup, idle-session reset, scheduled work, and
  expert training export.

### Isolated Execution

- A clear brain/hands split: workers run the LLM loop; sandboxes run untrusted
  code and file operations.
- Development sandboxes via local processes and production sandboxes via
  Kubernetes pods.
- Session-scoped workspaces mounted at `/workspace`.
- S3-compatible storage for session files, tenant assets, skills, memory, and
  MCP configuration.
- Workspace browsing, file viewing, uploads, downloads, and artifact rendering
  in the web UI.

### Multi-Tenancy

- Orgs, users, and channel identities with database-backed authentication.
- Per-org provider configuration (LLM endpoints, MCP servers, credentials).
- Service-account tokens for programmatic and batch access.
- Channel identity mapping (link Slack/Telegram users to internal accounts).
- Tenant context and per-org credential vault for secret isolation.

See [Multi-Tenancy](docs/multi-tenancy/index.md).

### Channels

- Web chat SPA with streaming events, workspace browsing, browser live view,
  session tree navigation, scheduled work, missions panel, and agent inbox.
- Slack and Telegram channel adapters for messaging workflows.
- Website widget SDK for embedding public chat entry points.
- API channel for non-interactive batch and pipeline use cases with
  service-account tokens.
- Shared delivery model through durable outbox rows and Redis nudges.

### Human-in-the-Loop Inbox

The agent inbox is a per-user queue of items that need attention:

- `input_required` for text answers through the `clarify` flow.
- `action_required` for browser login, MFA, OAuth approval, CAPTCHA, file
  picker, consent, or other external user actions.
- `task_complete` for completion summaries.
- `governance_gate` for user-overridable policy decisions.
- `progress_checkin` for long-running session updates.

Each inbox item can open the related session and can be deleted. Depending on
the kind, users can submit answers, acknowledge informational updates, approve
or reject governance gates, or mark external actions complete so the session can
resume.

See [Agent Inbox](docs/agent-inbox/index.md).

### Browser Use

Agents can control a real session-scoped Chromium browser:

- Navigate, click, type, scroll, inspect accessibility state, and capture
  screenshots.
- Share a workspace with sandbox tools.
- Let the user take over for login, MFA, CAPTCHA, and other manual steps.
- Continue from the same browser session after the user completes the action.

See [Browser Use](docs/browser-use/index.md).

### Tools and MCP

Surogates includes built-in tools for:

- Shell commands, file reads/writes, patching, code execution, and workspace
  operations.
- Web search, extraction, crawling, browser automation, and vision analysis.
- Memory, skills, sub-agent delegation, session search, scheduled work, and
  loop control.
- MCP servers over stdio and HTTP, with OAuth 2.1 PKCE support.
- MCP proxying with credential injection so sandboxes do not see tenant
  secrets.

Every tool call passes through governance before execution.

See [Tools](docs/tools/index.md) and
[MCP Integration](docs/mcp-integration/index.md).

### Skills, Sub-Agents, and Experts

- **Skills** are reusable prompt-based behaviors loaded from platform, org, and
  user layers.
- **Sub-agents** are declarative child-session presets with their own prompt,
  tool envelope, model override, iteration cap, and optional governance policy
  profile.
- **Experts** are task-specialized models or scoped mini-loops that can be
  selected for hard tasks and retrained from collected event data.

See [Skills](docs/skills/index.md), [Sub-Agents](docs/sub-agents/index.md), and
[Experts](docs/experts/index.md).

### Commands and Goals

Slash commands shape the next harness turn without going through a tool call:

- `/clear` — drop the current context and destroy the session sandbox while
  keeping the durable event log intact.
- `/compress` — force context compression on demand.
- `/goal <description>` — define an outcome with optional rubric. Surogates
  works the conversation, grades each final response against the rubric, and
  appends synthetic continuations until the evaluator returns `satisfied`,
  `blocked`, `failed`, or the iteration budget is reached.
- `/loop [interval] <prompt>` — schedule recurring user-owned work. Supports
  fixed intervals (`5m`, `1h`, `2d`) or dynamic self-pacing via `loop_wait`.
- `/<skill-name> [args...]` — invoke any skill directly from chat.

Programmatic clients can drive the same goal flow via `user.define_outcome` on
the `/v1/sessions/{id}/events` endpoint.

See [Commands](docs/commands/index.md) and
[Goals Quick Start](docs/goals/index.md).

### Tasks and Missions

The **task layer** adds durable, DAG-aware coordination on top of
`spawn_worker`:

- A task is a database row wrapping zero or more attempt sessions with a goal,
  optional `parents=[...]` for fan-in dependencies, structured `result` /
  `result_metadata`, and a `todo → ready → running → done/blocked/failed/cancelled`
  state machine.
- Six tools: `spawn_task`, `unblock_task`, `cancel_task`, `task_complete`,
  `task_block`, `task_show`.
- A 5-second dispatcher tick promotes ready tasks, finalizes completed attempts,
  retries on crash up to `max_attempts`, and enqueues new workers.
- Retry attempts get a "Prior attempts on this task" section injected into the
  initial user message, plus full structured access via `task_show`.

**Missions** are long-running, rubric-judged objectives built on the task
layer:

- `/mission <description>` plus a written rubric defines criterion-driven work
  (e.g. "satisfied when `result_metadata.accuracy >= 0.85`").
- The coordinator agent decomposes the goal into work tasks and a verifier task
  that records the measurable signal in `result_metadata`.
- An LLM judge grades the workstream against the rubric whenever a task reaches
  a terminal state, returning `satisfied`, `needs_revision`, `blocked`, or
  `failed`.
- `/mission pause`, `/mission resume`, `/mission cancel [--cascade]` control
  the loop without losing in-flight workers.
- A dedicated mission dashboard renders the rubric, current iteration, latest
  verdict, task DAG, and live worker activity.

See [Tasks and Missions](docs/tasks/index.md).

### Memory

Memory is stored as file-shaped assets:

- `MEMORY.md` for durable project or org knowledge.
- `USER.md` for user-specific preferences and stable facts.
- Frozen snapshots are injected at session start.
- Updates are security-scanned and deduplicated before storage.

See [Memory](docs/memory/index.md).

### Governance, Security, and Audit

- Tenant-scoped auth, storage, credentials, skills, memory, MCP config, and
  policies.
- Policy engine for allow-lists, deny-lists, ABAC rules, and file path
  containment, with per-session immutability once a session is frozen.
- Policy profiles that narrow child-session permissions.
- MCP tool scanning for prompt injection, invisible unicode, schema abuse, and
  rug-pull attacks (SHA-256 fingerprinting of tool definitions).
- Sandbox network isolation via Kubernetes NetworkPolicy.
- Credential vault, encrypted at rest, with per-org and per-user scoping.
- Saga tracking for multi-step tool chains with automatic compensation on
  failure.
- Per-org and per-user sliding-window rate limiting.
- Session event log, tenant audit log, and SQL views (typed projections) for
  compliance, debugging, dashboards, and training data.

See [Governance and Security](docs/governance-and-security/index.md) and
[Audit & Observability](docs/audit/index.md).

## Architecture

Surogates follows a three-component model:

```
Channels / API clients
        |
        v
API server
  - auth, tenant routing, REST APIs, web SPA
  - tenant storage and credential access
        |
        v
Redis orchestrator
        |
        v
Workers
  - stateless harness loop
  - tool routing, governance, memory, skills, MCP proxy calls
        |
        v
Sandboxes
  - isolated terminal, file, patch, and code execution
  - session-scoped workspace only
        |
        v
PostgreSQL session store
  - sessions, events, leases, inbox items, delivery outbox
```

The API server is the trusted control plane. Workers are stateless and can serve
any session. Sandboxes are isolated and receive only session-scoped workspace
access. If a worker or sandbox fails, the next run resumes from the durable
event log.

See [Architecture](docs/architecture/index.md).

## Repository Layout

| Path | Purpose |
|---|---|
| `surogates/` | Python backend: API server, worker harness, tools, storage, governance, jobs. |
| `web/` | React web application for the hosted chat UI. |
| `sdk/agent-chat-react/` | Shared React chat and inbox components used by the web app and downstream apps. |
| `sdk/website-widget/` | Embeddable website widget SDK. |
| `docs/` | User, operator, and architecture documentation. |
| `scripts/` | Development and release helper scripts. |
| `tests/` | Backend unit and integration tests. |

## Quick Start

For the complete local setup, follow [Getting Started](docs/getting-started/index.md).

At a high level, a development environment needs:

- Python 3.12+
- Node 20.19+ or 22.12+
- PostgreSQL
- Redis
- S3-compatible object storage for tenant assets and session workspaces
- An LLM provider reachable through an OpenAI-compatible API

Install and configure the Python package:

```bash
uv sync
export SUROGATES_CONFIG=/path/to/config.yaml
```

Run the API server and a worker:

```bash
SUROGATES_CONFIG=$SUROGATES_CONFIG uv run surogates api
SUROGATES_CONFIG=$SUROGATES_CONFIG uv run surogates worker
```

Run the web UI in development:

```bash
cd web
npm install
npm run dev
```

The detailed configuration reference is in
[Appendix A: Configuration](docs/appendices/configuration.md).

## Development Checks

Common backend checks:

```bash
uv run pytest
python -m compileall -q surogates
```

Shared React SDK checks:

```bash
cd sdk/agent-chat-react
npm run typecheck
npm test
```

Web app checks:

```bash
cd web
npm run typecheck
npm run build
```

Some integration tests require PostgreSQL, Redis, Docker, browser images, or
Kubernetes depending on the test marker.

## Documentation Map

- [Introduction](docs/intro/index.md)
- [Getting Started](docs/getting-started/index.md)
- [Architecture](docs/architecture/index.md)
- [Multi-Tenancy](docs/multi-tenancy/index.md)
- [Channels](docs/channels/index.md)
- [Browser Use](docs/browser-use/index.md)
- [Agent Inbox](docs/agent-inbox/index.md)
- [Commands](docs/commands/index.md)
- [Goals Quick Start](docs/goals/index.md)
- [Tools](docs/tools/index.md)
- [Skills](docs/skills/index.md)
- [Sub-Agents](docs/sub-agents/index.md)
- [Tasks and Missions](docs/tasks/index.md)
- [Experts](docs/experts/index.md)
- [MCP Integration](docs/mcp-integration/index.md)
- [Memory](docs/memory/index.md)
- [Governance and Security](docs/governance-and-security/index.md)
- [Audit & Observability](docs/audit/index.md)
- [Storage](docs/storage/index.md)
- [Background Jobs](docs/background-jobs/index.md)
- [Operations](docs/operations/index.md)
- [REST API Reference](docs/appendices/api-reference.md)
- [Configuration Reference](docs/appendices/configuration.md)
- [Glossary](docs/appendices/glossary.md)

## Contributing

Surogates builds on ideas from managed-agent architecture, sandboxed execution,
MCP, governance policy systems, and existing open agent projects. Notable
influences and dependencies include:

- [Anthropic Managed Agents](https://www.anthropic.com/engineering/building-effective-agents)
- [Anthropic Sandbox Runtime](https://github.com/anthropic-experimental/sandbox-runtime)
- [Microsoft Agent Governance Toolkit](https://github.com/microsoft/agent-governance-toolkit)
- [Hermes Agent](https://github.com/NousResearch/hermes-agent)
- [OpenClaw](https://github.com/openclaw/openclaw)
- [NVIDIA OpenShell](https://github.com/NVIDIA/OpenShell)

Contributions that improve reliability, security, documentation, and
interoperability are welcome.

## License

AGPL-3.0-only. See [LICENSE.AGPL-3.0](LICENSE.AGPL-3.0).
