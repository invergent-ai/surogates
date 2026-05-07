# 15. Background Jobs

Surogates includes three background jobs that run as Kubernetes CronJobs.

## `cleanup_sessions` -- Orphaned Session Prefix Sweep

Over time, session prefixes can become orphaned -- the session is deleted or completed, but the `sessions/{session_id}/` path persists due to a crash or race condition during cleanup.

### What It Does

```
1. List all `sessions/{session_id}/` prefixes in the configured agent bucket
2. Query the database for all active/paused session IDs
3. Compare: find prefixes with no matching active session
4. Delete orphaned prefixes and their contents
```

### Usage

```bash
# Run manually
uv run python -m surogates.jobs.cleanup_sessions

# Dry run (report what would be deleted without deleting)
uv run python -m surogates.jobs.cleanup_sessions --dry-run
```

### Kubernetes CronJob

```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: cleanup-sessions
  namespace: surogates
spec:
  schedule: "0 */6 * * *"    # every 6 hours
  jobTemplate:
    spec:
      template:
        spec:
          containers:
          - name: cleanup
            image: ghcr.io/invergent-ai/surogates:latest
            command: ["python", "-m", "surogates.jobs.cleanup_sessions"]
          restartPolicy: OnFailure
```

The job is idempotent -- running it multiple times has no side effects.

## `reset_idle_sessions` -- Idle Session Reset with Memory Flush

Detects sessions that have been inactive beyond a configurable threshold, runs a temporary LLM agent to review the conversation transcript and save important facts to memory, then tears down the sandbox pod. The session itself (events, counters, cursor) is left untouched -- the user can come back and continue at any time.

### What It Does

```
1. Query PostgreSQL for sessions where updated_at exceeds idle threshold
   (and/or crossed a daily boundary, depending on mode)
2. Skip sessions with active harness leases (currently being processed)
3. For each idle session (capped at 200 per run):
   a. Load USER_MESSAGE, LLM_RESPONSE, CONTEXT_COMPACT events
   b. Extract a user/assistant transcript (skip if < 4 messages)
   c. Read current MEMORY.md and USER.md from disk (stale-overwrite guard)
   d. Run a temporary LLM agent with only the memory tool enabled
      - Agent reviews the transcript and saves important facts
      - Max 8 iterations (configurable)
   e. Persist memory files to TenantStorage (S3/Garage) for durability
   f. Interrupt any running harness via Redis pub/sub
   g. Tear down sandbox pod (K8s backend)
   h. Emit SESSION_RESET event
```

### Configuration

See [Appendix A: Configuration Reference](../appendices/configuration.md#session-reset-session_reset) for the full settings table.

In `config.yaml`:

```yaml
session_reset:
  enabled: true
  mode: "idle"              # "daily", "idle", "both", or "none"
  idle_minutes: 1440        # 24 hours of inactivity
  at_hour: 4                # Hour for daily reset (0-23, only for "daily"/"both" mode)
  flush_max_iterations: 8   # Max LLM iterations for the flush agent
```

Or via environment variables:

```bash
SUROGATES_SESSION_RESET_ENABLED=true
SUROGATES_SESSION_RESET_MODE=idle
SUROGATES_SESSION_RESET_IDLE_MINUTES=1440
```

### Usage

```bash
# Run manually
uv run python -m surogates.jobs.reset_idle_sessions

# Dry run (report what would be reset without resetting)
uv run python -m surogates.jobs.reset_idle_sessions --dry-run
```

### Kubernetes CronJob

```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: reset-idle-sessions
  namespace: surogates
spec:
  schedule: "*/5 * * * *"    # every 5 minutes
  concurrencyPolicy: Forbid  # prevent overlapping runs
  jobTemplate:
    spec:
      template:
        spec:
          containers:
          - name: reset
            image: ghcr.io/invergent-ai/surogates:latest
            command: ["python", "-m", "surogates.jobs.reset_idle_sessions"]
            volumeMounts:
            - name: tenant-assets
              mountPath: /data/tenant-assets
          volumes:
          - name: tenant-assets
            persistentVolumeClaim:
              claimName: tenant-assets
          restartPolicy: OnFailure
```

The job requires access to the same `tenant-assets` PersistentVolume as the worker pods (for reading/writing memory files), the database (for querying sessions and events), Redis (for interrupt signals), and an LLM API key (for the flush agent).

### How the Memory Flush Works

The flush is a mini agent loop, not a simple file copy:

1. The conversation transcript (user + assistant messages only) is extracted from the event log
2. A temporary LLM agent receives the transcript plus a structured prompt instructing it to save important facts
3. Current memory state from disk is included in the prompt so the agent avoids overwriting newer entries
4. The agent can call the `memory` tool (add/replace/remove entries in MEMORY.md and USER.md)
5. After the agent finishes, memory files are copied to S3/Garage for durability

## `training_collector` -- Expert Training Data Export

The training collector extracts successful conversation trajectories from the event log and writes them as JSONL files to the tenant's Garage bucket.

The exported trajectories can be used for fine-tuning, LoRA/adapter training, evaluation datasets, or prompt/config updates. The collector does not decide which training method the organization uses.

### What It Does

```
1. Scan completed sessions that involved expert delegation
2. Identify successful trajectories:
   - expert.delegation -> expert.result (no subsequent expert.override)
   - Task description, tool calls, tool results, final response
3. Format as OpenAI fine-tuning compatible JSONL
4. Write to tenant-{org_id}/shared/skills/{expert}/training/
```

Sessions from every channel (web, Slack, Telegram, API) are considered
training candidates.  Synthetic-data pipelines that submit prompts via
`POST /v1/api/prompts` feed successful trajectories back into expert
training exactly like human-driven sessions.

### Usage

```bash
# Export training data for a specific expert
uv run python -m surogates.jobs.training_collector --expert-id <uuid>

# Export since a specific date
uv run python -m surogates.jobs.training_collector --expert-id <uuid> --since 2025-01-01
```

### Important Boundary

The platform's responsibility ends at the JSONL file. Training strategy, fine-tuning, evaluation, prompt/config changes, and model hosting are the organization's concern. The platform exports data; the org trains the expert; the org registers the resulting model, endpoint, and configuration back in the expert's `SKILL.md`.

See [Experts -- Collect Training Data](../experts/index.md#2-collect-training-data) for details on the export format.
