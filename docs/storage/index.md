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

### Local backend: file permissions

The local backend creates every stored file with mode `0660`, less the process umask. The owner (the runtime's user) and the file's group can read and write the file, and other users get no access. A stored file is never readable or writable by other users, even under umask `0`.

A sandbox that runs as a different user than the runtime cannot read these files unless the operator grants it access. The docker sandbox is one such case: it runs commands as uid 1000 in a session workspace bind-mounted from the local bucket. Grant access in one of two ways:

- **Default ACL (recommended).** Give the sandbox user an ACL on the storage root and a default ACL that new folders and files inherit:

  ```sh
  setfacl -R -m u:1000:rwX <base_path>
  setfacl -R -d -m u:1000:rwX <base_path>
  ```

  A stored file created under this ACL gets an ACL mask of `rw-`. The sandbox user can therefore read the file and change it in place, for example with `>>`, by saving a spreadsheet over an uploaded one, or by writing to an uploaded SQLite database. Under a default ACL the kernel ignores the umask: the ACL decides the new file's permissions, capped by the mode it is created with (`0660`).

  On many hosts uid 1000 is a person's login, and an ACL for uid 1000 gives that person the stored files too. Grant a uid that only the sandbox uses instead. You can confirm that no account on the host has uid 1000. Or you can run Docker with user-namespace remapping (`userns-remap`), which maps the container's uid 1000 to a subordinate host uid, and grant the ACL to that host uid. For example, the host uid is 101000 when the subordinate range starts at 100000.
- **Shared group.** Give the storage root a group that the sandbox user is in (inside the container: the same gid). Set the setgid bit on the root's folders so new files and folders inherit that group. Under the usual umask `022`, a new file is `0640`, so the group can only read it. If the sandbox should also write the files and folders the runtime creates, run the runtime with umask `002`.

The `/tmp/surogates/tenant-assets` base used in the example above is not private on a shared host. Any local user can create folders under `/tmp`, and could create this one first and own it. Outside single-user development, set `storage.base_path` to a folder that only the runtime's user (and the sandbox's user) can reach.

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

### A case's boundary

The work of a Keel case sits in the `case:<case id>` boundary, at `{storage.key_prefix}boundaries/case:<case id>/`:

```
boundaries/case:<case id>/
  workspace/         the case's files, mounted in a step's sandbox
  org.json           the organisation the case is under
  versions/<sha256>  a file as it was when its digest was handed to Keel
```

Only `workspace/` is mounted or listed. `org.json` and `versions/` are for keel-link and the worker alone. A version is kept once per case, and its retention is the case engine's, which asks keel-link to remove it.

A rerun's or a trial's folder, which holds the files Keel's reruns and trials work on, has the same layout, and its record says `"rerun": "<kind>"`. The case engine purges it whole through keel-link: the memory, the sessions and every row that points at them, `workspace/`, `versions/` and, last, `org.json`. A case's folder, recorded without that mark, is never deleted that way.

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

A background CronJob (`platform_cleanup`) sweeps the workspaces of archived sessions that no other session still uses, left behind when a delete failed part-way. A folder with no session row is kept. It is a safety net for a cleanup path that did not finish, not a retention policy — a session the user still has keeps its files.
