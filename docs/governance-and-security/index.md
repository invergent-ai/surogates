# 13. Governance and Security

Surogates enforces security at every layer: policy-based tool governance, MCP security scanning, three-component trust isolation, network policies, encrypted credentials, and audit logging.

## Policy Engine

Every tool call passes through the per-wake `GovernanceGate` before
execution. The gate composes two layers:

- **Platform floor** (always on, not configurable): workspace-sandbox
  path containment for file/terminal tools, shell-variable path
  hygiene, and argument-level checks. No agent configuration can relax
  the floor.
- **Agent policy** (configured in Studio, projected through the
  runtime config's `governance` blob): `allowed_tools` /
  `denied_tools` over the built-in tool catalog, plus network egress
  rules for the web/browser tools.

Composition is narrowing-only: allow-lists intersect, deny-lists
union, the stricter egress default wins. Denials are returned to the
LLM as tool results and recorded as `policy.denied` events (visible in
the session's Policies tab); `governance.log_allowed` additionally
records every pass.

The allow-list governs the **built-in** tool catalog. MCP tools
(`mcp__*`) are governed by their own plane — per-agent MCP server
attachment enforced by the MCP proxy, plus package entitlements — so a
built-in allow-list does not reject them.

### AI disclosure (EU AI Act Art. 50)

Per-agent disclosure is part of the agent policy
(`policy.transparency`, levels `none|basic|enhanced|full`):

- the public `GET /v1/transparency` endpoint serves the per-agent
  config (query param or Host-subdomain resolution) with a
  deployment-settings fallback, and the web SPA renders the banner
  from it;
- adapter channels (Slack, Telegram, WhatsApp) deliver the disclosure
  text as the first message of every new conversation, recorded as a
  `disclosure.presented` event. Delivery rides each platform's
  `post_input_nudge`; a platform without one (e.g. the Teams stub)
  silently cannot disclose, so do not enable such a channel for a
  disclosure-required agent;
- the web banner's accept action persists a `disclosure.confirmed`
  event on the session log, giving deployers per-session evidence.

### Policy Immutability

The composed gate is **frozen** for the wake. The agent cannot modify
its own policy during execution, which blocks prompt-injection
attempts to weaken governance mid-session. Policy edits in Studio
reach live sessions on their next wake via the runtime-config
invalidation channel.

## Trust Boundaries

Surogates enforces a three-component isolation model:

| Component | Trust Level | Access |
|---|---|---|
| **API Server** | Trusted | Full database, all storage buckets, JWT issuance |
| **Worker** | Trusted | Database + Redis for session state; tenant operations go through API server |
| **Sandbox** | Untrusted | Only the current session's path in the agent bucket; no database, no API, no tenant storage |

The structural fix for prompt injection: credentials and tenant data are never reachable from the sandbox where the LLM's generated code runs.

## Network controls

There is no single network switch. Three independent planes govern
outbound traffic, and an operator needs all three to reason about what
an agent can reach:

1. **Agent web/browser egress** (Studio policy): domain/port allow/deny
   rules enforced by the governance gate on the URL arguments of
   `web_search`, `web_extract`, `web_crawl` and `browser_navigate`.
   `default_action: deny` with no allow rules blocks those tools'
   requests. It governs nothing else — in particular it does **not**
   stop `terminal` from running `curl`, nor MCP calls, nor coding
   agents. Restrict those by denying the tool or detaching the MCP
   server.
2. **Terminal sandbox allowlist**: the terminal tool writes its own
   sandbox-runtime settings with a fixed domain allowlist (package
   registries, GitHub, coding-agent vendor APIs) plus any configured
   SSH hosts. It is independent of the agent policy; the only policy
   lever over it is denying `terminal` (or `run_coding_agent`).
3. **MCP server attachment**: tool discovery and calls are scoped to
   the servers attached to the agent, enforced by the MCP proxy. An
   agent with no attached servers can reach no MCP endpoint.

Additionally, sessions with configured SSH targets get a per-session
Kubernetes NetworkPolicy pinning egress to the resolved target IPs;
targets without a pinned host key fail closed.

A blanket sandbox-egress NetworkPolicy (deny internet/DB/Redis/API
from sandbox pods) is a deployment concern: this repository ships an
ingress policy for the sandbox executor, and the cluster deployment is
responsible for the egress rules appropriate to its environment.

## Credential Vault

Sensitive values are stored encrypted at rest and never exposed to sandboxes:

- **Encryption**: Fernet (AES-128-CBC + HMAC-SHA256)
- **Scope**: Org-wide (shared) or user-specific
- **Access**: Only the API server and MCP proxy read the vault. The MCP proxy fetches credentials and injects them into outbound requests.
- **Git auth**: Clone tokens are used once during sandbox provisioning and never stored in the sandbox.

## MCP Security Scanning

Every MCP tool definition is scanned before registration:

| Threat | What It Catches |
|---|---|
| **Invisible unicode** | Zero-width chars, bidi marks in tool names/descriptions |
| **Prompt injection** | Deceptive descriptions that trick the LLM |
| **Hidden HTML** | HTML comments with invisible instructions |
| **Tool poisoning** | Descriptions that manipulate the LLM into dangerous behavior |
| **Rug-pull detection** | Tool definitions that change between connections (SHA-256 fingerprinting) |

Tools that fail scanning are not registered. Scan results are logged for audit purposes.

## Saga: Multi-Step Tool Chains with Automatic Rollback

When the agent performs a sequence of tool calls that modify state (write files, run commands, call external APIs), a failure partway through can leave things in a broken state. The saga system tracks these multi-step operations and automatically rolls back completed steps if a later step fails.

### How It Works

```
Agent writes 3 files, then runs a command that fails:

  Step 1: write_file("config.yaml")    --> COMMITTED (checkpoint saved)
  Step 2: write_file("main.py")        --> COMMITTED (checkpoint saved)
  Step 3: write_file("test.py")        --> COMMITTED (checkpoint saved)
  Step 4: terminal("python test.py")   --> FAILED

  Automatic compensation (reverse order):
  Step 3: restore checkpoint            --> file reverted
  Step 2: restore checkpoint            --> file reverted
  Step 1: restore checkpoint            --> file reverted
```

### Compensation Strategies

| Tool Type | How It Rolls Back |
|---|---|
| **Builtin tools** (write_file, patch, terminal) | Restores a filesystem checkpoint taken before the tool executed |
| **MCP tools** (external services) | Calls a declared undo tool (e.g., `delete_jira_ticket` to undo `create_jira_ticket`) |

### Behavior

- **Sequential execution**: When saga is active, tool calls are executed one at a time (no parallelization) to ensure deterministic ordering for rollback.
- **Read-only tools are excluded**: Tools like `read_file`, `search_files`, `web_search`, and `skills_list` have no side effects and are not tracked by the saga.
- **Crash recovery**: Saga state is reconstructed from the event log on harness restart. If the worker crashes mid-saga, the new worker can resume or compensate.
- **Escalation**: If a compensation step itself fails (e.g., the undo tool errors), the saga enters an `escalated` state. An operator must intervene manually.

### Configuration

```yaml
saga:
  enabled: false              # disabled by default (opt-in)
  default_step_timeout: 300   # max seconds per tool call
  default_max_retries: 2      # retries per step before failing
  retry_delay: 1.0            # initial retry delay (exponential backoff)
```

### Events

The saga lifecycle is fully captured in the event log:

| Event | When |
|---|---|
| `saga.start` | Saga created at session start |
| `saga.step_begin` | Tool call registered and execution begins |
| `saga.step_committed` | Tool call completed successfully |
| `saga.step_failed` | Tool call failed |
| `saga.compensate` | Rollback triggered (with reason and step count) |
| `saga.complete` | Saga finished (normally or after compensation) |

## Audit Trail

The events table IS the audit log. Every action is recorded:

- Every user message, LLM response, and tool call
- Every governance decision (allowed or denied)
- Every sandbox operation
- Every session lifecycle transition
- Every expert delegation and result

No separate audit infrastructure is needed.

## Rate Limiting

Per-org and per-user rate limits are enforced via Redis sliding windows. When limits are exceeded, requests receive a `429 Too Many Requests` response. Limits are configurable per org.
