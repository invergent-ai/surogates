# Agents' Last Exam Benchmark

## Description

Runs [Agents' Last Exam](https://agents-last-exam.org/)
([rdi-berkeley/agents-last-exam](https://github.com/rdi-berkeley/agents-last-exam)
— Berkeley RDI's expert-sourced benchmark of long-horizon professional
workflows across 55 subdomains; ~165 public tasks in the pinned
checkout, each with a deterministic grader and hidden reference
outputs) against a Surogate agent, graded by **each task's own grader
script** against the reference data. No judge.

It is a measurement tool for the harness, not a test of the model.
Upstream provisions cua_bench VM snapshots (including Windows images)
per task; here **the platform's own session sandbox is the OS sandbox**
— workspace-bench shape: inputs staged into the workspace, the agent
works with its real tools, outputs collected back over the API. Tasks
whose fidelity depends on the exact upstream VM (Windows-only software,
GUI work) will underperform here by construction; the number is **not
comparable to the public leaderboard** — different scaffold by design.

Scores: [RESULTS.md](RESULTS.md) — append a row **and the per-task
score table** after every counted run.

**The benchmark is a client, not a library.** It talks to the harness
over its HTTP API and never imports `surogates` or `surogate_ops`
(enforced in tests). No tunnel, no MCP, no ops-plane writes.

### How a task runs

1. **Stage.** The task's `input/` tree (from the gated data archive) is
   uploaded into a fresh session's workspace. The eligibility gate
   skips-and-reports tasks whose data is absent or over the upload
   caps, tasks without a grader, and cards without a prompt — never
   silently.
2. **Roll out.** One message: the task card's prompt, its graded
   must-do list, and the workspace conventions (inputs in `input/`,
   outputs to `output/`, pip-install what's missing). Streamed to a
   terminal state with the sibling benchmarks' reconnect discipline.
3. **Collect.** Everything the agent wrote under `output/` comes back
   over the workspace API into the run's `pred/` directory.
4. **Grade** (offline, free, repeatable). The task's own grader script
   runs locally: `python scripts/<grader>.py --pred-dir … --gt-dir …`,
   producing a JSON report whose `total_score` is recorded. Graders
   import domain libraries (each card's `software` list); one that
   cannot run here marks the task **ungradable with the reason** —
   install the domain packages into this venv and re-grade, rollouts
   are never re-run.

### Layout

| File | Responsibility |
| --- | --- |
| `vendor.py` | Pinned checkout (`PIN`) + data archive location |
| `dataset.py` | Task discovery from task cards; eligibility gate; staging plan |
| `client.py` | Async harness API client. HTTP only, no business logic |
| `runner.py` | Stage → prompt → collect `output/` |
| `grade.py` | Run the task's grader as a subprocess; parse `total_score` |
| `report.py` | Graded/positive counts per domain; per-task score table |
| `cli.py` | `tasks` / `run` / `grade` / `report` |

Scores are task-local point scales, so the report counts graded and
positive-scoring tasks rather than averaging raw points across tasks.

## How to run

### Requirements

Values, placed in `benchmarks/agents_last_exam/.env` (git-ignored):

| Variable | What |
| --- | --- |
| `SUROGATES_SA_TOKEN` | harness `/v1/api/*` auth (same token as the siblings) |
| `ALE_BASE_URL` | prod `https://cloud.surogate.ai` (default `http://localhost:8000`) |
| `ALE_AGENT_ID` | agent under test — **sandbox/terminal capable**, pip allowed |
| `ALE_HOME` / `ALE_DATA_DIR` | optional overrides (checkout / data archive) |

The task **data archive is gated**: request access at
[agents-last-exam-data-archive](https://huggingface.co/datasets/agents-last-exam/agents-last-exam-data-archive),
`huggingface-cli login`, then extract with the vendored script.

### Setup

```bash
cd benchmarks/agents_last_exam
git clone https://github.com/rdi-berkeley/agents-last-exam.git vendor/agents-last-exam
git -C vendor/agents-last-exam checkout "$(cat PIN)"   # audited upstream commit

uv venv .venv --python 3.12
uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/pytest                                       # offline suite

vendor/agents-last-exam/scripts/fetch_task_data.sh task-data   # gated archive
.venv/bin/alebench tasks                               # eligibility per task
```

### Running

From `benchmarks/agents_last_exam/`:

```bash
set -a; source .env; set +a
.venv/bin/alebench run --domains demo --limit 1    # smoke: stage + collect + grade
.venv/bin/alebench run                             # every eligible task
.venv/bin/alebench grade <run_id>                  # offline; re-run after installing deps
.venv/bin/alebench report <run_id>
```

Run ids auto-increment per sequence: unfiltered runs are counted
(`full-00x`); anything filtered by `--domains`, `--limit` or `--tasks`
is a pilot in `smoke-00x`. Default per-task cap is 3600 s — these are
long-horizon tasks; expect real cost on a full run and pilot first.

**After every counted run: append a row AND the per-task score table to
[RESULTS.md](RESULTS.md).**

## Discipline

- **Per-task scores are the product** — the score table with grader
  notes goes into RESULTS.md verbatim; ungradable ≠ zero.
- **Single runs are noisy**; compare means, one config change per run.
- **Pin the model** — check the served model in the traces; **pin the
  checkout** — graders at another commit score differently, and the
  runner refuses a PIN mismatch.
- **Never delete run folders** — the id sequence takes the highest
  existing number per prefix. Summarize dead runs in RESULTS.md instead.
