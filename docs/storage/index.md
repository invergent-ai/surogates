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

Each agent gets one bucket. Each session gets an ephemeral path
inside that bucket:

```
{configured-agent-bucket}/
  sessions/{session_id}/
  (workspace files -- whatever the agent creates or modifies)
```

### Lifecycle

1. **Session created** -- API server ensures the configured agent bucket exists.
2. **First sandbox tool call** -- sandbox pod is provisioned with `sessions/{session_id}/` FUSE-mounted as `/workspace`.
3. **Agent works** -- reads and writes files at `/workspace`. All changes are immediately durable in Garage.
4. **Session ends** -- sandbox pod is destroyed, and the session prefix is deleted.

If the sandbox pod dies, a new pod mounts the same path and the workspace is intact.

### Workspace Modes

**S3-backed (default)**: The session path is FUSE-mounted as `/workspace`. Writes are immediately durable. Survives pod restarts.

**Git-cloned**: A repository is cloned during sandbox provisioning. The clone token is used once and not stored in the sandbox. Changes can be pushed back.

## Security

- **Tenant buckets** (`tenant-{org_id}`) are accessible only by the API server.
- **Agent buckets** store session paths under `sessions/{session_id}/`.
- Sandboxes cannot access other sessions' paths or tenant storage.
- Even if the LLM is compromised, the sandbox can only access the current session's workspace files.

### Cleanup

A background CronJob (`cleanup_sessions`) sweeps orphaned `sessions/{session_id}/` prefixes that no longer have a corresponding active session. This is a safety net for cases where the normal cleanup path fails.
