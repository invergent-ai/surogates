# 14. Storage

Surogates uses S3-compatible object storage (Garage) for workspace files, skills, memory, and MCP configurations.

## Storage Backends

| Backend | Use Case | Description |
|---|---|---|
| **Local** | Development | Maps buckets to directories on the local filesystem |
| **S3** | Production | Connects to Garage (or any S3-compatible service) |

**Configuration:**

```yaml
# Development
storage:
  backend: "local"
  base_path: "/tmp/surogates/tenant-assets"

# Production
storage:
  backend: "s3"
  bucket: "agent-bucket-name"
  endpoint: "http://garage.surogates.svc:3900"
  region: "garage"
  access_key: "..."
  secret_key: "..."
```

## Tenant Asset Buckets

Each organization gets a Garage bucket containing asset directories:

```
tenant-{org_id}/
  shared/                         # org-wide resources
    memory/
      MEMORY.md
      USER.md
    skills/
      code_reviewer/
        SKILL.md
      sql_writer/
        SKILL.md
        training/
          dataset_001.jsonl
    mcp/
      servers.json
    tools/
      config.json
  users/{user_id}/                # per-user resources
    memory/
      MEMORY.md
      USER.md
    skills/
      my_custom_skill/
        SKILL.md
    mcp/
      servers.json
```

## Session Workspaces

Session workspaces share one bucket, named by `storage.bucket`. A
session's files sit directly under its own id, with no intervening
segment:

```
{storage.bucket}/
  {storage.key_prefix}{session_id}/
  (workspace files -- whatever the agent creates or modifies)
```

`storage.key_prefix` is stamped into the session's config when the
session is created and is empty in the shared runtime, which makes the
session id the whole of the path. A deployment that serves a single
agent can set it (`{project_id}/{agent_id}`) to slice the bucket per
agent.

Two cases do not get a path of their own. A managed-channel thread
(Slack, Telegram) shares one workspace across its participants at
`{storage.key_prefix}boundaries/{boundary}/workspace/`, and a delegation
child works in its root ancestor's path rather than an empty one of its
own.

### Lifecycle

1. **Session created** -- API server ensures the workspace bucket exists.
2. **First sandbox tool call** -- sandbox pod is provisioned with the session's prefix FUSE-mounted as `/workspace`.
3. **Agent works** -- reads and writes files at `/workspace`. All changes are immediately durable in Garage.
4. **Session ends** -- sandbox pod is destroyed, and the session prefix is deleted.

If the sandbox pod dies, a new pod mounts the same path and the workspace is intact.

### Workspace Modes

**S3-backed (default)**: The session path is FUSE-mounted as `/workspace`. Writes are immediately durable. Survives pod restarts.

**Git-cloned**: A repository is cloned during sandbox provisioning. The clone token is used once and not stored in the sandbox. Changes can be pushed back.

## Security

- **Tenant buckets** (`tenant-{org_id}`) are accessible only by the API server.
- **The workspace bucket** stores each session's files under its own id, and the prefix a sandbox mounts is derived server-side from the session, never from the request that created it.
- Sandboxes cannot access other sessions' paths or tenant storage.
- Even if the LLM is compromised, the sandbox can only access the current session's workspace files.

### Cleanup

A background CronJob (`cleanup_sessions`) sweeps orphaned `sessions/{session_id}/` prefixes that no longer have a corresponding active session. This is a safety net for cases where the normal cleanup path fails.
