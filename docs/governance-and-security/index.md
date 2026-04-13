# 12. Governance and Security

Surogates enforces security at every layer: policy-based tool governance, MCP security scanning, three-component trust isolation, network policies, encrypted credentials, and audit logging.

## Policy Engine

Every tool call passes through a policy check before execution. Policies are evaluated in sub-millisecond time and cannot be bypassed by the agent.

### Allow-List Mode

By default, only explicitly allowed tools can be called. The allow-list is configured per org:

```yaml
# Platform-level defaults (mounted as /etc/surogates/policies/)
tools:
  allow:
    - terminal
    - read_file
    - write_file
    - patch
    - search_files
    - list_files
    - web_search
    - memory
    - skills_list
    - skill_view
```

### Attribute-Based Access Control (ABAC)

Fine-grained rules based on attributes allow precise control:

- "Allow `refund_user` only if `user_status == verified` AND `amount < 1000`"
- "Allow `terminal` only if `command` does not match `rm -rf`"
- "Allow `web_search` only during business hours"

ABAC rules can reference tool arguments, user attributes (role, group membership), session attributes (channel, model), and time-based conditions.

### Policy Immutability

Policies are **frozen at session start**. The agent cannot modify its own policy during execution. This prevents prompt injection attacks that attempt to weaken governance mid-session.

## Trust Boundaries

Surogates enforces a three-component isolation model:

| Component | Trust Level | Access |
|---|---|---|
| **API Server** | Trusted | Full database, all storage buckets, JWT issuance |
| **Worker** | Trusted | Database + Redis for session state; tenant operations go through API server |
| **Sandbox** | Untrusted | Only the current session's workspace bucket; no database, no API, no tenant storage |

The structural fix for prompt injection: credentials and tenant data are never reachable from the sandbox where the LLM's generated code runs.

## Sandbox Network Isolation

Kubernetes NetworkPolicy restricts sandbox pod egress:

| Allowed | Denied |
|---|---|
| MCP proxy (for external tool calls with credential injection) | Internet |
| Garage S3 API (for workspace file I/O) | Database (PostgreSQL) |
| | Redis |
| | API server |
| | Other sandbox pods |

This prevents data exfiltration from the sandbox, even if the LLM is compromised.

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
