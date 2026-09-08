# EnterpriseOps-Gym Results

Scored on upstream's Task Success Rate: a task passes when **every one
of its hidden SQL verifiers** confirms the final database state.
Deterministic — no judge. Not comparable to the public leaderboard
(different scaffold by design — see README); rows are only comparable
to each other at the same PIN and the same served model. "Model served"
is the model that actually answered the calls — tier sentinels resolve
per request, so check the traces, not the config.

Every counted run records two things: its row, and its **failed-task
list** (with failing verifier names) copied from the run's `report.md`.

| Date | Run | Where | Model served | Size | Strict | Score |
| --- | --- | --- | --- | --- | --- | --- |

*(no counted runs yet)*

### Smaller runs

Pilots, domain probes and pipeline checks. Too small to read as a score.

| Date | Run | Model served | Size | Strict | Score |
| --- | --- | --- | --- | --- | --- |

## Method notes

- One config change per run, recorded in the row's notes. The PIN and
  the task set (`EOG_TASKS_DIR`) both define the benchmark — never
  compare across them.
- The default task set is the vendored public sample (13 tasks, all
  `database_state`-verified); the full 1,150-task HF set slots into the
  same layout when scale is wanted.
- Reference points from the public leaderboard (upstream scaffold —
  context, not targets): Claude Opus 4.6 leads at 44.6% Task Success
  Rate; sub-50% across the board.
