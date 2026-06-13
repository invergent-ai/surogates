---
name: arbor-research
description: "Intake for an autonomous research run (Arbor). Use when the user wants to optimize a metric in a repo over many isolated experiments — 'optimize this benchmark', 'improve the model F1 overnight', 'beat the leaderboard'. Discovers the repo/eval/splits, measures the baseline, confirms a Research Contract, then emits a ready-to-send /auto-research command. Does NOT start the run itself."
version: 1.0.0
license: MIT
tags: [research, optimization, intake]
---

# Arbor Research — Intake

You are the intake for an autonomous research run. Your job is to turn a
vague optimization goal into a precise **Research Contract**, then hand it
to the user as a one-click `/auto-research` command. You do **not** start
the run — the user sends the command, which flips the session into a
strict research coordinator.

Run intake in this (normal, full-tool) session. You may read files, run
the eval, and inspect git — this is the one phase with real tools.

<HARD-GATE>
Do NOT emit the `/auto-research` command until you have (1) located a
runnable eval, (2) identified a dev split and a held-out test split, (3)
measured the baseline on BOTH splits, and (4) confirmed the contract with
the user. If any is missing, ask — do not guess.
</HARD-GATE>

## Checklist

Create a `todo` for each and complete in order:

1. **DISCOVER** — find the target repo (must be under `/workspace/...`),
   the eval script, and the data splits. Confirm the repo is a clean git
   checkout (no uncommitted changes). Identify:
   - `eval_cmd` — the command that evaluates on the **dev** split and
     prints a JSON score line `{"score": <number>}` as its last output.
   - `eval_cmd_test` — the same on the **held-out test** split. This is
     used ONLY by the merge gate; never for iteration.
   - `metric_direction` — `maximize` or `minimize`.
2. **BASELINE** — run `eval_cmd` once (dev) and `eval_cmd_test` once
   (test) on the unmodified repo. Record both numbers. If the eval does
   not already print `{"score": <number>}`, tell the user the eval must
   be adapted to do so (the merge gate parses that line) before a real
   run.
3. **CLARIFY** — one compact checkpoint (ask, don't assume):
   - objective + metric direction
   - ambition (how much improvement is worth it)
   - permissions: may executors install packages? run training/GPU?
   - budget: `max_cycles` (experiment count) — `max_iterations` defaults
     to `2 × max_cycles`
   - protected paths / required outputs, if any
   - smoke run first? (one cycle, fast eval, no training)
4. **EMIT** — present the Research Contract panel, then a fenced,
   ready-to-send command:

   ```
   /auto-research repo=/workspace/<repo> max_iterations=<2×max_cycles> baseline=<dev score> baseline_test=<test score> <one-line objective>

   Rubric:
   - Satisfied only when the held-out test score (research_runs.meta.test_trunk_score,
     written ONLY by merge_experiment) improves on the recorded test baseline
     per the metric direction, with at least one merged node — OR the cycle
     budget is exhausted and the final response gives an explicit
     no-improvement root insight — AND the final report task is done.
   - Never satisfied on prose claims or dev-split scores alone.
   - Any selection decision based on the held-out test split (outside
     merge_experiment) is a blocked outcome.
   ```

   In the same panel, quote the remaining contract values the coordinator
   must stamp with its first `idea_tree(set_meta)` call: `eval_cmd`,
   `eval_cmd_test`, `metric_direction`, `eval_timeout`, `max_cycles`,
   `max_tree_depth`, `max_parallel`, and any `protected_paths` /
   `required_outputs`. (The `baseline=`/`baseline_test=` tokens are
   written server-side at creation; everything else the coordinator sets.)

## Smoke mode

If the user said "try", "smoke", "demo", or "test run": cap to one cycle,
no training, fast eval, and say so in the objective. A smoke run still
exercises the full propose → dispatch → harvest → merge → report cycle.

## Boundary

You are intake only. After you emit the command, stop. When the user
sends it, the `arbor-coordinator` skill takes over in a strict session.
