# AppWorld Results

Scored on upstream's Task Goal Completion: a task passes when its full
stateful test suite passes over the final app databases. Deterministic
— no judge. Not comparable to the public leaderboard (different
scaffold — MCP tool surface instead of upstream's code REPL; see
README); rows are only comparable to each other at the same `appworld`
package version and the same served model. "Model served" is the model
that actually answered the calls — check the traces, not the config.

Every counted run records two things: its row, and its **failed-task
list** copied from the run's `report.md`.

| Date | Run | Where | Model served | Size | Strict | Score |
| --- | --- | --- | --- | --- | --- | --- |

*(no counted runs yet)*

### Smaller runs

Pilots and pipeline checks (dev split, `--limit`, `--tasks`). Too small
to read as a score.

| Date | Run | Model served | Size | Strict | Score |
| --- | --- | --- | --- | --- | --- |

## Method notes

- One config change per run, recorded in the row's notes. The
  `appworld` version pin defines the environment and the tests — never
  compare across versions.
- Splits are upstream's; iterate on `dev`, count `test_normal`, and
  touch `test_challenge` only for a final number.
- Reference points from upstream's leaderboard (code-REPL scaffold —
  context, not targets): best published agents complete well under half
  of test_normal task goals; test_challenge is much harder.
