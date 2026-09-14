# AutomationBench Results

Scored with upstream's assertion rubric over the final simulated-world
state: Strict is tasks where **every** assertion passed (the official
pass-rate signal); partial credit (fraction of assertions satisfied)
rides along. Deterministic — no judge. Not comparable to the official
leaderboard (different scaffold, and the official set is private — see
README); rows are only comparable to each other at the same PIN and
served model. "Model served" is the model that actually answered the
calls — check the traces, not the config.

Every counted run records two things: its row, and its **failed-task
list** copied from the run's `report.md`.

| Date | Run | Where | Model served | Size | Strict | Score |
| --- | --- | --- | --- | --- | --- | --- |

*(no counted runs yet)*

### Smaller runs

Pilots and domain probes (`simple` domain included here only). Too
small to read as a score.

| Date | Run | Model served | Size | Strict | Score |
| --- | --- | --- | --- | --- | --- |

## Method notes

- One config change per run, recorded in the row's notes. The PIN
  defines worlds and assertions — never compare across pins.
- The public set is directional for Zapier's private leaderboard set at
  best; our numbers are for harness iteration only.
- Reference points (upstream scaffold, public set — context, not
  targets): frontier models score under 10% strict; partial credit is
  the readable signal at that level.
