# S3 Session Path Storage Design

## Context

Surogates currently supports local and S3-compatible storage through the
`StorageBackend` abstraction. Session workspaces are currently modeled as
per-session buckets, usually named `session-{session_id}`. That shape does
not match the target S3 deployment model: each agent should own one bucket,
and each session should occupy a path within that bucket.

The new S3 workspace shape is:

```text
s3://{agent_bucket}/sessions/{session_id}/
```

There is no legacy support requirement for existing per-session S3 buckets.

## Goals

- Use one S3 bucket per agent for session workspace storage.
- Store each session workspace under the hard-coded path
  `sessions/{session_id}/`.
- Avoid storing a `workspace_prefix` field in session config.
- Keep local storage working for development.
- Make cleanup delete only the session path, not the agent bucket.
- Mount only the session path into Kubernetes sandboxes at `/workspace`.

## Non-Goals

- Migrating or reading old per-session S3 buckets.
- Redesigning tenant storage buckets.
- Introducing first-class persisted storage-location records.
- Changing the visible workspace path inside sandboxes; it remains
  `/workspace`.

## Storage Naming

For S3 storage, the agent bucket is supplied by the agent
deployment config:

```python
agent_session_bucket(settings.storage.bucket) == settings.storage.bucket
```

The helper should validate that the resulting bucket name is S3-compatible and
raise when the configured bucket is empty or invalid. Session keys are derived from the
session id:

```python
session_workspace_prefix(session_id) == f"sessions/{session_id}/"
```

Callers should not persist or hand-compose this prefix. They should use the
helper whenever they need to address session workspace objects.

## Session Config

New S3 sessions store:

```python
config["storage_bucket"] = agent_bucket
config["workspace_path"] = "/workspace"
```

No `workspace_prefix` is written. The session id is already persisted in the
session row, so the prefix is deterministic duplicate state.

For local storage, the workspace directory should mirror the S3 logical shape
under the local base path:

```text
{base_path}/{configured_agent_bucket}/sessions/{session_id}/
```

That keeps development and production layouts aligned while still exposing a
plain filesystem path to local-process sandboxes and tools.

## Backend and API Behavior

Session creation ensures the agent bucket exists. It does not create a
per-session bucket. S3 has no real directory concept, so the session prefix
does not need to be pre-created.

Workspace and artifact APIs must derive S3 object keys by prepending
`sessions/{session_id}/` to user-visible relative workspace paths. For
example:

```text
/workspace/src/app.py -> sessions/{session_id}/src/app.py
```

Deletion and cleanup must list and delete objects under
`sessions/{session_id}/`. They must not delete the agent bucket.

## Kubernetes Sandbox Mount

The sandbox sidecar should mount the session path rather than the bucket root:

```bash
s3fs "{agent_bucket}:/sessions/{session_id}" /workspace ...
```

Inside the sandbox, files still appear directly under `/workspace`. A write to
`/workspace/file.txt` maps to:

```text
s3://{agent_bucket}/sessions/{session_id}/file.txt
```

## Security

The mounted prefix controls what s3fs addresses, but it is not a complete
authorization boundary if the sandbox credentials can access the whole bucket.
For strong isolation, the credentials or bucket policy should restrict the
sandbox to the derived session prefix.

The application should still validate user-supplied workspace paths before
mapping them to storage keys, preserving the existing traversal protections
and reserved-path checks.

## Testing

Unit tests should cover:

- Agent bucket and session prefix helper outputs.
- Session creation config for S3.
- Workspace read/write/list/delete mapping to `sessions/{session_id}/`.
- Artifact storage mapping to `sessions/{session_id}/`.
- Cleanup deleting only keys under a session prefix.
- Kubernetes s3fs mount source using `{agent_bucket}:/sessions/{session_id}`.

Local backend tests should confirm development behavior still works through
the public routes and helper APIs.
