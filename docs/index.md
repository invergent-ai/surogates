# Surogates Documentation

Surogates is an open platform for managed agents at scale. It runs long-horizon AI agents on your behalf through a small set of interfaces meant to outlast any particular implementation.

Built on Kubernetes, Surogates implements the [Managed Agents architecture](https://www.anthropic.com/engineering/building-effective-agents) (Anthropic) with production-hardened tool implementations, MCP integration, and a multi-tenant, channel-first design.

---

## Table of Contents

### [1. Introduction](intro/index.md)
- What is Surogates?
- Architecture overview (the three-component model: API / Worker / Sandbox)
- Key concepts (sessions, events, channels, harness, sandbox, tenants)
- Design philosophy (channels-only, K8s-native, file-shaped assets)

### [2. Getting Started](getting-started/index.md)
- Prerequisites (K8s cluster, PostgreSQL, Redis, Garage)
- Kubernetes cluster quickstart
- Your first session (web chat UI walkthrough)

### [3. Architecture](architecture/index.md)
- System components diagram
- Data flow: message in -> LLM loop -> response out
- Event-driven design (the append-only event log)
- Trust boundaries (API server / Worker / Sandbox isolation)
- Storage architecture (Garage buckets, tenant assets, workspace files)

### [4. Deployment](deployment/index.md)
- Kubernetes manifests (namespace, deployments, services, ingress)
- API server deployment and configuration
- Worker deployment and HPA scaling
- Sandbox pods (K8sSandbox lifecycle, s3fs-fuse sidecar, activeDeadlineSeconds)
- Channel adapters (per-channel deployment)
- MCP proxy deployment
- Infrastructure dependencies (PostgreSQL, Redis, Garage)
- Platform volumes (skills, tools, MCP configs, policies)

### [5. Multi-Tenancy](multi-tenancy/index.md)
- Tenant model (orgs, users, channel identities)
- Authentication (database provider)
- Service-account tokens for programmatic access (API channel)
- Per-org provider configuration
- JWT token flow (issuance, refresh, validation)
- Tenant context and credential vault
- Channel identity mapping (linking Slack users to internal accounts)

### [6. Channels](channels/index.md)
- Channel adapter protocol
- [Web](channels/web.md) -- browser chat UI with real-time streaming, session management, workspace browsing
- [Slack](channels/slack.md) -- setup guide, Socket Mode, DMs, @mentions, threading, file attachments, multi-workspace
- [Telegram](channels/telegram.md) -- Bot API, DMs, groups, forum topics, media handling
- [API](channels/api.md) -- fire-and-forget programmatic channel for synthetic-data pipelines and batch jobs
- Session routing and response delivery (durable outbox, Redis nudges)

### [7. Tools](tools/index.md)
- Tool overview and registry
- Tool routing (harness-local vs. sandbox vs. MCP proxy)
- Builtin tools reference
- Tool argument coercion

### [8. Skills](skills/index.md)
- What is a skill? (prompt-based reusable behaviors)
- `SKILL.md` format and frontmatter
- 3-layer loading (platform > org > user)
- Skill CRUD via API
- Skill validation rules

### [9. Sub-Agents](sub-agents/index.md)
- What is a sub-agent? (declarative agent type as a child session)
- Sub-agents vs. skills vs. experts (when to use which)
- `AGENT.md` format and frontmatter
- 4-layer loading (platform > user FS > org DB > user DB)
- Spawning with `spawn_worker` / `delegate_task` and `agent_type`
- Governance policy profile composition (narrowing semantics)
- "Running" panel + session tree endpoints
- Web UI library page and REST API reference

### [10. Experts](experts/index.md)
- What is an expert? (task-specialized model as a skill)
- Define an expert (`SKILL.md` with type: expert)
- Collect training data from the event log
- Train the expert externally (fine-tuning, adapters, prompt/config updates)
- Activate, verify, monitor, and retrain
- Mini agent loop (scoped tools, bounded iterations)
- Feedback tracking and auto-disable
- API reference

### [11. Memory](memory/index.md)
- Memory system overview
- `MEMORY.md` / `USER.md` format (section-delimited entries)
- MemoryStore (frozen snapshots, security scanning, dedup)
- MemoryProvider lifecycle hooks
- MemoryManager (builtin + external providers, prefetch fencing)
- API-mediated access (worker -> API server -> tenant bucket)

### [12. MCP Integration](mcp-integration/index.md)
- MCP client (stdio + HTTP transport)
- Auto-reconnect and sampling
- OAuth 2.1 PKCE for MCP servers
- [MCP Proxy](mcp-integration/proxy.md) -- credential injection, setup, network isolation, troubleshooting
- MCP server configuration (platform + org + user layers)
- MCP security scanning (poisoning, rug-pull, invisible unicode)

### [13. Governance and Security](governance-and-security/index.md)
- Policy engine (AGT PolicyEngine, allow-list, ABAC)
- MCP security scanner (tool poisoning, SHA-256 fingerprinting)
- Policy immutability (freeze per session)
- Trust boundaries (3-component isolation model)
- Sandbox network isolation (NetworkPolicy)
- Credential vault (encrypted at rest, per-org/per-user)
- Saga (multi-step tool chains with automatic rollback)
- Rate limiting (per-org, per-user, sliding window)

### [13a. Audit &amp; Observability](audit/index.md)
- [Session event log](audit/events.md) -- every turn, tool call, policy decision, saga step inside a session
- [Tenant audit log](audit/audit_log.md) -- auth, MCP scan, credential access (events with no session)
- [SQL views](audit/views.md) -- typed projections for dashboards, audit queries, training data
- Denormalization trigger, trace correlation, non-blocking emission, stability policy

### [14. Storage](storage/index.md)
- StorageBackend protocol (`LocalBackend` / `S3Backend`)
- Tenant asset roots (bucket layout, directory conventions)
- Session prefixes (lifecycle, s3fs-fuse mount)
- Bucket security model (session-scoped vs. tenant-wide)

### [15. Background Jobs](background-jobs/index.md)
- `cleanup_sessions` -- orphaned session prefix sweep
- `training_collector` -- expert training data export

### [16. Operations](operations/index.md)
- Health checks and metrics
- HPA configuration and scaling strategy
- Monitoring active sessions
- Troubleshooting (crash recovery, lease expiry, delivery retries)
- Database schema and migrations (Alembic)

### Appendices
- [A. Configuration Reference](appendices/configuration.md) -- all YAML keys and env vars
- [B. REST API Reference](appendices/api-reference.md) -- all endpoints, request/response formats
- [C. Glossary](appendices/glossary.md) -- session, harness, sandbox, channel, tenant, expert, skill, event, lease, cursor
