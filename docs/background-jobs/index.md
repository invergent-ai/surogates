# 15. Background Jobs

Surogates runs one Kubernetes CronJob (`platform_cleanup`) and a set of
sweepers hosted inside the runtime processes: `board_maintenance`,
`browser_reaper` and `inbox_expire` tick on the worker, and scheduled
sessions tick on the platform ticker. `training_collector` is a manual
export, run on demand.

## `platform_cleanup` -- Leftover Workspace Sweep

Deleting a session archives its row and deletes its workspace prefix. A prefix outlives that when the delete fails part-way or a worker dies mid-cleanup, and it is then unreachable: no session lists it, and nothing else would ever remove it.

### What It Does

```
1. List the workspace bucket and take each key's first path segment
2. Keep the segments that parse as a session UUID (so boundaries/… and
   anything else in the bucket are skipped)
3. Ask the database which of those sessions still claim their workspace
   -- a row that exists and is not archived
4. Delete the prefixes left over, one failure at a time
```

A session the user still has keeps its files however old it is, and however long ago it finished. Only the archived ones -- the tombstone the delete route writes -- and prefixes with no row at all are swept.

### Usage

```bash
# Run manually
uv run python -m surogates.jobs.platform_cleanup
```

### Kubernetes CronJob

```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: runtime-cleanup
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
            command: ["python", "-m", "surogates.jobs.platform_cleanup"]
          restartPolicy: OnFailure
```

The job needs the same config the runtime uses: the database URL and the storage credentials for the workspace bucket. It is idempotent -- a second run finds nothing left to delete.

## Scheduled Sessions

Scheduled sessions are user-owned recurring prompts stored in PostgreSQL.
Each agent worker ticks schedules for its own `agent_id`, claims due rows
with `FOR UPDATE SKIP LOCKED`, creates a fresh `channel="scheduled"`
session, emits the stored prompt as `user.message`, and enqueues that
session on the agent's Redis work queue.

Users can create short-lived loops with `/loop [interval] <prompt>`.
Fixed-interval loops expire after 3 days; a run can end the schedule early
by calling `loop_complete` when the prompt's stop condition is met. When
the interval is omitted, Surogates creates a dynamic loop: each run chooses
its next delay with `loop_wait`, clamped to 1 minute through 1 hour, and
the loop expires after 7 days; passing `completed: true` to `loop_wait`
ends the schedule early. Use `/loop list` and `/loop cancel <id>` to
manage them. See [Commands](../commands/index.md) for the full slash-command
reference.

### Run result delivery

A scheduled run executes on its own `channel="scheduled"` child session, so
each run's output is surfaced back to the conversation that created the loop:

- **Channel-origin loops** (Slack, Telegram, Teams): a run's deliverable
  events resolve to the **parent** session's channel and routing config, so
  every run is posted back into the origin channel. Without this resolution a
  run strands under the `scheduled` channel, which no delivery loop drains.
- **Web / API-origin loops**: a run's final answer is emitted as a
  `loop.result` event on the **parent** session and appears inline in the
  originating conversation — web clients receive it over the session SSE
  stream, API clients by polling `GET /sessions/{id}/events`. For these runs
  the otherwise-redundant `inbox.task_complete` card is suppressed. A
  `loop.result` is never replayed into the parent agent's context and never
  wakes (re-enqueues) the parent; a run with no text output emits nothing.

### Expiry reaping

Because the claim query skips rows whose `expires_at` has passed, a loop that
expires in the gap between its last run and its next due instant would
otherwise remain `active` forever — due, but never firing. The platform
ticker's periodic recovery pass sweeps any `active` schedule past its
`expires_at` (across all tenants) and transitions it to `completed`, so an
expired loop never lingers as a non-firing `active` row.

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
