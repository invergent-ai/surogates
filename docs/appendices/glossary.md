# Appendix E: Glossary

| Term | Definition |
|---|---|
| **ABAC** | Attribute-Based Access Control. Policy rules that evaluate attributes of the user, session, tool arguments, or environment to make access decisions. Example: "allow `refund_user` only if `amount < 1000`". |
| **AGT** | Agent Governance Toolkit. Microsoft's open-source library for agent policy enforcement, MCP security scanning, and capability modeling. Surogates uses AGT's `PolicyEngine`, `MCPSecurityScanner`, and `CapabilityModel`. |
| **API Channel** | The programmatic channel. Non-interactive clients (synthetic-data pipelines, batch jobs) submit prompts via `POST /v1/api/prompts` with a service-account token. Sessions have `channel="api"` and no user identity; results are read directly from the `events` table. |
| **Channel** | The user-facing interface. Surogates has no CLI. Users interact through channels: web, Slack, Telegram, and the API channel for programmatic clients. Each has an adapter (or, for web/API, a REST endpoint set) that normalizes inbound messages into the internal API. |
| **Channel Identity** | A mapping between a platform-specific user ID (e.g., Slack user `U03ABCDEF`) and an internal Surogates user. Enables cross-channel session sharing. |
| **Cursor** | The last fully-processed event ID for a session. Used for crash recovery -- the new worker replays events after the cursor. Also used by SSE clients to resume event streams without data loss. |
| **Delivery Outbox** | A PostgreSQL table that acts as a durable queue for outbound messages. Channel adapters claim rows, send messages, and mark them as delivered. Redis nudges are a latency optimization, not the source of truth. |
| **Event** | An immutable record in the session's append-only log. Events capture every interaction: user messages, LLM responses, tool calls, tool results, sandbox operations, governance decisions, lifecycle transitions. |
| **Expert** | A skill backed by a fine-tuned small language model (SLM) instead of a prompt template. Experts run scoped mini-loops with restricted tool access. The base LLM explicitly delegates to experts via the `consult_expert` tool. |
| **Garage** | A lightweight, S3-compatible object storage system used by Surogates for workspace files and tenant assets. One bucket per session (workspace), one bucket per tenant (skills, memory, MCP configs). |
| **GovernanceGate** | The policy enforcement point. Wraps AGT's `PolicyEngine` to check every tool call before execution. Supports allow-lists, deny-lists, ABAC rules, and file path containment. Policies are frozen at session start. |
| **Harness** | The agent loop that runs inside a worker. It replays events, calls the LLM, dispatches tool calls, and emits new events. The harness is stateless -- all state lives in the session log. Also called "the brain". |
| **Lease** | An exclusive lock on a session. Only one worker can run a session's harness at a time. Leases have TTLs and are renewed during processing. If a worker crashes, the lease expires and another worker acquires it. |
| **MCP** | Model Context Protocol. An open standard for connecting LLMs to external tools and data sources. Surogates includes a full MCP client (stdio + HTTP), OAuth 2.1 PKCE support, and a credential-injecting proxy. |
| **MCP Proxy** | A trusted service that sits between sandboxes and external MCP servers. It injects credentials from the vault so that the sandbox never sees secrets. |
| **Orchestrator** | The Redis queue consumer that pulls session IDs and dispatches them to available harness instances. Handles retries with exponential backoff. |
| **Org** | Organization. The top-level tenant boundary. Each org has its own users, skills, memory, credentials, MCP servers, and policies. |
| **Saga** | A tracked sequence of tool calls with automatic rollback. When a step fails, previously completed steps are compensated in reverse order -- builtin tools via filesystem checkpoints, MCP tools via declared undo operations. Named after the [saga pattern](https://microservices.io/patterns/data/saga.html) from distributed systems. |
| **Sandbox** | An isolated execution environment where the LLM's generated code runs. In development: a subprocess in a temp directory. In production: a dedicated K8s pod with s3fs-fuse workspace mount. Also called "the hands". |
| **Service Account** | An org-scoped principal used by non-interactive clients to authenticate against the API channel. Issued by an admin via `POST /v1/admin/service-accounts`; produces a long-lived `surg_sk_...` bearer token that is accepted only on `/v1/api/*` routes and carries no user identity. |
| **Session** | A conversation between a user and an agent. Backed by an append-only event log in PostgreSQL. Sessions survive crashes -- any worker can resume from the last event. |
| **Session Source** | Metadata about where a message came from: platform, chat ID, chat type, user ID, thread ID. Used to route messages to the correct session. |
| **Skill** | A reusable, prompt-based behavior defined in a `SKILL.md` file. Skills are loaded from three layers (platform > org > user) with last-wins precedence. |
| **SLM** | Small Language Model. A smaller, task-specific model (typically 1-13B parameters) used as an expert. Fine-tuned on successful conversation trajectories from the platform. |
| **SSE** | Server-Sent Events. The protocol used by the web chat UI to receive real-time events from the API server. The browser subscribes to `/v1/sessions/{id}/events` and receives events as they are emitted. |
| **StorageBackend** | The abstraction for S3-compatible object storage. Two implementations: `LocalBackend` (filesystem, for development) and `S3Backend` (Garage/S3, for production). |
| **Tenant** | An organization and its users. Tenant isolation is enforced at every layer: database queries, storage access, API routing, and policy enforcement. |
| **TenantContext** | Runtime context that flows through every request via Python's `contextvars`. Contains org_id, user_id, permissions, and asset root. Set by the auth middleware, accessible anywhere via `get_tenant()`. |
| **Tool** | A capability the agent can invoke. Tools are either harness-local (memory, web search, skills), sandbox-bound (terminal, file operations), or MCP-proxied (external services). |
| **ToolRouter** | Determines where each tool runs (harness vs. sandbox vs. MCP proxy) and dispatches accordingly. Governance is checked before dispatch. |
| **Worker** | A long-running pod that picks sessions from the Redis queue and runs the agent harness. Workers are stateless and horizontally scalable. Also called "the brain" (the pod that hosts the harness). |
