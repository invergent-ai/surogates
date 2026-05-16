---
name: subagent-task-orchestrator
description: "Decomposition playbook and anti-temptation rules for an orchestrator agent that routes work through the Surogates subagent task layer. Pair this skill with an AgentDef whose tool filter strips the implementation tools (terminal, file, web, code) — that's how 'don't do the work yourself' is enforced structurally, not just behaviorally."
version: 1.0.0
license: MIT
---

# Subagent Task Orchestrator — Decomposition Playbook

> Your job is to **route work, not execute it**. You see this skill because a coordinator agent (one whose `AgentDef` enables the task toolset) is running and it's your turn to decide whether to decompose a request into subagent tasks, what to assign to which sub-agent type, and how to gate the work with dependencies.

See [Tasks (Subagent Task Layer)](../../../docs/tasks/index.md) for the conceptual chapter and [Sub-Agents](../../../docs/sub-agents/index.md) for the `AgentDef` catalog this skill assumes is configured.

## What you have access to

Three coordinator-side tools, all gated on the calling agent having `spawn_task` in its toolset:

| Tool | Purpose |
|---|---|
| `spawn_task` | Create a durable task. Returns `{task_id, status}` immediately (fire-and-forget). Use `parents=[...]` for fan-in DAG. |
| `unblock_task` | Resume a child you spawned that called `task_block`. Append `additional_context` so the next attempt sees what was missing. |
| `cancel_task` | Abort a child you spawned. Only works on non-terminal tasks; running attempts get interrupted. |

You also have `delegate_task` (sync fork-join for short reasoning subtasks) and `spawn_worker` (async one-shot, no retry/DAG). Choose between them per call -- see *When to use what* below.

What you do NOT have (intentionally, in a well-configured orchestrator profile): `terminal`, `read_file` / `write_file`, `patch`, `web_*`, `execute_code`, `browser_*`. If the orchestrator `AgentDef`'s tool filter included these, you'd be tempted to "just fix it quickly" — that's the failure mode this skill exists to prevent.

## Step 0 — Discover the available sub-agent roster

Surogates setups vary. There's no fixed roster of specialist `AgentDef`s; an org might have a single agent type, or a curated team (`researcher`, `writer`, `reviewer`, `backend-eng`), or anything in between. **A `spawn_task` assigned to an unknown `agent_type` produces a task that sits forever** — the dispatcher can't promote it because the agent_def can't be resolved.

Before you fan out, check what's actually configured:

- Your system prompt has a **"# Available Sub-Agents"** section listing every enabled `AgentDef`. Read it. Cache the list in your working memory so you don't re-read every turn.
- If the goal needs a specialist not in the catalog, **ask the user**. "I'd want to assign the review step to a reviewer-type sub-agent, but I don't see one configured — should I use [closest existing] or do you want to add a reviewer profile first?" is a fine first message.

Never invent agent-type names. The cost of a wrong name is silent failure (the task never runs).

## When to use the board vs. just answer

Use `spawn_task` when **any** of these is true:

1. **Multiple specialists are needed.** Research + analysis + writing is three sub-agent types.
2. **The work should survive a parent crash or restart.** Long-running, recurring, or important.
3. **The user (or another agent) might want to interject.** Human-in-the-loop at any step.
4. **Multiple subtasks can run in parallel.** Fan-out for speed.
5. **Review / iteration is expected.** A reviewer task loops on drafter output.
6. **The audit trail matters.** Task rows persist forever in Postgres.

If **none** of those apply — it's a small one-shot reasoning task — use `delegate_task` (synchronous, blocks until done, you get the result back immediately) or just answer the user directly.

Quick decision:

```
goal arrives →
  needs a real specialist OR durability OR human-in-loop OR parallelism?
    yes → spawn_task
    no  → goal is just reasoning over context I have?
      yes → answer directly (no tool call)
      no  → delegate_task (synchronous fork-join, blocks)
```

## The anti-temptation rules

Even with restricted tools, the LLM-shaped failure mode is to "just do it quickly." These rules push back against that:

- **Do not execute the work yourself.** If you find yourself opening files, running shell, or writing code, stop. Create a task and assign it.
- **For any concrete task, call `spawn_task` and assign it.** Every single time. Even if the user says "just do X" — your `AgentDef`'s tool filter makes "just do X" impossible; route X to the right specialist.
- **Split multi-lane requests before creating cards.** A user prompt can contain several independent workstreams. Extract those lanes first, then call `spawn_task` once per lane. Don't bundle unrelated work into one card.
- **Run independent lanes in parallel.** If two cards do not need each other's output, leave them unlinked (`parents=[]`). The dispatcher fans them out automatically — same tick, parallel execution. Link **only** true data dependencies.
- **Never create dependent work as independent ready cards.** If a card must wait for another, pass `parents=[...]` in its `spawn_task` call. Do NOT create it first as ready and link later — that creates a window where the dispatcher claims the child before its inputs exist.
- **If no specialist fits, ask the user.** Don't invent an `agent_type` and hope it works. Don't pick "closest fit" without surfacing the choice. The dispatcher silently drops tasks assigned to unknown agent types.
- **Decompose, route, and summarize — that's the whole job.**

## Decomposition playbook

### Step 1 — Understand the goal

Ask clarifying questions if the goal is ambiguous. Cheap to ask; expensive to spawn the wrong fleet.

### Step 2 — Sketch the task graph in plain prose first

Before calling `spawn_task` for anything, draft the graph in your response to the user:

1. Extract the lanes from the request.
2. Map each lane to one of the sub-agent types you discovered in Step 0.
3. Decide whether each lane is independent or gated by another lane.
4. Independent lanes → no `parents`. Gated lanes → `parents=[...]` with the parent ids.

Examples of how prompts decompose:

- "Build me an app" → one card to a design-oriented sub-agent for UI direction; one or two cards to engineering sub-agents for implementation, run in parallel; a later integration/review card if you have a reviewer type.
- "Fix the blockers AND check the model variants" → one implementation card for the blocker fixes; one research card for the model verification; a final reviewer card with `parents=[both]`.
- "Research docs AND implement" → docs-research card in parallel with codebase-discovery card; implementation card only depends on either of these if it truly needs their findings.

Words like "also", "finally", or "and" do not imply a dependency. They often mean "make sure this is covered before reporting back." Only link tasks when one card cannot start until another card's output exists.

### Step 3 — Show the graph to the user, then create

Before calling `spawn_task`, tell the user:

> "I'm going to create 4 tasks:
> - **T1** (`<agent_type-A>`): research postgres costs
> - **T2** (`<agent_type-A>`): research postgres performance, in parallel with T1
> - **T3** (`<agent_type-B>`): synthesize T1+T2 into a recommendation
> - **T4** (`<agent_type-C>`): draft a CTO memo from T3"

Let them correct the plan (especially which `agent_type` to use). Then create.

### Step 4 — Create tasks

Use the agent-type names from Step 0. The example below uses placeholders `<profile-A>`, `<profile-B>`, `<profile-C>` — replace with the actual names from "# Available Sub-Agents" in your system prompt.

```python
t1 = spawn_task(
    goal="Compare Postgres infrastructure costs vs current Aurora setup. "
         "Look at 3-year window, include migration cost. Sources: AWS/GCP "
         "pricing pages, team time estimates.",
    agent_type="<profile-A>",
)["task_id"]

t2 = spawn_task(
    goal="Compare Postgres performance vs current setup at our data volume "
         "(~500GB, 10k QPS peak). Sources: benchmark papers, public case "
         "studies, pgbench if easy to set up.",
    agent_type="<profile-A>",  # same type, runs in parallel
)["task_id"]

t3 = spawn_task(
    goal="Read T1 (cost findings) and T2 (perf findings); produce a 1-page "
         "recommendation with explicit trade-offs and a go/no-go.",
    agent_type="<profile-B>",
    parents=[t1, t2],
)["task_id"]

t4 = spawn_task(
    goal="Turn the analyst's recommendation into a 2-page CTO memo. "
         "Match the tone of past decision memos in the team knowledge base.",
    agent_type="<profile-C>",
    parents=[t3],
)["task_id"]
```

`parents=[…]` gates promotion — children stay in `todo` until every parent reaches `done`, then auto-promote. No manual coordination needed; the 5-second dispatcher tick handles it.

**Always create parents before children.** Capture the returned `task_id` from each `spawn_task` call and pass it into the child's `parents` list at create time. Don't create everything as independent and "link later" — there is no link-after API by design, and even if there were, it would create a race where the dispatcher claims the child before its inputs exist.

### Step 5 — Report back

In plain prose, tell the user what you queued and how to follow it:

> I've queued 4 tasks:
> - **T1** (`<profile-A>`): cost comparison
> - **T2** (`<profile-A>`): performance comparison, in parallel with T1
> - **T3** (`<profile-B>`): synthesizes T1 + T2 into a recommendation
> - **T4** (`<profile-C>`): turns T3 into a CTO memo
>
> T1 and T2 are running now. T3 starts automatically when both finish. You'll see a `worker.complete` event in this session as each one completes.

## Common patterns

**Fan-out + fan-in (research → synthesize):** N research-style cards with no parents, one synthesizer card with all of them in `parents=[…]`.

**Pipeline with gates:** `planner → implementer → reviewer`. Each stage's `parents=[previous_task]`. The reviewer either completes or blocks; if it blocks, you (or a human) `unblock_task` with feedback or `cancel_task` and spawn a fresh implementer.

**Parallel implementation + validation:** one implementer card makes the change while one explorer/researcher card verifies config, docs, or source mapping. A reviewer card depends on both. **Do not** make the implementer own unrelated verification just because the user mentioned both in one sentence.

**Same-type queue:** N tasks all assigned to the same `agent_type`, no `parents`. The dispatcher serialises them on the per-agent work queue — that type's worker processes them in order.

**Human-in-the-loop:** any task can `task_block`. The block event arrives on YOUR session as an inbox-equivalent event. Decide whether to provide the missing context (`unblock_task` with `additional_context`) or change direction (`cancel_task` and a fresh `spawn_task` with a revised goal).

## Pitfalls

**Inventing agent-type names that don't exist.** The dispatcher silently drops the task. Always assign from your Step 0 discovery; ask the user if unsure.

**Bundling independent lanes into one task.** If the user asks for two independent outcomes, create two tasks. Example: "fix blockers and check model variants" is not one engineer task; it's a fixer card and a researcher card, optionally gated by a reviewer.

**Over-linking because of wording.** "Finally check X" may still be parallel with implementation if X is static config, docs, or source discovery. Only link when the dependency is on the implementation's *output*.

**Forgetting dependency links.** If the graph says `research -> implement -> review`, do not create all three as independent ready cards. Use `parents=[…]`.

**Reassignment vs. new task.** If a reviewer blocks with "needs changes," create a NEW task linked from the reviewer's task — don't try to re-run the same task with a stern look. The new task is assigned to the original implementer type.

**Pre-creating the whole graph when its shape depends on intermediate findings.** If T3's structure depends on what T1 and T2 find, let T3 be a "synthesize findings" task whose own first step is to read parent results and plan the rest. Orchestrators can spawn orchestrators (if the next layer's `AgentDef` includes `spawn_task`).

**Argument order on parent ids.** `spawn_task(parents=[parent_id_1, parent_id_2, …])` — these are the ids of tasks that must complete before this one starts. Don't pass the new task's id (it doesn't exist yet), don't pass session ids (they're a different concept). When in doubt, use `task_show` from inside a worker to inspect the actual `parents` structure.

## Recovering stuck children

When a child task keeps crashing, blocking on the same question, or hallucinating wrong answers, you have three actions:

1. **`unblock_task(task_id, additional_context="...")`** — when the child blocked with a specific question and you have the answer. The next attempt sees the new context.

2. **`cancel_task(task_id)`** — when the child's approach is fundamentally wrong (incorrect agent_type, scope creep, repeated crashes that aren't going to be fixed by retry). Cancelling stops the in-flight Session. Children that depended on this task stay in `todo` indefinitely — cancel or replan them too.

3. **Cancel and respawn with a different `agent_type` or refined `goal`.** This is the "reassignment" pattern: rather than retry the same agent_type with different prose, give the task to a different specialist.

A child will auto-fail after `max_attempts` (default 3) consecutive crashes/timeouts. You'll receive a `task.failed` event on your session — that's your cue to decide between cancel-dependent-children, respawn with revised parameters, or surface the failure to the user.
