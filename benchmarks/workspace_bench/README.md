# Workspace-Bench Benchmark

## Description

Runs [Workspace-Bench-Lite](https://huggingface.co/datasets/Workspace-Bench/Workspace-Bench-Lite)
([Workspace-Bench 1.0](https://arxiv.org/abs/2605.03596) — 100 file-heavy
knowledge-work tasks over documents, spreadsheets, presentations and code,
each graded against fine-grained rubrics) against a Surogate agent, and
scores the produced files with an LLM judge using the upstream verdict
contract.

It is a measurement tool for the harness, not a test of the model — same
philosophy as `benchmarks/gaia`. Workspace tasks exercise exactly the
surface the platform sells: files in a session workspace, sandbox tool
use over them, files back out. A task fails when the harness fails to
act, and the trace says which part gave up.

Scores: [RESULTS.md](RESULTS.md) — append a row **and the failed-task
list** after every counted run.

**The benchmark is a client, not a library.** It talks to the harness
over its HTTP API and never imports `surogates` or `surogate_ops`
(`tests/test_isolation.py` enforces this). Unlike `benchmarks/claweval`
it needs **no tunnel, no MCP registration, and no ops-plane writes**:
task inputs go in through `POST /v1/api/sessions/{id}/workspace/upload`,
the sandbox sees them because the session workspace is mounted at
`/workspace`, and outputs come back through the workspace tree/download
routes. Three HTTP endpoints, zero extra infrastructure.

### How a task runs

1. **Stage.** The task's input files (from its `data_manifest`; the Lite
   drop ships them, no 37 GB workspace archive involved) are uploaded
   into a fresh session's workspace under `workdir/`. Eligibility is
   checked first — a task whose inputs are missing or over the harness
   upload caps is *skipped and reported*, never half-staged.
2. **Roll out.** One prompt: persona + task instruction + the workspace
   conventions (inputs in `workdir/`, outputs to `outputs/`). Stream to
   a terminal state with GAIA's reconnect discipline (300 s server-side
   stream cap, failed sessions never close their stream — poll status).
3. **Collect.** Diff the workspace tree against the uploaded set;
   download every file the agent created, wherever it wrote it. Missing
   expected outputs are recorded, matched by basename.
4. **Judge** (separate command, offline from stored traces). One
   OpenAI-compatible completion per task: rubrics + extracted file
   contents (docx/xlsx/pptx/pdf → text) + a compact action trace for the
   process rubrics. Upstream's verdict contract: per-rubric
   `{index, passed, confidence, evidence}`, insufficient evidence =
   failed, unanswered rubric = failed with explicit evidence.

Our number is **not comparable to the public leaderboard** — upstream
runs agents in its own Docker harness over full 20 GB workspaces and
judges with an agentic filesystem judge; here the surogates harness runs
the loop over the Lite per-task file sets and the judge reads extracted
content inline. Same tasks, same rubrics, different scaffold — by
design: deltas between *our* runs are the product.

### Layout

| File | Responsibility |
| --- | --- |
| `dataset.py` | Load Lite EN from HF (ungated); frozen stratified 70/30 split |
| `staging.py` | Manifest → upload plan; eligibility gate (caps, traversal) |
| `client.py` | Async harness API client. HTTP only, no business logic |
| `runner.py` | One task to one rollout; upload, stream, collect outputs |
| `extract.py` | Judge-side text extraction (docx/xlsx/pptx/pdf/code) |
| `judge.py` | Rubric judging with upstream's verdict contract |
| `report.py` | Rubric Pass Rate, per-difficulty accuracy, Pass@50/60/80 |
| `cli.py` | `run` / `judge` / `report` |

Each run writes `runs/<run_id>/`: `rollout.json`, then per-task
`events.jsonl`, `meta.json`, `trajectory.md`, collected `outputs/`, and
after judging `scores.json` per task plus `outcomes.json` at the root.
All analysis reads those stored traces — you never re-run a task to ask
a new question of it. `runs/` is gitignored: local trace evidence, not
repo content.

### The split

Frozen in `wsbench/splits/lite_en.json` from the 100 EN Lite tasks, seed
20260902, **stratified by (persona, difficulty)** — GAIA's random split
left its holdout skewed easier, which biases the overfitting signal;
this one cannot. `tests/test_dataset.py` re-derives the split from a
committed fixture, so silent drift fails the suite. Iterate on dev;
touch holdout only to report a final number.

| Split | easy | medium | hard | total |
| --- | --- | --- | --- | --- |
| dev | 10 | 37 | 23 | 70 |
| holdout | 4 | 17 | 9 | 30 |

### Metrics (matching the public card)

- **Rubric Pass Rate** — passed rubrics / total rubrics, micro-averaged
  (the headline; 1,850 rubrics across the 100 tasks).
- **Easy/Medium/Hard Rubrics Accuracy** — the same ratio per difficulty.
- **Pass@50/60/80** — fraction of tasks with ≥ that percent of their own
  rubrics passed. Pass@60 is the per-task pass/fail line the failed-task
  list and regression tracking use.

## How to run

Two ways to point the benchmark at an agent. **Against production** is
the default and what RESULTS.md rows come from. **Against a local
harness** is for iterating on harness changes before they ship.

The benchmark drives real agent sessions on prod: the agent's model,
tools, and prompts are whatever that agent is configured with in ops.
The agent under test **must have the sandbox/file tools enabled** —
these are file tasks. Prefer a lean agent without web browsing for
counted runs (task content is local; web access just adds noise).
Nothing runs remotely except the sessions — the orchestrator is local,
so the machine running it must stay awake and online for the duration.

### Requirements

Values, placed in `benchmarks/workspace_bench/.env` (git-ignored). No HF
token needed — the dataset is ungated.

| Variable | What | Where it comes from |
| --- | --- | --- |
| `SUROGATES_SA_TOKEN` | harness `/v1/api/*` auth | minted once inside the prod cluster — see "Minting `SUROGATES_SA_TOKEN`" in `benchmarks/gaia/README.md` (same token works for all benchmarks) |
| `WSBENCH_BASE_URL` | harness API base | prod `https://cloud.surogate.ai` (default `http://localhost:8000`) |
| `WSBENCH_AGENT_ID` | the agent under test | the agent's page in ops/Studio |
| `WSBENCH_JUDGE_BASE_URL` | OpenAI-compatible judge endpoint (judge cmd only) | platform model proxy: `https://api.surogate.ai/proxy/services/_model/<deployed model id>/v1` |
| `WSBENCH_JUDGE_KEY` / `WSBENCH_JUDGE_MODEL` | judge credentials / model id | the `sk-agent` key vaulted for that model at deploy time (model defaults to `claude-sonnet-5`) |

`WSBENCH_BASE_URL` / `WSBENCH_AGENT_ID` keep their own prefix on
purpose — `SUROGATES_API_URL` and `SUROGATES_AGENT_ID` are real harness
variables, and reusing them cross-talks when the harness and the
benchmark share a shell.

### Setup

The benchmark keeps its own venv. Never run `uv sync` from the repo root
while working here — it reinstalls the pinned `surogates` wheel over the
local dev install, which is the tree you are trying to measure.

```bash
cd benchmarks/workspace_bench
uv venv .venv --python 3.12
uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/pytest          # offline test suite, no network
```

The first `run` downloads the dataset (~260 MB) into the shared HF cache
and is free afterwards.

### Running

From `benchmarks/workspace_bench/`:

```bash
set -a; source .env; set +a                          # load credentials
.venv/bin/wsbench run --split dev --limit 3          # smoke: staging + rollout + collection
.venv/bin/wsbench judge <run_id> --split dev         # grade stored outputs
.venv/bin/wsbench report <run_id>                    # scores + failed-task list
.venv/bin/wsbench report <run_id> --compare <previous_run_id>
.venv/bin/wsbench run --split dev --tasks 171,276    # re-check specific tasks
```

Run ids auto-increment per sequence: a full-split run is a counted run
and takes the split's sequence (`dev-001`, `dev-002`, …, likewise
`holdout-00x`), while any run filtered by `--limit` or `--tasks` is a
pilot and lands in the `smoke-00x` sequence — the two never shift each
other's numbering. Pass `--run-id` to name a run explicitly.
Default concurrency is 3 parallel sessions; tasks carry a 1800 s
wall-clock cap each, so expect a full 70-task dev run to take roughly
2–4 hours. Judging is separate and re-runnable: `judge` skips tasks that
already have `scores.json` unless `--overwrite` is passed, so a crashed
judging pass resumes for free.

**After every counted run: append a row AND its failed-task list to
[RESULTS.md](RESULTS.md).**

### Against a local harness instead

Two live services — a local ops server and the harness in shared mode
pointed at it (`api` + `worker`; no mcp-proxy needed, unlike claweval).
See "Against a local harness instead" in `benchmarks/gaia/README.md` for
the full environment; then:

```bash
export SUROGATES_SA_TOKEN=<service-account token>
export WSBENCH_BASE_URL=http://localhost:8000
export WSBENCH_AGENT_ID=<agent under test>
```

Note the local Docker sandbox bind-mounts the workspace rather than
FUSE-mounting S3; behaviour is equivalent for this benchmark's purposes.

## Grading

Upstream's judging contract on our transport: strict evaluator system
prompt, per-rubric JSON verdicts, insufficient evidence = failed.
Bridging approximations, all deliberate and visible in the traces:

- Office/PDF outputs are extracted to text for the judge (`extract.py`);
  a file that cannot be extracted is judged as "present but not
  extractable", which fails content rubrics *with a stated reason*.
- Process rubrics are judged against the session's action trace
  (tool calls + results), not upstream's container snapshot.
- A rollout that produced no files and no events is scored locally as
  all-failed (with the rollout error as evidence) instead of paying for
  a judge call that can only say "no evidence".
- A judge crash marks the task `judge_error` and scores it 0 — visible
  in the report's Run health section; re-judge with `--overwrite` before
  comparing runs.

## Discipline

- **Failed tasks are the product.** The report's failed-task table goes
  into RESULTS.md verbatim; score movements without trace evidence are
  noise.
- **Single runs are noisy.** Both the agent and the judge are
  stochastic. Compare means of ≥3 runs; `--tasks` is a fast filter,
  never the basis for a claim. One config change per run.
- **Iterate on dev; touch holdout only to report a final number.**
- **Pin the model.** Sessions run under the `surogate` / `surogate-pro`
  tier sentinels, which resolve per request. Check the served model in
  the traces before attributing a delta to harness changes — GAIA's
  RESULTS.md records a run where this bit.
- **Pin the judge.** A judge model change re-baselines every number.
  Record the judge model with every row (it is stored in each task's
  `scores.json`); when it changes, re-judge the comparison run too —
  judging is cheap, rollouts are not.
- **Never delete run folders** — the run-id sequence takes the highest
  existing number per prefix (stray files cannot shift it, unlike
  GAIA's), but deleting the newest run frees its id for silent reuse.
  Summarize dead runs in RESULTS.md instead.
