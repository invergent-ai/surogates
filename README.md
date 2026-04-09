# Surogates

An open platform for running managed agents on Kubernetes.

Surogates decouples the components of an agent — session, harness, and sandbox — so each can fail, scale, and be replaced independently. The session is a durable append-only event log. The harness (brain) is a stateless worker that drives the LLM loop. The sandbox (hands) is an isolated execution environment reached via tool calls. No component assumes anything about the others beyond a small set of interfaces.

## Why

The two most prominent open agent projects occupy opposite ends of a spectrum, and neither covers the middle ground that enterprise teams actually need.

**OpenClaw** (is a personal AI assistant that runs locally and connects an LLM to 100+ skills across apps, browsers, and messaging platforms. It optimizes for breadth of integrations and ease of setup. But it is a single-process, single-user tool — there is no session durability, no multi-tenancy, no Kubernetes-native deployment, and no governance layer. If the process dies, the session is gone. Security is a known concern: the agent runs with broad permissions in the same environment as user data, and Nvidia built NemoClaw specifically to address the enterprise security gap.

**Hermes Agent** (NousResearch) is a self-improving agent framework with a genuine agent loop, 40+ built-in tools, MCP support, a skill system, memory with full-text search, and platform adapters for Telegram, Slack, Discord, and more. But it is a single-user, single-process system — state lives in a local SQLite database, there is no multi-tenancy, no credential isolation, no orchestration layer, and no crash recovery beyond restarting the process. It runs on a $5 VPS, not a fleet.

Surogates fills the gap. It takes the operational pattern from Anthropic's Managed Agents architecture — decoupled session, harness, and sandbox — and makes it an open, self-hosted Kubernetes-native platform with what neither project provides:

- **Session durability** — an append-only event log in PostgreSQL. The harness crashes? A new one calls `wake(session_id)`, replays events, and picks up where it left off. The sandbox dies? The harness gets a tool-call error and provisions a new one.
- **Multi-tenancy** — orgs, users, per-tenant credentials vault, pluggable auth.
- **Security boundary** — credentials never enter the sandbox. LLM-generated code cannot reach tokens.
- **Governance** — policy enforcement and MCP tool poisoning defense at the platform level.
- **Model-agnostic** — any OpenAI-compatible provider or native Anthropic. Swap models without changing application code.

## Architecture

```
Channel Adapters (Web Chat, Slack, Teams, Telegram)
        │
   API Gateway (FastAPI, JWT, tenant routing)
        │
   Orchestrator (Redis queue, wake/retry)
        │
   Harness Workers (stateless, any can serve any session)
        │
   Tool Router (harness-local │ sandbox │ MCP proxy)
        │
   Session Store (PostgreSQL append-only event log)
```

## Key Design Decisions

- **Channels only** — no CLI/TUI. Users interact through a web chat UI or messaging platforms (Slack, Teams, Telegram).
- **Kubernetes-native** — workers, sandboxes, and adapters are all K8s workloads. Sandbox pods are provisioned on demand.
- **Multi-tenant** — orgs, users, per-tenant credentials vault, pluggable auth (database, LDAP, OIDC).
- **OpenAI-compatible LLM providers** — swap models without changing application code. Native Anthropic path retained where needed.
- **Governance** — Microsoft Agent Governance Toolkit for tool-call policy enforcement and MCP tool poisoning defense.


## Status

Early development. 