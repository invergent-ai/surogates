# DABstep Results

Scored against the frozen dev split (100 tasks: 16 easy / 84 hard)
unless the size column says otherwise. "Model served" is the model that
actually served the calls: sessions run under the `surogate` /
`surogate-pro` tier sentinels, and the sentinel is not a model — it
resolves per request, so a run is only comparable to another run that
resolved the same way.

Strict is correct/gradable tasks as graded by the vendored official
scorer against the **derived answer key** (see README — the reference
is scorer-accepted public answers, not the hidden ground truth, so
these numbers are NOT leaderboard-comparable and are only comparable to
each other at the same key). Record the key coverage with every row;
re-score the comparison run whenever the key is rebuilt.

Every counted run records two things: its row, and its **failed-task
list** copied from the run's `report.md` — the failed tasks are the
harness-improvement backlog, and a row without them is just a number.

| Date | Run | Where | Model served | Size | Strict | Score |
| --- | --- | --- | --- | --- | --- | --- |

*(no counted runs yet — the first full dev run appends here)*

### Smaller runs

Pilots, regression probes and single-task checks. Too small to read as
a score — they exist to isolate one behaviour.

| Date | Run | Model served | Size | Strict | Score |
| --- | --- | --- | --- | --- | --- |

## Method notes

- One config change per run, recorded in the row's notes.
- Single-run deltas are provisional; re-run an affected subset 3×
  before believing a fix.
- Scoring is offline and free — when in doubt, re-score; never re-run
  sessions just to re-grade.
- Key state at benchmark creation (2026-09-06): 450/450 tasks covered,
  444 with ≥3 independent agreeing submissions.
- Reference points from the public leaderboard (official hidden-GT
  grading — context, not targets): multiple agents at 100% easy and
  ~99–100% hard; the interesting signal for us is therefore the failure
  *shape*, not distance to 100%.
