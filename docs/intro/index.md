# 1. Introduction

## What is Surogates?

Surogates is a multi-tenant, Kubernetes-native platform for running managed AI agents at scale. It provides the infrastructure for long-horizon agents to operate on behalf of users through channels -- a custom web chat UI, Slack, Telegram, and a programmatic API channel for non-interactive workloads (synthetic-data pipelines, batch jobs).

The platform targets enterprise deployments of thousands of users, combining:

- **Managed Agents architecture** (Anthropic) -- decoupled session, harness, and sandbox components with crash recovery and durable state.
- **Policy enforcement and MCP security scanning** -- every tool call passes through governance before execution, with attribute-based access control.
- **Saga-based rollback** -- multi-step tool chains are tracked automatically. If a step fails, completed steps are compensated in reverse order, restoring the workspace to a consistent state.

Surogates is not a CLI tool or a library. It is a hosted service that runs inside your Kubernetes cluster and exposes agents exclusively through **channels** -- authenticated interfaces that users interact with from their browser or messaging platform.

## Architecture Overview

Surogates follows the three-component model described in Anthropic's [Building Effective Agents](https://www.anthropic.com/engineering/building-effective-agents) and the [Managed Agents](https://www.anthropic.com/engineering/managed-agents-for-the-long-term) architecture:

```
API Server (control plane)  -->  Worker (brain)  -->  Sandbox (hands)
     trusted                      trusted              untrusted
     tenant-wide S3 + DB          DB + Redis           session-scoped S3 only
```

**API Server** -- the control plane. Serves the REST API and web chat UI. Manages sessions, exposes skills/memory/workspace endpoints. Has full database and tenant storage access. The only component the frontend talks to.

**Worker** -- the brain. Runs the `AgentHarness` -- the core loop that calls the LLM, routes tool calls, and emits events. Stateless and horizontally scalable. Picks sessions from a Redis work queue. Calls the API server for tenant-level operations (skills, memory). Creates and manages sandbox instances.

**Sandbox** -- the hands. An ephemeral execution environment for untrusted tool commands (terminal, file I/O, code execution). One sandbox per session, lazily provisioned on first use. The sandbox is cattle -- if it dies, the harness catches the error and provisions a new one.

Each component can fail or be replaced independently. The session log (an append-only event stream in PostgreSQL) sits outside all three, ensuring nothing is lost on crash.

## Key Concepts

| Concept | Description |
|---|---|
| **Session** | A conversation between a user and an agent. Backed by an append-only event log in PostgreSQL. Sessions survive crashes -- any worker can resume from the last event. |
| **Event** | An immutable record in the session log. Events capture every interaction: user messages, LLM responses, tool calls, tool results, sandbox operations, lifecycle transitions. |
| **Channel** | The user-facing interface. Surogates has no CLI. Users interact through the web chat UI or Slack. Each channel has an adapter that normalizes platform-specific messages into the internal API. |
| **Harness** | The agent loop that runs inside a worker. It replays events, calls the LLM, dispatches tool calls, and emits new events. The harness is stateless -- all state lives in the session log. |
| **Sandbox** | An isolated execution environment where the LLM's generated code runs. In development, this is a subprocess in a temp directory. In production, it is a dedicated K8s pod with an s3fs-fuse workspace mount. |
| **Tenant** | An organization (org) and its users. Each org has its own skills, memory, MCP servers, credentials, and policies. Tenant isolation is enforced at every layer. |
| **Skill** | A reusable prompt-based behavior defined in a `SKILL.md` file. Skills are loaded from three layers (platform > org > user) with last-wins precedence. |
| **Expert** | A skill backed by a fine-tuned small language model (SLM) instead of a prompt template. Experts run scoped mini-loops with restricted tool access. |
| **Tool** | A capability the agent can invoke. Tools are either harness-local (memory, web search, skills) or sandbox-bound (terminal, file operations, code execution). Every tool call passes through governance before execution. |
| **Saga** | A tracked sequence of tool calls with automatic rollback. When a step fails, previously completed steps are compensated in reverse order -- builtin tools via filesystem checkpoints, MCP tools via declared undo operations. |
| **Lease** | An exclusive lock on a session. Only one worker can run a session's harness at a time. Leases have TTLs and are renewed during processing. If a worker crashes, the lease expires and another worker picks up. |
| **Cursor** | The last fully-processed event ID for a session. On crash recovery, the new worker replays events after the cursor. |

## Design Philosophy

### Channels-Only

There is no CLI, no TUI, no local agent. All interaction happens through channels. The web chat UI is a browser-based SPA that talks to the REST API. Slack and Telegram use adapter processes that normalize platform events into the internal API. The [API channel](../channels/api.md) is the programmatic equivalent for non-interactive clients -- synthetic-data pipelines submit prompts via `POST /v1/api/prompts` with a service-account token and read results back from the database. Channels are first-class, not an afterthought.

### Kubernetes-Native

Surogates runs on Kubernetes from the start. All components (API server, workers, sandbox pods, channel adapters, MCP proxy) are K8s workloads. Infrastructure dependencies (PostgreSQL, Redis, Garage) run as StatefulSets or managed services. There is no docker-compose development mode -- use a local K8s cluster (kind, k3s, minikube).

### File-Shaped Assets

Assets like `MEMORY.md`, `USER.md`, skill directories, and MCP config stay file-shaped -- stored in S3-compatible buckets, not in database tables. PostgreSQL is for tenancy, orchestration, and auditability. File-shaped assets stay file-shaped until there is a concrete reason to relationalize them.

### Decouple the Brain from the Hands

The core architectural insight from Anthropic's Managed Agents: separate what the agent thinks (the harness/brain) from what it does (the sandbox/hands) and what it remembers (the session log). Each can fail, scale, and evolve independently. The harness calls the sandbox the way it calls any other tool: `execute(name, input) -> string`. The sandbox is cattle. The session log is the source of truth.
