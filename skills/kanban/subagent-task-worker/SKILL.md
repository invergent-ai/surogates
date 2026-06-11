---
name: subagent-task-worker
description: "Pitfalls, examples, and edge cases for workers executing a Surogates subagent task. Loaded automatically when the dispatcher spawns a session bound to a Task (Session.task_id is set)."
version: 1.0.0
license: MIT
---

# Subagent Task Worker — Pitfalls and Examples

> You're seeing this skill because the dispatcher spawned you for a subagent task — your `Session.task_id` is set and the harness has exposed the `worker_complete`, `worker_block`, and `worker_context` self-tools to you. This skill is the deeper detail beyond the system-prompt basics: good handoff shapes, retry diagnostics, edge cases, and what NOT to do.

See [Tasks (Subagent Task Layer)](../../../docs/tasks/index.md) for the conceptual chapter and [Tools](../../../docs/tools/index.md) for the parameter tables.

## What you have access to

You're running for a Task. Three self-tools are available only to you, gated by `Session.task_id`:

| Tool | Use it when |
|---|---|
| `worker_context` | At the start of your run, *especially* on retry. Returns goal, accumulated context (from prior unblocks), parent task results, and prior attempt summaries. |
| `worker_complete` | When you've actually finished. Writes the explicit summary + structured metadata to the task row; the parent agent sees this in its `worker.complete` event. |
| `worker_block` | When you need new context that isn't available without a human or peer providing it. Does NOT consume a retry attempt. |

You also have the same tool surface a `spawn_worker` child would have: read/write files, web, terminal, etc. -- subject to the `AgentDef` filter pinned to this attempt.

## Step 0 — Orient yourself

The first thing you should do on a non-trivial task is `worker_context`. It tells you:

- The original `goal`.
- Any `context` accumulated from prior unblocks (timestamped).
- `attempt_count` -- if > 1, you are a retry. Read the `prior_attempts` list before doing anything.
- `parents` -- if non-empty, each entry's `result` and `result_metadata` is the upstream work you're building on. Don't re-derive it.

Your initial USER_MESSAGE already includes a short summary of prior attempts (when `attempt_count > 1`), bounded to the last 5. `worker_context` is how you read the full detail.

## Good `summary` + `metadata` shapes

`worker_complete(summary, metadata)` is how downstream readers (the parent agent, future retries, humans) understand what you did. Aim for `summary` to be 1-3 sentences a human can scan; `metadata` to be machine-readable facts.

**Coding task:**

```python
worker_complete(
    summary="shipped rate limiter — token bucket keyed on user_id with IP fallback; 14 tests pass",
    metadata={
        "changed_files": ["rate_limiter.py", "tests/test_rate_limiter.py"],
        "tests_run": 14,
        "tests_passed": 14,
        "decisions": ["user_id primary; IP fallback for unauthenticated requests"],
    },
)
```

**Coding task that needs human review:**

For most code-changing work, "done" should mean "human reviewed and approved." Use `worker_block` instead of `worker_complete`, with a `reason` that starts `review-required:`. Leave the structured info (diff path, test counts, what to look at) for the parent agent or human to discover via `worker_context`.

```python
# (NOT worker_complete — block instead so a reviewer steps in)
worker_block(
    reason=(
        "review-required: rate limiter shipped, 14/14 tests pass — "
        "needs eyes on the user_id-vs-IP fallback choice before merge"
    ),
)
```

A reviewer (human, or another task spawned by the orchestrator) then calls `unblock_task` to resume you, OR `cancel_task` if a fresh attempt is wanted.

**Research task:**

```python
worker_complete(
    summary="3 inference servers reviewed; vLLM wins on throughput, SGLang on latency, TRT-LLM on memory",
    metadata={
        "sources_read": 12,
        "recommendation": "vLLM",
        "benchmarks": {"vllm": 1.0, "sglang": 0.87, "trtllm": 0.72},
    },
)
```

**Review task:**

```python
worker_complete(
    summary="reviewed PR #123; 2 blocking issues: SQL injection in /search, missing CSRF on /settings",
    metadata={
        "pr_number": 123,
        "findings": [
            {"severity": "critical", "file": "api/search.py", "line": 42, "issue": "raw SQL concat"},
            {"severity": "high", "file": "api/settings.py", "issue": "missing CSRF middleware"},
        ],
        "approved": False,
    },
)
```

Shape `metadata` so downstream parsers (the parent orchestrator, an aggregator task, a reviewer skill) can use it without re-reading your prose.

## The coordination board — share as you work

Because you were spawned into a fan-out, you also have `share_note`, `read_board`, and `expand_note`: a shared, verified board that all sibling workers, retries, and the coordinator read. This is how your discoveries help peers *while they work*, instead of only at handoff time.

When to write (batch related notes into one `share_note` call):

- **FAIL — highest value, post immediately.** You tried something and it dead-ended: name what you ran and the observed error. A sibling is one note away from repeating it.
- **FACT** for reusable knowledge anchored to specifics (path, symbol, endpoint, error class). Not narration — anchors a peer can act on.
- **CLAIM** before starting a substantial unit of work that a sibling might also pick ("claiming the slack adapter"). Expires automatically; re-post to renew.
- **RESULT** when you have a candidate outcome, as `outcome=…|evidence=…|risk=…` where the evidence names a check you ACTUALLY ran and what it printed. Your latest RESULT replaces your previous one.

When to read: `[Board update]` messages arrive in your history automatically as peers post. Additionally call `read_board` at decision points — before committing to an approach, and before retrying anything that smells like a known dead end — because inline updates may be stale (superseded results, expired claims).

Notes are admission-verified: vague or unevidenced notes are rejected with a reason. Write them anchored the first time. Sharing on the board does NOT replace `worker_complete` — the board is live coordination; the completion summary is your durable handoff.

## Block reasons that get answered fast

Bad: `"stuck"`. The human or parent has no context.

Good: one sentence naming the specific decision. If you need more context to justify the question, do it in the work you've done so far -- don't stuff a paragraph into the reason. The parent/human can call `worker_context` to read your full state.

```python
worker_block(
    reason=(
        "Rate-limit key choice: should I key on IP (simple, NAT-unsafe) "
        "or user_id (requires auth — skips anonymous endpoints)?"
    ),
)
```

## Retry scenarios

When `worker_context` returns `task.attempt_count > 1`, you are a retry. The `prior_attempts` array tells you what earlier sessions did:

- `outcome: "completed"` -- they emitted a structured summary but the task was reopened (rare). Their `summary` is in the entry. Don't redo their work.
- `outcome: "blocked"` -- a previous attempt blocked; an `unblock_task` re-launched you. Read the accumulated `task.context` for what was added on unblock.
- `outcome: "crashed"` -- the prior session ended without emitting either a complete or block event (crash, OOM, timeout, hard-kill). No structured summary is available; check the parent's events or `worker_context` for any clue.

**Don't repeat what failed.** If three prior attempts crashed at the same step, change your approach, narrow scope, or block for guidance.

## Tenant isolation

If your session has `org_id` set, you are scoped to that tenant. Any persistent memory you write should be prefixed by the tenant so context doesn't leak across orgs. The Surogates memory tool generally namespaces by tenant automatically; if you write directly to shared scratch files, prefix manually.

## Do NOT

- **Call `delegate_task` as a substitute for `spawn_task`.** `delegate_task` is a synchronous fork-join for short reasoning subtasks inside YOUR run; `spawn_task` is for durable cross-agent handoffs that outlive one API loop.
- **Call `spawn_task` if you are a leaf worker.** Children spawned by either `spawn_worker` or `spawn_task` have `WORKER_EXCLUDED_TOOLS` applied; if `spawn_task` is in your toolset, you're an orchestrator-shaped session (and you should see the [orchestrator skill](../subagent-task-orchestrator/SKILL.md) instead).
- **Complete a task you didn't actually finish.** Use `worker_block` to ask for help; the retry budget is not consumed by blocking.
- **Modify files outside your sandbox workspace** unless the task body says to. The parent's workspace is shared via inheritance; don't surprise it.
- **Hand-write task ids into your prose.** When you spawn child tasks (only if you're an orchestrator-shaped worker), keep the returned `task_id` from each `spawn_task` call and reference them in your `worker_complete` summary by quoting the actual return value, not making one up.

## Pitfalls

**Task state can change between dispatch and your startup.** Between when the dispatcher claimed the task and your process boot, the task may have been cancelled or reblocked. Always `worker_context` first. If the task is no longer `running` (e.g. `cancelled` or `blocked`), stop — you shouldn't be doing the work.

**Your attempt may have been reclaimed.** If the session lease expired while you were inside a long-running tool call, the dispatcher's stale-claim recovery may have started a new attempt. The `worker_complete` / `worker_block` tools refuse with "this attempt is no longer the current task attempt" — that's the signal. Exit cleanly; the new attempt has the work.

**Don't rely on a CLI.** The `task_*` tools work uniformly across all execution backends (sandbox, Modal, remote SSH). There is no `surogates kanban` CLI to fall back on — use the tool surface.

**Read your parents' results.** When the task has `parents`, each parent's `result` and `result_metadata` is the upstream work. The orchestrator placed you here because their output is your input. Read it via `worker_context`; don't re-derive.
