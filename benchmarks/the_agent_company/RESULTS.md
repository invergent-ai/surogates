# TheAgentCompany Results

Graded by upstream's own per-task evaluators (run in upstream's task
images against the live service stack), scored with upstream's
partial-credit formula: per task `0.5 × full + 0.5 × points_ratio`,
averaged as a percentage. Not comparable to the public leaderboard
(different agent scaffold — see README); rows are only comparable to
each other at the same PIN, the same stack state discipline, and the
same served model. "Model served" is the model that actually answered
the calls — check the traces, not the config.

Every counted run records two things: its row, and its
**below-full-completion table** copied from the run's `report.md`.

| Date | Run | Where | Model served | Size | Full | Score |
| --- | --- | --- | --- | --- | --- | --- |

*(no counted runs yet)*

### Smaller runs

Pilots and category probes. Too small to read as a score.

| Date | Run | Model served | Size | Full | Score |
| --- | --- | --- | --- | --- | --- |

## Method notes

- One config change per run. The PIN, the stack reset discipline and
  the served model all define the run — never compare across them.
- Grade in the same environment session as the rollout: many
  checkpoints inspect live service state.
- Ungradable tasks (missing task image, evaluator crash) are excluded
  from the score and listed with reasons.
- Reference points from the public leaderboard (OpenHands scaffold —
  context, not targets): best published score 52.73 with roughly a
  quarter of tasks fully completed; sub-30 for most models.
