# DABstep Benchmark

## Description

Runs [DABstep](https://huggingface.co/spaces/adyen/DABstep) (Adyen's
Data Agent Benchmark for Multi-step Reasoning — 450 data-analysis tasks
over a fixed payments-domain corpus: structured CSVs/JSON plus an
unstructured `manual.md` that defines every domain concept) against a
Surogate agent, and scores answers with the **vendored official
scorer**.

It is a measurement tool for the harness, not a test of the model —
same philosophy as `benchmarks/gaia`, and the closest to it in shape:
question in, exact-format answer out. DABstep tasks need real multi-step
data work (the sandbox mounts the corpus as workspace files), so a task
fails when the harness fails to act, and the trace says which part gave
up.

Scores: [RESULTS.md](RESULTS.md) — append a row **and the failed-task
list** after every counted run.

**The benchmark is a client, not a library.** It talks to the harness
over its HTTP API and never imports `surogates` or `surogate_ops`
(`tests/test_isolation.py` enforces this). No tunnel, no MCP
registration, no ops-plane writes, no LLM judge: context goes in over
`workspace/upload`, the answer comes back in the final message, grading
is the official scorer against a local key.

### The answer key (read this before quoting numbers)

Upstream withholds ground truth for the 450 tasks — official grading
happens on the leaderboard. The same public dataset, however, publishes
every submission's per-task grading (`task_scores`: task id, the
submitted answer, and whether the official scorer accepted it).
`dabbench build-key` derives a local key from that: per task, the
**modal answer among all scorer-accepted rows**, overridden by the 10
public dev ground truths. Current coverage: **450/450**, 444 tasks with
≥3 independent agreeing submissions.

Consequences, stated plainly:

- Local grading is deterministic, free and instantly repeatable —
  ideal for harness iteration, which is this benchmark's purpose.
- The reference is *scorer-accepted answers*, not the hidden truth, so
  a borderline-tolerance answer can grade differently than the
  leaderboard would. Our numbers are **not leaderboard-comparable** and
  must never be quoted as DABstep scores.
- For an official number, `dabbench export <run_id>` writes
  `submission.jsonl` in the leaderboard format
  (`{"task_id", "agent_answer"}` lines) for manual submission.
- The key is derived data and git-ignored; rebuild it any time. A run
  is only comparable to another run graded with the same key.

### Layout

| File | Responsibility |
| --- | --- |
| `dataset.py` | Load tasks + context from HF (ungated); frozen 100/350 split |
| `answers.py` | Derive + load the local answer key from public task_scores |
| `official_scorer.py` | Upstream scorer, vendored verbatim (do not edit) |
| `client.py` | Async harness API client. HTTP only, no business logic |
| `runner.py` | One task to one session; context upload, FINAL ANSWER extraction |
| `report.py` | Accuracy overall/easy/hard, ungradable accounting, regressions |
| `cli.py` | `build-key` / `run` / `score` / `report` / `export` |

Each run writes `runs/<run_id>/`: `rollout.json` plus per-task
`events.jsonl` and `meta.json`, then `outcomes.json` and `report.md`
after scoring. `runs/` is gitignored: local trace evidence, not repo
content.

### The split

Frozen in `dabbench/splits/tasks_v1.json` from the 450 tasks, seed
20260906, stratified by level: **dev 100** (16 easy / 84 hard) /
**holdout 350** (56 easy / 294 hard). `tests/test_dataset.py` re-derives
it from a committed fixture, so silent drift fails the suite. Iterate on
dev; touch holdout only to report a final number. The 10-task
`upstream-dev` split (public answers, true ground truth) is for smokes.

## How to run

**Against production** is the default. The agent under test **must have
the sandbox/terminal tools enabled** — these are pandas-over-CSV tasks
(payments.csv is 23 MB / ~138k rows; eyeballing it does not work).
Prefer a lean agent without web browsing; everything needed is in the
workspace. Nothing runs remotely except the sessions — the orchestrator
is local, so the machine must stay awake for the duration.

### Requirements

Values, placed in `benchmarks/dabstep/.env` (git-ignored). The dataset
is ungated — no HF token needed.

| Variable | What | Where it comes from |
| --- | --- | --- |
| `SUROGATES_SA_TOKEN` | harness `/v1/api/*` auth | minted once inside the prod cluster — see "Minting `SUROGATES_SA_TOKEN`" in `benchmarks/gaia/README.md` (same token works for all benchmarks) |
| `DABSTEP_BASE_URL` | harness API base | prod `https://cloud.surogate.ai` (default `http://localhost:8000`) |
| `DABSTEP_AGENT_ID` | the agent under test | the agent's page in ops/Studio |

### Setup

The benchmark keeps its own venv. Never run `uv sync` from the repo
root while working here — it reinstalls the pinned `surogates` wheel
over the local dev install.

```bash
cd benchmarks/dabstep
uv venv .venv --python 3.12
uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/pytest                  # offline suite, no network
.venv/bin/dabbench build-key      # one-time; downloads public task_scores
```

### Running

From `benchmarks/dabstep/`:

```bash
set -a; source .env; set +a                            # load credentials
.venv/bin/dabbench run --split upstream-dev            # smoke: 10 tasks, true GT
.venv/bin/dabbench run --split dev                     # counted: 100-task dev run
.venv/bin/dabbench score <run_id> --split dev          # grade (offline, free)
.venv/bin/dabbench report <run_id> --compare <previous_run_id>
.venv/bin/dabbench run --split dev --tasks 1712,1810   # re-check specific tasks
.venv/bin/dabbench export <run_id>                     # leaderboard submission file
```

Run ids auto-increment per sequence: full-split runs are counted and
take the split's sequence (`dev-001`, `holdout-00x`, `upstream-dev-00x`),
while `--limit`/`--tasks` pilots land in `smoke-00x` — the sequences
never shift each other. Each task uploads the 24 MB corpus into its own
session (~2.4 GB total upload for a full dev run, sequential-ish at
concurrency 3); the 1800 s wall-clock cap per task matches gaia's.

**After every counted run: append a row AND its failed-task list to
[RESULTS.md](RESULTS.md).**

### Against a local harness instead

Same two services as the siblings (ops server + harness in shared mode;
no mcp-proxy). See "Against a local harness instead" in
`benchmarks/gaia/README.md`, then:

```bash
export SUROGATES_SA_TOKEN=<service-account token>
export DABSTEP_BASE_URL=http://localhost:8000
export DABSTEP_AGENT_ID=<agent under test>
```

## Discipline

- **Failed tasks are the product.** The report's failed-task table goes
  into RESULTS.md verbatim; score movements without trace evidence are
  noise.
- **Single runs are noisy.** Compare means of ≥3 runs; `--tasks` is a
  fast filter, never the basis for a claim. One config change per run.
- **Iterate on dev; touch holdout only to report a final number.**
- **Pin the model.** Sessions run under the tier sentinels; check the
  served model in the traces before attributing a delta.
- **Pin the key.** Rebuilding the key after new public submissions can
  change borderline references; record key coverage with every row and
  re-score the comparison run when the key changes (scoring is free).
- **Never delete run folders** — the id sequence takes the highest
  existing number per prefix; deleting the newest frees its id for
  silent reuse. Summarize dead runs in RESULTS.md instead.
