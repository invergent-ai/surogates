# 14. Background Jobs

Surogates includes two background jobs that run as Kubernetes CronJobs.

## `cleanup_sessions` -- Orphaned Bucket Sweep

Over time, session workspace buckets can become orphaned -- the session is deleted or completed, but the bucket persists due to a crash or race condition during cleanup.

### What It Does

```
1. List all buckets with the "session-" prefix in Garage
2. Query the database for all active/paused session IDs
3. Compare: find buckets with no matching active session
4. Delete orphaned buckets (and all their contents)
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

## `training_collector` -- Expert Training Data Export

The training collector extracts successful conversation trajectories from the event log and writes them as JSONL files to the tenant's Garage bucket.

### What It Does

```
1. Scan completed sessions that involved expert delegation
2. Identify successful trajectories:
   - expert.delegation -> expert.result (no subsequent expert.override)
   - Task description, tool calls, tool results, final response
3. Format as OpenAI fine-tuning compatible JSONL
4. Write to tenant-{org_id}/shared/skills/{expert}/training/
```

### Usage

```bash
# Export training data for a specific expert
uv run python -m surogates.jobs.training_collector --expert-id <uuid>

# Export since a specific date
uv run python -m surogates.jobs.training_collector --expert-id <uuid> --since 2025-01-01
```

### Important Boundary

The platform's responsibility ends at the JSONL file. Training, fine-tuning, evaluation, and model hosting are the organization's concern. The platform exports data; the org trains the model; the org registers the resulting endpoint back in the expert's `SKILL.md`.

See [Experts -- Training Data Export](../experts/index.md#training-data-export) for details on the export format.
