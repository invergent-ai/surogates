---
name: arbor-merge-discipline
description: "DECIDE-phase doctrine for an Arbor research run: when to merge, prune, combine, or finalize; how the held-out merge gate works; and the rule that the final report uses TEST scores. Load before deciding what to do with completed experiments."
version: 1.0.0
license: MIT
tags: [research, decide, merge, arbor]
---

# Arbor Merge Discipline — DECIDE Doctrine

## When to merge
Call `merge_experiment(action=start, node_key=...)` for a `done` node whose dev
score beats trunk. The tool re-runs the held-out test eval ITSELF (you cannot
pass a score); poll `merge_experiment(action=status, node_key=...)` on a later
turn. A successful merge writes `test_trunk_score` and advances trunk — later
experiments branch from the new HEAD automatically. A refusal (no improvement,
protected-path hit, conflict) is tree evidence, not an error to retry blindly.

## When to prune
`idea_tree(action=prune, node_key=..., reason=<the lesson>)` for dead ends. The
reason is backpropagated up the ancestor chain, so write the transferable lesson
("lr schedules don't help this objective"), not "didn't work".

## Combine (ensemble)
When several diverse nodes each help a different failure class, propose a child
hypothesis that ensembles/blends them. Once single ideas plateau this is often
the highest-leverage move — it is exactly the "Combine" the convergence
intervention suggests.

## Search-scout (related work)
Before merging a validated winner, optionally `delegate_task` a short web search
("related work for <mechanism>"), then record it with
`idea_tree(action=update, node_key=..., fields={"related_work": "<refs>"})`. Run
it async — never block the cycle waiting on it.

## Finalize — the report uses TEST, not dev
On budget exhaustion, a convergence STOP, or hitting the target: merge the best
node, call `idea_tree(action=report)` (held-out test scores are authoritative
there), then spawn ONE report task whose worker creates the artifact from
`/workspace/.arbor/REPORT.md` and completes with metadata `{"report": true}`.
The mission is only satisfied once a machine-written test improvement AND that
report task both exist — prose claims never satisfy it.
