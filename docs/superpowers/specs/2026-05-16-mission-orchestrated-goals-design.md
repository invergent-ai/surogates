# `/mission` Orchestrated Goals Design

## Summary

Build `/mission` as a first-class, dashboard-backed orchestration mode.
A mission runs in the current chat session, turns that session into the
coordinator, uses the existing `spawn_task` task layer for workers, evaluates
completion with an LLM rubric judge, and exposes a dedicated
`/missions/:missionId` page focused on mission state, task graph, evidence,
controls, and live worker activity summaries.

`/goal` remains the simple single-session outcome loop. `/mission` is the
durable, multi-worker, monitored objective mode.

## Key Changes

### Persistent Mission State

- Add a `missions` table with:
  - `id` (uuid, PK)
  - `org_id` (uuid, FK orgs, NOT NULL)
  - `user_id` (uuid, FK users, NOT NULL)
  - `session_id` (uuid, FK sessions, NOT NULL) — the coordinator chat session
  - `agent_id` (text, NOT NULL)
  - `description` (text, NOT NULL)
  - `rubric` (text, NOT NULL) — no default; explicit rubric is required (see
    `/mission` builtin command)
  - `status` (text, NOT NULL, DEFAULT `'active'`) — one of:
    `active`, `paused`, `satisfied`, `blocked`, `failed`, `cancelled`,
    `max_iterations_reached`
  - `iteration` (int, NOT NULL, DEFAULT `0`) — count of evaluator calls that
    returned `needs_revision`; bounded by `max_iterations`
  - `max_iterations` (int, NOT NULL, DEFAULT `20`)
  - `last_evaluation_result` (text, NULL) — the verdict from the latest judge
    pass (one of `satisfied | needs_revision | blocked | failed`)
  - `last_evaluation_explanation` (text, NULL)
  - `last_evaluation_feedback` (text, NULL)
  - `last_evaluation_at` (timestamp, NULL) — used by the evaluator
    rate-limit guard (see Mission Evaluator)
  - `paused_reason` (text, NULL)
  - `cancelled_reason` (text, NULL)
  - `created_at`, `updated_at` (timestamptz)
- Indexes:
  - `idx_missions_session` ON `(session_id)` — one active/paused mission per
    session check is a fast lookup
  - `idx_missions_user_agent_status` ON `(org_id, user_id, agent_id, status)` —
    `GET /missions` list
- Add nullable `mission_id` (uuid, FK missions) to `tasks`. Set by `spawn_task`
  when the parent session has an active mission. Add `idx_tasks_mission`
  ON `(mission_id)` for the `GET /missions/:id/tasks` query.
- Store `active_mission_id` in `session.config` for fast command and evaluator
  lookup. The DB row is the source of truth; the config field is a cache that
  is cleared when the mission reaches a terminal state.
- A session may have many historical missions, but only one mission can be
  `active` or `paused` at a time. The constraint is enforced at insertion
  time via a `WHERE status IN ('active','paused')` check, not as a partial
  unique index (we want to allow many historical rows).

### `/mission` Builtin Command

- Reserve `mission` in slash-command parsing so it is never treated as a
  dynamic skill command. Reserve in both the backend command parser and the
  web/SDK autocomplete catalog so the user gets `/mission` suggestions
  rather than a stray skill match.
- Add builtin command handling for:
  - `/mission <description>` — create. Requires a `Rubric:` block; reject
    with a clear error message when missing (no default rubric, unlike
    `/goal` — a measurable success criterion is the whole point).
  - `/mission status`
  - `/mission pause [reason...]` — optional reason captured into
    `paused_reason`
  - `/mission resume`
  - `/mission cancel [reason...]` — optional reason captured into
    `cancelled_reason`; see Mission APIs for the cascade-to-workers
    behaviour
- Parse `Rubric:` blocks the same way `/goal` does. Reject `/mission` create
  if no `Rubric:` block is present.
- On mission creation:
  - insert the mission row with `status='active'`, `iteration=0`
  - set `session.config.coordinator = true`
  - set `session.config.active_mission_id`
  - preload `subagent-task-orchestrator` skill
  - emit `mission.defined` event on the session
  - emit a synthetic kickoff user message (`synthetic="mission_kickoff"`)
    containing the description, the rubric, and an inline reminder of the
    orchestrator pattern
  - enqueue the current session for immediate processing
- Reject mission creation when the session already has an active or paused
  mission. Also reject when the session has an active `/goal` outcome
  (`session.config["outcome"]` present and not terminal) — two evaluator
  loops on the same session is confusing and we forbid it; the user must
  clear/pause one before starting the other. The same check applies in
  reverse to `/goal` so the user can't sneak in either order.

### Mission Evaluator — When It Runs and What It Sees

The mission evaluator reuses `/goal`'s implementation skeleton (strict LLM
rubric judge returning JSON) but with three deliberate changes that
prevent the failure modes a naive "grade the coordinator's prose" loop
would produce.

#### When the evaluator runs

The evaluator does **not** fire on every coordinator no-tool-call response
(that's `/goal`'s rule and it produces too many calls graded over too
little new information). For a mission, fire when **either** of:

1. A task linked to this mission (`tasks.mission_id = mission.id`)
   transitions into a terminal state (`done`, `failed`, `cancelled`). The
   coordinator session has already been woken by the existing
   `WORKER_COMPLETE` / `TASK_FAILED` flow; the evaluator runs after the
   coordinator's next no-tool-call response that follows this wake.
2. The coordinator's latest no-tool-call response contains an explicit
   completion-claim marker. The orchestrator skill teaches the model to
   emit `[[mission-complete]]` on its own line when it believes the
   rubric is satisfied. This gates the "early victory" failure mode —
   the coordinator must explicitly claim completion before the judge
   considers ending the mission outside a task-completion event.

Plus a hard **rate limit**: at most one evaluator call per 30 seconds per
mission. The `last_evaluation_at` column is checked before each call;
calls inside the window are silently skipped and the trigger condition
is re-evaluated on the next wake.

Plus a hard cap: when `mission.iteration >= mission.max_iterations`, the
mission transitions to `max_iterations_reached`. The evaluator stops
firing and a synthetic system message is recorded on the session so the
coordinator (or the user) sees the hit-cap state on the next wake.

#### What the evaluator sees

The evaluator's prompt is built from four blocks:

1. **The rubric** (verbatim from `mission.rubric`).
2. **The coordinator's latest assistant response** (truncated by the same
   cap `/goal` uses).
3. **Completed mission tasks** — for every task with
   `mission_id = mission.id` and `status='done'`, render one line:
   `- T<short-id> (<agent_def_name|"worker">): result=<task.result trunc 400 chars>; metadata=<result_metadata>`.
   Bounded to the most recent 20 by `completed_at`.
4. **In-flight mission tasks** — for every task with `mission_id = mission.id` and
   `status IN ('todo','ready','running','blocked')`, render one line:
   `- T<short-id> (<agent_def_name|"worker">): status=<status>; attempts=<n>`.
   Bounded to the most recent 20 by `created_at`.

This is the keystone: the judge grades the *actual workstream state*,
not the coordinator's prose alone. For criteria of the form "score >=
X", the judge looks for the latest verifier task's
`result_metadata.score` in block (3); for fuzzier rubrics ("synthesizer
produced a coherent recommendation") it grades the synthesizer's
`task.result` body.

#### Verdict handling

The judge returns the same JSON shape as `/goal`'s evaluator
(`{result, explanation, feedback}`). Verdicts:

- `satisfied` → `mission.status='satisfied'`, `completed_at=now`. Emit
  `mission.evaluation.end` and stop continuations. The coordinator
  session is left alive (the user may keep chatting), but no more
  evaluator passes fire.
- `blocked` or `failed` → set the matching status and stop, same as
  `satisfied`. The dashboard surfaces the difference; the coordinator
  can call `task_block` on its own session if a human input is required.
- `needs_revision` → bump `mission.iteration`, set the
  `last_evaluation_*` fields, emit `mission.continuation`, and append a
  synthetic user message with the continuation prompt (below). The
  coordinator wakes on the new message and decides what to spawn next.
- On repeated evaluator JSON parse failures (3 in a row), pause the
  mission with `paused_reason="evaluator parse failure"` — same rule as
  `/goal`.

#### Continuation prompt content

The continuation message is **not** "revise your prose" (`/goal`'s
default). It tells the coordinator to inspect the workstream and
intervene structurally:

```
[Continuing toward your mission]

Description: <mission.description>

Rubric:
<mission.rubric>

Evaluator verdict: needs_revision
Evaluator feedback: <feedback or explanation>

Current mission state:
- <N> tasks completed
- <M> tasks in flight (running/ready/todo/blocked)
- Iteration <i>/<max_iterations>

Inspect the mission task tree via task_show on a recent child if you
need detail. Then either:
  (a) spawn one or more corrective tasks (via spawn_task) to address
      the evaluator's feedback, OR
  (b) call task_block on your own session with a question if you need
      human input, OR
  (c) call task_complete on your own session with a failure summary if
      you believe the rubric cannot be satisfied.

Do NOT claim completion in prose alone. The evaluator only honors a
completion claim when a verifier task's result_metadata supports it,
or when you explicitly mark completion with [[mission-complete]] on
its own line.
```

#### Mission lifecycle events

All emitted on the coordinator session's event log; the dashboard polls
these to render mission state.

- `mission.defined` — payload: `{mission_id, description, rubric, max_iterations}`
- `mission.evaluation.start` — payload: `{mission_id, iteration, trigger}`
  where `trigger ∈ {"task_terminal", "completion_claim"}`
- `mission.evaluation.end` — payload: `{mission_id, iteration, result, explanation, feedback, parse_failed}`
- `mission.continuation` — payload: `{mission_id, iteration}`
- `mission.paused` — payload: `{mission_id, reason}`
- `mission.resumed` — payload: `{mission_id}`
- `mission.cancelled` — payload: `{mission_id, reason, cascade_to_workers}`

### Pause and Cancel Semantics

- `/mission pause` and `POST /missions/:id/pause` stop **future evaluator
  passes** only. The mission row transitions to `status='paused'`.
  Running worker tasks linked to this mission continue uninterrupted.
- `/mission cancel` and `POST /missions/:id/cancel` accept an optional
  `cascade_to_workers: bool` (default `false`):
  - `cascade_to_workers=false` (default): mission status →
    `'cancelled'`; evaluator stops; running mission tasks continue
    until natural completion. The dashboard surfaces a banner ("N
    workers still running. [Cancel all workers]") so the user is not
    surprised by hours of post-cancel compute.
  - `cascade_to_workers=true`: in addition to the above, the backend
    issues `cancel_task` (the existing tool / API) on every
    non-terminal task with `mission_id = mission.id`. Each running
    worker session gets an `INTERRUPT_CHANNEL_PREFIX` interrupt and
    exits cleanly.

### Mission APIs

- `GET /missions`: list current user/agent missions, newest first. Supports
  `?status=` filter (one of the status enum values; comma-separated for
  multiple).
- `GET /missions/{mission_id}`: return mission summary, latest evaluator
  state (`last_evaluation_*`), and counts of tasks by status.
- `GET /missions/{mission_id}/tasks`: return the mission task DAG.
  Response includes every task with `mission_id = id`, plus their
  `task_links` edges so the client can render dependency arrows.
- `GET /missions/{mission_id}/workers`: return live/recent worker
  activity summaries:
  - `task_id`
  - `worker_session_id`
  - `agent_def_name` (may be null for plain-spawn workers; this surface
    is mission-scoped so they're always task-backed)
  - `task_status` and `session_status`
  - `latest_event_id`, `latest_event_kind`, `latest_event_at`,
    `latest_event_summary` — the **client** derives a human-friendly
    activity label from these (priority: latest `tool.call` → tool name
    + truncated args; else latest `llm.response` → first 80 chars; else
    fall back to session status). Keeping label-derivation client-side
    avoids re-implementing the same heuristic in the backend.
  - `transcript_url` — relative URL to the worker session's full
    transcript page; clicked from the dashboard, opens in a new tab
- `POST /missions/{mission_id}/pause` — body: `{reason: string?}`. Stops
  evaluator passes; running workers unaffected.
- `POST /missions/{mission_id}/resume` — re-enables the evaluator and
  enqueues the coordinator session so it processes any queued
  continuations.
- `POST /missions/{mission_id}/cancel` — body:
  `{reason: string?, cascade_to_workers: bool = false}`. See *Pause and
  Cancel Semantics* in the previous section.

### Dedicated Mission Dashboard

- Add a frontend route at `/missions/:missionId`.
- Mission header shows description, status, rubric summary, iteration,
  and evaluator state (latest verdict + feedback excerpt).
- Task graph shows durable `spawn_task` DAG status, dependencies,
  retries, and blockers. Built client-side from `GET .../tasks`.
- Live workers panel shows concise current activity per active/recent
  worker. The client derives the activity label from the event-summary
  fields the `workers` endpoint returns (see *Mission APIs*).
- Evidence panel shows worker completions (with `result_metadata` JSON
  pretty-printed), the latest mission evaluator result + feedback, and
  transcript links.
- Controls support pause, resume, and cancel. The cancel button opens a
  confirm dialog with the `cascade_to_workers` checkbox surfaced:
  unchecked by default, with the running-workers count shown inline
  ("3 workers will continue running unless you check this") so the user
  cannot accidentally abandon expensive jobs mid-flight.
- After a non-cascade cancel, the dashboard header renders a persistent
  banner: "Mission cancelled. N workers still running. [Cancel all
  workers]". The banner clears once the workers reach terminal states.
- Worker rows link to the full session transcript (open in new tab)
  instead of embedding full transcripts inline.
- Poll mission/task/worker APIs while a mission is active (5s
  cadence — matches `tasks_tick`). Stop polling when the mission status
  becomes terminal. No mission-specific SSE stream in v1.

## Failure Handling

- **Coordinator claims completion in prose with no evidence.** The
  evaluator only honours a completion claim when (a) a recent mission
  task supports it via `result_metadata` matching the rubric, or (b)
  the coordinator emitted the explicit `[[mission-complete]]` marker.
  Pure prose claims trigger `needs_revision` with a continuation prompt
  that re-states the evidence requirement.
- **Coordinator creates no tasks and refuses to spawn.** The evaluator's
  `mission task state` block (empty) makes this visible to the judge;
  the verdict is `needs_revision` and the continuation prompt
  explicitly enumerates options (`spawn_task` corrective work,
  `task_block` for human input, `task_complete` with a failure
  summary). Repeated stalls hit `max_iterations` and end as
  `max_iterations_reached`.
- **Tasks block, fail, or are cancelled.** The mission remains `active`
  until the coordinator addresses them (spawn replacement, replan,
  block the mission) or the evaluator marks the mission `blocked`
  or `failed` based on the workstream state.
- **User chats mid-mission.** The coordinator session is the chat
  session; user messages are appended like any other user message and
  the coordinator sees them on its next wake alongside pending
  `WORKER_COMPLETE` events. This is a feature — the user can nudge or
  redirect the coordinator mid-flight. It is *not* a pause; the
  evaluator continues firing on its triggers.
- **Pausing a mission does not pause running worker sessions.** Worker
  sessions execute independently and may still complete (or block, or
  fail) while the mission is paused. The dashboard shows the paused
  state without hiding live worker activity.
- **Cancelling a mission does not cancel running worker sessions unless
  `cascade_to_workers=true` is passed.** The dashboard surfaces a
  running-workers warning after a non-cascade cancel (see *Dedicated
  Mission Dashboard*). The cascade option uses the existing
  `cancel_task` flow per-child so the implementation is shared with the
  `cancel_task` tool path.
- **Repeated evaluator parse failures pause the mission** after 3
  consecutive failures, mirroring `/goal`. `paused_reason` is set to
  `"evaluator parse failure"`.
- **Evaluator rate limit** prevents runaway evaluator calls when the
  coordinator emits many quick responses (or many tasks complete in
  burst). At most one evaluator call per 30 seconds per mission.

## Test Plan

### Backend Unit Tests

- `/mission` parser supports empty/status/control/create/rubric cases.
- `mission` is reserved from slash-skill expansion (backend) AND from
  the dynamic-skill autocomplete catalog (web/SDK).
- `/mission` create without a `Rubric:` block returns a clear error.
- Creating a mission rejects when another active or paused mission
  exists on the same session.
- Creating a mission rejects when the session has an active `/goal`
  outcome; `/goal` set rejects when an active or paused mission
  exists.
- Creating a mission sets coordinator config, `active_mission_id`, and
  preloaded `subagent-task-orchestrator` skill.
- `spawn_task` stamps `mission_id` for active-mission sessions and
  leaves it `NULL` for non-mission sessions.
- Mission evaluator **timing**:
  - Fires on a mission task transitioning to a terminal state (after
    the coordinator's next no-tool-call response).
  - Fires on a coordinator response containing `[[mission-complete]]`.
  - Does NOT fire on every no-tool-call response (the `/goal`-style
    rule must NOT apply for missions).
  - Respects the 30s rate-limit (a second qualifying event inside the
    window is skipped).
- Mission evaluator **inputs** include rubric, latest coordinator
  response, completed mission tasks block, and in-flight tasks block.
- Mission evaluator verdict transitions:
  - `satisfied` → `status='satisfied'`, no further evaluator calls
  - `needs_revision` → `iteration` bumps, continuation event + synthetic
    message emitted
  - `blocked` → `status='blocked'`
  - `failed` → `status='failed'`
  - 3 consecutive parse failures → `status='paused'`,
    `paused_reason="evaluator parse failure"`
  - `iteration >= max_iterations` → `status='max_iterations_reached'`
- Cancel with `cascade_to_workers=true` issues `cancel_task` on every
  non-terminal task with `mission_id = id`.
- Cancel with `cascade_to_workers=false` leaves non-terminal tasks
  running.

### Backend Integration Tests

- `/mission` create emits `mission.defined`, synthetic kickoff, and
  enqueues the coordinator session.
- Pause/resume/cancel work from slash command and API; events emitted
  accordingly.
- Mission tasks endpoint returns only tasks for that mission, with
  `task_links` edges.
- Worker summary endpoint reports active worker status, latest event
  id/time, and event-derivation fields the client uses for the activity
  label.
- Cancelled mission (`cascade_to_workers=false`) does not automatically
  cancel running workers, and the workers complete naturally afterwards.
- Cancelled mission (`cascade_to_workers=true`) interrupts running
  workers via the `INTERRUPT_CHANNEL_PREFIX` pub/sub path.
- End-to-end: mission with a verifier task pattern — coordinator spawns
  workers, workers complete with `result_metadata`, evaluator reads the
  task block and returns `satisfied` when the rubric threshold is met.
- End-to-end: mission with a stalling coordinator — coordinator spawns
  no tasks; iteration counter bumps each evaluator pass; mission hits
  `max_iterations_reached`.

### Frontend Tests

- Mission list/detail API clients typecheck.
- `/missions/:missionId` renders header, task graph, live worker rows,
  evidence panel, and controls.
- Activity label is derived correctly from the latest-event fields the
  workers endpoint returns (priority: tool call → llm response → session
  status fallback).
- Pause/resume buttons call the correct endpoints and update UI state.
- Cancel button opens the confirm dialog showing the
  `cascade_to_workers` checkbox and the running-workers count; both
  paths POST the correct body.
- After a non-cascade cancel, the dashboard renders the
  running-workers banner with a "Cancel all workers" CTA that issues
  the cascade flow.
- Worker rows link to the full session transcript (new tab).
- Polling stops automatically when the mission reaches a terminal
  state.

## Assumptions and Explicit Decisions

- Command name is `/mission`; `/launch` can be added later as an alias
  but is not part of v1.
- V1 uses an LLM rubric judge as the authoritative mission completion
  signal. Deterministic / programmatic verifiers (e.g. "score >= X
  computed by code") are expressed as **verifier tasks** the coordinator
  spawns — they produce structured `result_metadata` that the judge
  reads. The judge is always an LLM.
- **The current chat session IS the coordinator session.** No separate
  coordinator child session is created. Trade-off: simpler UX (the user
  doesn't have to navigate to a separate place), at the cost of a
  long-running coordination thread tangled with the user's chat. The
  dashboard is the dedicated focus surface; this trade-off is
  intentional for v1 and may be revisited if friction emerges.
- Dashboard worker visibility is a live activity summary with transcript
  links, not full inline transcripts or process-output tails.
- Mission dashboard is a dedicated page (`/missions/:missionId`), not a
  chat side rail.
- The `subagent-task-orchestrator` skill needs an addendum for
  criterion-driven loops (the verifier-task pattern, the
  `[[mission-complete]]` marker convention, the "spawn corrective work
  instead of revising prose" instruction). Ship the addendum alongside
  the `/mission` command in the same PR set so the orchestrator
  behaviour is consistent with what the evaluator expects.
- No structured `success_criterion` column on `tasks` in v1; criterion
  lives in `missions.rubric`, and per-task criterion targets (when the
  coordinator spawns verifier tasks) live in the spawn_task `context`
  field. A structured column can be added in a follow-up if the v1 free-
  text shape proves too loose.
- Recursive orchestrator spawning (an orchestrator-typed task that
  itself spawns more orchestrator tasks) is out of scope. The current
  `WORKER_EXCLUDED_TOOLS` rule prevents this; revisit if a use case
  emerges where a sub-mission needs its own coordination context.
