# Agents' Last Exam Results

Graded by each task's own deterministic grader against the gated
reference data; scores are task-local point scales, so rows report
counts (graded, positive-scoring) with the per-task table attached.
Not comparable to the public leaderboard (different scaffold — our
session sandbox instead of upstream's VM snapshots; see README); rows
are only comparable to each other at the same PIN and served model.
"Model served" is the model that actually answered the calls — check
the traces, not the config.

Every counted run records two things: its row, and its **per-task
score table** copied from the run's `report.md`.

| Date | Run | Where | Model served | Size | Graded | Positive |
| --- | --- | --- | --- | --- | --- | --- |

*(no counted runs yet)*

### Smaller runs

Pilots and pipeline checks. Too small to read as a score.

| Date | Run | Model served | Size | Graded | Positive |
| --- | --- | --- | --- | --- | --- |

## Method notes

- One config change per run, recorded in the row's notes. The PIN
  defines the graders — never compare across pins.
- Ungradable tasks (grader deps missing in this venv) are excluded from
  counts, listed with reasons; install the domain packages and
  re-grade — rollouts are never re-run for grading.
- Eligibility at benchmark creation (2026-09-08, pin `d10fb61a`):
  165 public tasks discovered; 25 lack grader scripts, 8 demo cards
  lack prompts — reported, not counted. The rest need the gated data
  archive fetched locally.
