# Research (Arbor) skill suite

An autonomous-research workflow ported from Arbor: turn a long-horizon
optimization goal into a cumulative tree search. A coordinator grows a
durable **Idea Tree** of hypotheses; ephemeral executors implement and
evaluate each in an isolated git worktree; verified gains merge into a
protected trunk only after an independently re-run held-out eval.

Launch with **`/auto-research`** (intake first refines the goal into a
Research Contract). The deterministic spine — the Idea Tree, the dispatch
gates, the bypass-proof merge gate, and the wake-time harvest — lives in
the harness (`surogates/arbor/`, `surogates/tools/builtin/arbor.py`,
`surogates/harness/loop_arbor.py`). These skills carry the *judgment*.

## Skills

| Skill | Role | Loaded |
|---|---|---|
| `arbor-research` | Intake: discover repo/eval/splits, measure baseline, emit `/auto-research` | user types `/arbor-research` |
| `arbor-coordinator` | OBSERVE→IDEATE→SELECT→DISPATCH→DECIDE cycle protocol | preloaded on `/auto-research` sessions |
| `arbor-executor` | Implement+evaluate one hypothesis in a worktree; report via `worker_complete` | preloaded on `arbor-executor` task workers |

`arbor-ideate` (the hard-gated four-line-hypothesis drafting skill) is a
v2 addition; until then the coordinator skill carries the ideation rules
inline.

## Provisioning the `arbor-executor` sub-agent

`dispatch_experiments` spawns executors with `agent_def_name="arbor-executor"`.
That AgentDef must resolve through one of the standard sub-agent layers
(`surogates.tools.loader.ResourceLoader.load_agents`): a per-agent Hub
bundle (`agents/arbor-executor/AGENT.md`), an org/user agents bucket file,
or an `agents` DB row. Recommended `AGENT.md`:

```markdown
---
name: arbor-executor
description: >-
  Executor for an autonomous research run (Arbor). Implements and evaluates
  exactly ONE hypothesis inside an isolated git worktree, then reports a
  structured result via worker_complete. Never merges, never touches trunk
  or the held-out test split.
tools: [read_file, write_file, patch, search_files, list_files, terminal, process, worker_block, worker_complete, worker_context]
max_iterations: 80
preloaded_skills: [arbor-executor]
category: research
tags: [research, executor]
---

You are an executor in an autonomous research run. Follow the preloaded
`arbor-executor` skill: implement the one hypothesis in your brief inside
your worktree, evaluate on the dev split, and finish with worker_complete
carrying {node_key, score, insight, result, branch}. Never git merge,
never touch trunk/main/master, never run the held-out test split.
```

The preferred production path is an ops feature pack that publishes this
file into the agent bundle behind a per-agent `research_enabled` flag
(mirroring `deep_research_enabled`); that toggle, with its DB migration,
is an ops-repo deliverable and is intentionally not part of the harness.
