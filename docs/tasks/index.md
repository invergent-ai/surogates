# 11a. Tasks (Subagent Task Layer)

The **task layer** adds durable, DAG-aware coordination on top of the existing [sub-agent](../sub-agents/index.md) infrastructure. A *task* is a row in the `tasks` table that wraps zero or more `Session` attempts of the same goal, with retries, fan-in dependencies, and self-pause-for-input semantics.

When to reach for a task vs the existing primitives:

| You want | Use |
|---|---|
| Short reasoning subtask, parent blocks for the result | `delegate_task` (existing) |
| Fire-and-forget async work, one shot, no retry, no DAG | `spawn_worker` (existing) |
| Durable goal that survives crashes; multi-parent DAG; auto-retry; mid-flight pause via `task_block` | `spawn_task` (this chapter) |

The task layer **wraps** `spawn_worker`, it does not replace it. Both remain registered side-by-side; child sessions that get spawned for a task are normal `Session` rows with a new `task_id` column pointing at their owning task.

## Concepts

**Task** -- a row in `tasks` representing a durable goal. Has a [status state machine](#status-state-machine), a goal string, an optional context blob (appended on unblock), and structured `result` / `result_metadata` once the worker completes. Tasks live forever; the GC strategy is shared with sessions.

**TaskLink** -- a row in `task_links` carrying one parent->child DAG edge. A child task may have multiple parents (fan-in); it stays in `todo` until *every* parent has reached `done`. Cancelled or failed parents intentionally do **not** unblock children -- orchestrators must cancel/replan downstream explicitly.

**Attempt** -- one execution of a task by a worker session. Recorded via `sessions.task_id`; the dispatcher tick bumps `attempt_count` on each claim. After `max_attempts` (default 3) consecutive crash/timeout failures the task transitions to `failed`. A `task_block` does *not* consume an attempt -- blocking is a deliberate pause, not a failure.

**Spawning parent** -- the `Session` that called `spawn_task`. Stored as `tasks.parent_session_id`. Only the spawning parent's session may `unblock_task` or `cancel_task` its children.

## Status state machine

| Status | Owner | Meaning |
|---|---|---|
| `todo` | tick | Created; one or more parents not yet `done` |
| `ready` | tick | All parents done (or none); eligible for atomic claim |
| `running` | tick | A Session (`current_session_id`) is executing |
| `blocked` | `task_block` tool | Awaiting unblock_task or human intervention |
| `done` | tick / `task_complete` | Result written; terminal |
| `failed` | tick | All `max_attempts` exhausted, terminal |
| `cancelled` | `cancel_task` tool | Explicitly aborted; terminal |

```
                  parents all done            atomic claim
   ┌──────┐  ─────────────────────►  ┌──────┐ ─────────────►  ┌─────────┐
   │ todo │                          │ready │                  │ running │
   └──────┘                          └──────┘                  └─────────┘
       ▲                                 ▲                          │
       │              attempt_count < max_attempts                  │
       │           ┌───────── retry on crash ──────────────────────┤
       │           │                                                │
       │           │              task_block                        ▼
       │           │            ┌───────────────────────────► ┌─────────┐
       │           │            │                              │ blocked │
       │           │            │                              └─────────┘
       │           │            │      unblock_task                 │
       │           │            └◄──────────────────────────────────┘
       │           │
       │           │   max_attempts exhausted          natural complete
       │           │                                         OR task_complete
       │           ▼                                                ▼
       │       ┌────────┐                                      ┌──────┐
       │       │ failed │                                      │ done │
       │       └────────┘                                      └──────┘
       │
       └──────  cancel_task (from any non-terminal state)  ─────►  cancelled
```

Cancelled and failed are terminal; the orchestrator must explicitly cancel or replan any children that depended on them.

## Tools

The task layer ships **six** tools, registered into the `core` toolset alongside the existing `spawn_worker` / `delegate_task` family. Per-session filtering (`orchestrator/worker.py::_filter_effective_tools`) hides the three "self-tools" from sessions that have no `task_id` set, so plain chat and plain-`spawn_worker` children never see them.

### Coordinator tools (available to the agent that's calling `spawn_task`)

| Tool | Purpose |
|---|---|
| `spawn_task` | Create a durable task; optionally with DAG `parents=[...]` |
| `unblock_task` | Resume a blocked task with optional `additional_context` (ownership-checked) |
| `cancel_task` | Abort a non-terminal task; if running, interrupts the in-flight Session (ownership-checked) |

### Self-tools (available to the worker running for a task)

| Tool | Purpose |
|---|---|
| `task_complete` | Mark own task `done` with explicit `summary` + structured `metadata` |
| `task_block` | Self-pause without consuming a retry attempt |
| `task_show` | Read own task + parents (with results) + prior attempt summaries |

See [Tools](../tools/index.md) for full parameter tables.

## How the tick drives state transitions

A 5-second loop hosted in `surogates.orchestrator.dispatcher.Orchestrator._tasks_tick_forever` (alongside the existing orphan sweeper) runs three SQL-driven steps per pass:

1. **Promote** -- bulk `UPDATE tasks SET status='ready' WHERE status='todo' AND every parent is done`. Cancelled or failed parents do not promote children (design rule).
2. **Finalize** -- for each task in `running` whose `current_session_id` Session has ended, classify the attempt via `surogates.tasks.completion.classify_attempt_outcome` and write the resulting state:
   - `WORKER_COMPLETE` event present -> `done`, `result` from payload
   - `TASK_BLOCKED` event present -> already handled by the tool (no-op)
   - no completion event (crash / timeout / hard-kill) -> retry if `attempt_count < max_attempts`, else `failed` (+ emit `TASK_FAILED` to parent)
3. **Enqueue** -- atomic claim via `UPDATE ... RETURNING` (avoids a Postgres deadlock between the row lock and the FK lock the subsequent child-Session INSERT needs to acquire). Per-tick cap of 10 enqueues. A within-tick skipset prevents a single broken task (missing AgentDef, bad workspace config) from hot-looping and starving the queue.

The tick is **idempotent** under concurrent invocation across replicas: each step uses `status=X` SQL guards, and the claim's `UPDATE ... RETURNING` serializes against concurrent UPDATEs from another tick. Two ticks racing against the same row will at worst do a no-op on the losing side.

## Events emitted to the parent session

When a worker session for a task ends, the parent session is woken via the existing inbox / wake mechanism. Three event types carry the outcome:

| Event | When emitted | Payload |
|---|---|---|
| `worker.complete` | Worker session ended (natural or via `task_complete`) | `{worker_id, result, task_id?, metadata?}` |
| `task.blocked` | Worker called `task_block` | `{task_id, worker_id, reason}` |
| `task.failed` | Tick observed crash after `attempt_count >= max_attempts` | `{task_id, worker_id, attempt_count}` |

`WORKER_COMPLETE` is reused (rather than introducing a separate `TASK_COMPLETED`) so a parent agent's handling of "a child finished" stays uniform across `spawn_worker` and `spawn_task` paths. The `task_id` key is present only for task-backed sessions; plain `spawn_worker` completions omit it. When the worker called `task_complete` explicitly, the `result` and `metadata` reflect the worker's structured handoff; when the worker completed naturally, `result` is the auto-extracted LLM final response and `metadata` is omitted.

## Retry context

When the dispatcher claims a task for the second or subsequent attempt, `_create_session_for_task` injects a `## Prior attempts on this task` section into the new attempt's initial USER_MESSAGE. Each prior session contributes one line: completed attempts include their result, blocked attempts include the reason, crashed/timed-out attempts get a placeholder. Bounded to the last 5 entries so deep retry chains stay context-bounded.

A retry worker can also call `task_show` to read the full structured detail of every prior attempt (session_id, outcome, summary).

## Multi-tenancy

Every task carries `org_id` (FK to `orgs`). `task_links` and `sessions.task_id` inherit scope via FK. The `spawn_task` tool refuses cross-org parents -- a task in org A cannot list a task in org B in its `parents=[...]`. The dispatcher tick processes tasks regardless of org (one orchestrator process serves one `agent_id`, which is org-scoped via the Redis work queue key).

## Workspace inheritance

Task-backed worker sessions reuse the spawning parent's workspace via the existing `create_child_session` helper -- same `storage_bucket`, same `workspace_path`, same `sandbox_root_session_id`. This means worker attempts for the same task share state, and a retry sees whatever the prior attempt wrote. Channel is `task` (vs `worker` for `spawn_worker` children) so UIs can filter cleanly.

## Configuration

No new configuration. The dispatcher tick is enabled automatically when the orchestrator is constructed with `session_factory` and `tenant_for_task` (the bootstrap in `surogates.orchestrator.worker` always sets both). Without these, the tick is disabled with a warning at startup and `spawn_task` still creates task rows -- they just don't progress until a tick-enabled orchestrator runs.

## Choosing between `spawn_worker` and `spawn_task`

`spawn_worker` is the right tool when:

- You want a child running *right now* and you want to get its session id back immediately.
- The work is one-shot and you don't care about retry.
- You don't need DAG dependencies.

`spawn_task` is the right tool when any of:

- The work needs to survive a parent crash.
- Multiple specialist tasks must complete before a synthesizer runs (fan-in).
- You want bounded automatic retry on crash / timeout.
- The worker might need to pause for context mid-run (via `task_block`).
- Humans (or other agents) need to see and steer the work.

Both can coexist within one parent session; pick per call.

## Missions: orchestrated, rubric-judged objectives

A **mission** is a long-running objective with a written rubric. The coordinator agent spawns tasks (using everything described above), and an LLM judge keeps grading the workstream against the rubric — spawning more rounds when the rubric isn't yet met, ending the mission when it is.

Use a mission when "done" is criterion-driven and the user wants to set the bar up front (`gsm8k score >= 0.8`, `coverage >= 95%`, `all critical TODOs resolved`). Use raw `spawn_task` when the orchestrator's own judgement is enough.

### Starting a mission

Open a chat with your agent and send:

```
/mission Train a small classifier on the customer-feedback dataset. Generate training data if needed; iterate until the eval metric is met.

Rubric:
A verifier task must run the eval suite and record result_metadata.accuracy. Satisfied when accuracy >= 0.85.
```

That's the whole API. The agent becomes the mission's coordinator, the rubric is preserved on the session, and a kickoff message tells the agent to decompose the work.

The slash command's other forms:

| Form | Effect |
|---|---|
| `/mission` or `/mission status` | Show the current mission's status, iteration, latest verdict |
| `/mission pause [reason]` | Pause the evaluator loop. Workers in flight keep running. |
| `/mission resume` | Resume a paused mission |
| `/mission cancel [reason]` | Cancel without touching workers — they finish whatever they're on |
| `/mission cancel --cascade [reason]` | Cancel and interrupt every still-running worker |

You can have at most one `/mission` or `/goal` per chat session — they share an evaluator loop.

### Mission states

| Status | Meaning |
|---|---|
| `active` | The judge is grading new evidence as it arrives |
| `paused` | Evaluator suspended; workers continue. `/mission resume` reactivates. |
| `satisfied` | Terminal — rubric met |
| `blocked` | Terminal — judge says the rubric needs external input the agent hasn't requested |
| `failed` | Terminal — judge says the rubric is unreachable from where things stand |
| `cancelled` | Terminal — user cancelled |
| `max_iterations_reached` | Terminal — hit the 20-iteration cap without a `satisfied` verdict |

### When the judge runs

The judge does **not** run after every agent response. That would be expensive and would grade unchanged state. It runs only when:

1. A task the coordinator spawned (or any of its sub-tasks) reaches a terminal state — there's actual new evidence to grade. **This is the normal path.**
2. The coordinator emits `[[mission-complete]]` on its own line — an explicit "look now" hint from the agent.

A 30-second rate limit per mission keeps a burst of completing tasks from triggering a flurry of judge calls.

### How to design a rubric the judge can answer

The judge can't grade prose. It needs evidence in a structured form. The pattern that always works:

1. The orchestrator spawns the work tasks (research, training, generation, fixing, whatever).
2. The orchestrator spawns a **verifier task** that depends on the work tasks (`parents=[…]` so it runs after they finish). The verifier's job is to compute the measurable signal the rubric mentions and call `task_complete(summary, metadata={"<key>": <value>})` with it.
3. When the verifier finishes, the judge sees the verifier's metadata and grades against it.

So write rubrics in terms of metadata a verifier can produce:

- ✅ "Satisfied when `result_metadata.accuracy >= 0.85`"
- ✅ "Satisfied when `result_metadata.coverage_percent >= 95`"
- ✅ "Satisfied when `result_metadata.failing_tests == 0`"
- ❌ "Satisfied when the agent says it's done" (the judge ignores prose claims)
- ❌ "Satisfied when the code is clean" (no measurable signal)

The orchestrator skill bundled with the platform walks the coordinator through this pattern; you don't have to spell it out in every prompt.

### What happens when the judge says "needs revision"

The judge picks one of four verdicts: `satisfied`, `needs_revision`, `blocked`, `failed`. The first and last three are terminal. `needs_revision` means "keep going":

- The mission's iteration counter bumps.
- The judge's feedback is delivered to the coordinator as a continuation message ("here's what's missing; spawn the next round").
- The coordinator wakes, reads the feedback, decides what to do next — usually spawn corrective tasks.
- After 20 iterations without a `satisfied` verdict, the mission terminates as `max_iterations_reached`.

If the coordinator decides the rubric genuinely can't be met (contradictory criteria, missing data), it can call `task_complete` on itself with a failure summary; the judge reads that as evidence and returns `failed`.

### Pausing, resuming, cancelling

- **Pause** stops the evaluator from grading new evidence. Workers in flight keep running and recording their results; the judge just won't react. Useful when you want to inspect the workstream without it changing under you.
- **Resume** turns the evaluator back on and wakes the coordinator so any pending continuations get processed.
- **Cancel** terminates the mission. Without `--cascade`, currently-running tasks finish (their results are no longer graded). With `--cascade`, every running worker session for this mission gets an interrupt — use this when you've decided the whole direction is wrong.

If the judge fails to return parseable output three times in a row, the mission auto-pauses with reason `evaluator parse failure` so you can investigate; a single recoverable failure doesn't pause anything.

### Where to watch a mission run

Each agent surfaces missions in two places:

- A **Missions** panel in the sidebar, listing every active or paused mission. Click a row to open the dashboard.
- A **mission dashboard** that shows the rubric, current iteration, latest verdict, the task DAG (grouped by status), and live worker activity with links into each worker session's transcript. Active missions auto-refresh every 5 seconds; terminal missions stop polling.

The dashboard is the right place to watch a mission in flight; the chat thread is the right place to talk to the coordinator about it.

## See also

- [Sub-Agents](../sub-agents/index.md) -- the `AgentDef` catalog that `spawn_task`'s `agent_type` parameter resolves into.
- [Tools](../tools/index.md) -- full parameter tables for `spawn_task`, `task_complete`, `task_show`, etc.
- Design spec: [`docs/sub-agents/2026-05-16-subagent-task-layer-v1.md`](../sub-agents/2026-05-16-subagent-task-layer-v1.md)
- Implementation plan: [`docs/sub-agents/2026-05-16-subagent-task-layer-v1-plan.md`](../sub-agents/2026-05-16-subagent-task-layer-v1-plan.md)
- Mission spec: [`docs/superpowers/specs/2026-05-16-mission-orchestrated-goals-design.md`](../superpowers/specs/2026-05-16-mission-orchestrated-goals-design.md)
- Mission orchestrator playbook: [`skills/kanban/subagent-task-orchestrator/SKILL.md`](../../skills/kanban/subagent-task-orchestrator/SKILL.md) (the "Criterion-driven loops" section)
