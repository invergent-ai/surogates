# Galileo Agent Leaderboard Results

Scored against the frozen dev split (100 scenarios, 20 per domain)
unless the size column says otherwise. "Model served" is the model that
actually served the calls: sessions run under the `surogate` /
`surogate-pro` tier sentinels, and the sentinel is not a model — it
resolves per request, so a run is only comparable to another run that
resolved the same way. Record the sim/judge deployment too — user
simulator, tool simulator and judge share one endpoint, and changing it
re-baselines every number.

AC is Action Completion (macro average of per-scenario goal-completion
fractions, judged from tool-evidenced transcript); TSQ is Tool
Selection Quality (fraction of good tool calls). Both are our judge's
reimplementation of Galileo's published metric definitions — **not
leaderboard-comparable**, only comparable across our own runs.

Every counted run records two things: its row, and its
**failed-scenario list** (AC below 0.5) copied from the run's
`report.md` — the failed scenarios are the harness-improvement backlog,
and a row without them is just a number.

| Date | Run | Where | Model served | Size | AC | TSQ |
| --- | --- | --- | --- | --- | --- | --- |

*(no counted runs yet — the first full dev run appends here)*

### Smaller runs

Pilots, domain probes and protocol checks. Too small to read as a
score — they exist to isolate one behaviour.

| Date | Run | Model served | Size | AC | TSQ |
| --- | --- | --- | --- | --- | --- |

## Method notes

- One config change per run, recorded in the row's notes.
- Single-run deltas are provisional; re-run an affected subset 3×
  before believing a fix.
- Judging is decoupled and resumable (`--overwrite` to re-judge);
  re-judge the comparison run too when the judge deployment changes.
- Reference points from the public leaderboard (Galileo's own judge —
  context, not targets): top entry gpt-4.1 at AC 0.62 / TSQ 0.80; AC
  around 0.5–0.6 is the competitive band, and nobody is near 1.0.
