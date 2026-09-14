# AutomationBench Benchmark

## Description

Runs [AutomationBench](https://github.com/zapier/AutomationBench)
([paper](https://arxiv.org/abs/2604.18934) — Zapier's benchmark of 600
public business-workflow tasks across sales, marketing, operations,
support, finance and HR, executed against 40+ simulated SaaS apps and
graded programmatically on final state) against a Surogate agent,
scored with **upstream's own assertion rubric**. Deterministic — no
judge.

It is a measurement tool for the harness, not a test of the model.
Upstream's runner owns its own agent loop over a bare LLM endpoint;
here the surogates harness runs the loop, and the orchestrator drives
upstream's environment library directly — same worlds, same tools,
same rubric (`tests` pin the seams against the vendored checkout). The
number is therefore **not comparable to the official leaderboard**
(which also uses a held-out private task set) — different scaffold by
design.

Scores: [RESULTS.md](RESULTS.md) — append a row **and the failed-task
list** after every counted run.

**The benchmark is a client, not a library.** It talks to the harness
over its HTTP API and never imports `surogates` or `surogate_ops`
(enforced in tests). No tunnel, no MCP, no ops-plane writes: the
simulated world lives in the orchestrator's process.

### How a task runs

1. **World.** A fresh ``WorldState`` from the task's None-stripped
   initial state, with ``allowed_services`` computed exactly as
   upstream's ``setup_state`` does.
2. **Roll out.** One multi-turn session speaking the fenced
   ```` ```tool_call ```` protocol (the galileo benchmark's proven
   bridge): the first message carries the task's own system prompt
   verbatim, the ``api`` toolset catalog (endpoint discovery via
   ``api_search`` + generic ``api_fetch``), and the calling format.
   After each agent turn the orchestrator executes the emitted calls
   against the world and replies with TOOL RESULTS; the episode ends on
   ``TASK_COMPLETE``, a turn without tool calls, or the 50-message cap
   (upstream's max-steps analogue).
3. **Score** (in-process, immediately). Upstream's
   ``rubric.partial_credit`` (fraction of assertions satisfied) and
   ``task_completed_correctly`` (strict pass — the official pass-rate
   signal) over the final world, which is also dumped into the trace
   for offline re-scoring.

### Layout

| File | Responsibility |
| --- | --- |
| `bridge.py` | Library seams: tasks, world, tool dispatch, rubric |
| `protocol.py` | Preamble, tool_call parsing, TOOL RESULTS formatting |
| `client.py` | Async harness API client. HTTP only, no business logic |
| `runner.py` | Multi-turn tool-round loop; world dump; trace capture |
| `report.py` | Strict pass rate + partial credit, per domain |
| `cli.py` | `run` / `report` |

## How to run

**Use a lean, chat-only agent** — the SaaS apps are simulated by the
orchestrator, so any real harness tool (web, sandbox) is pure
contamination.

### Requirements

Values, placed in `benchmarks/automationbench/.env` (git-ignored):

| Variable | What |
| --- | --- |
| `SUROGATES_SA_TOKEN` | harness `/v1/api/*` auth (same token as the siblings) |
| `AB_BASE_URL` | prod `https://cloud.surogate.ai` (default `http://localhost:8000`) |
| `AB_AGENT_ID` | agent under test — lean, chat-only |

### Setup

Python **3.13** (upstream requires it). The vendored checkout is pinned
by `PIN`; `verifiers` is pinned to the version upstream's lockfile uses
(uv otherwise resolves a dev pre-release with a different layout).

```bash
cd benchmarks/automationbench
git clone https://github.com/zapier/AutomationBench.git vendor/AutomationBench
git -C vendor/AutomationBench checkout "$(cat PIN)"   # audited upstream commit

uv venv .venv --python 3.13
uv pip install --python .venv/bin/python -e ".[dev]" -e ./vendor/AutomationBench
.venv/bin/pytest          # offline suite + live library seam tests
```

### Running

From `benchmarks/automationbench/`:

```bash
set -a; source .env; set +a
.venv/bin/abbench run --domains simple --limit 2   # smoke: bridge + rubric
.venv/bin/abbench run --domains sales --limit 5    # domain probe
.venv/bin/abbench run                              # counted: 600 public tasks
.venv/bin/abbench report <run_id>
```

Run ids auto-increment per sequence: the unfiltered six-domain run is
counted (`full-00x`); anything filtered by `--domains`, `--limit` or
`--tasks` is a pilot in `smoke-00x`. The `simple` domain (200 basic
tasks) is upstream's tool-use baseline and never part of the score. A
full run is 600 multi-turn sessions — pilot first and extrapolate cost.
Frontier models score **below 10%** on this benchmark; expect a low
number, and read the failed-task list rather than the headline.

**After every counted run: append a row AND its failed-task list to
[RESULTS.md](RESULTS.md).**

## Discipline

- **Failed tasks are the product** — partial credit per failed task
  (with the first failure signal) goes into RESULTS.md verbatim.
- **Single runs are noisy**; compare means, one config change per run.
- **Pin the model** — check the served model in the traces; **pin the
  checkout** — assertions at another commit grade differently.
- **Never delete run folders** — the id sequence takes the highest
  existing number per prefix. Summarize dead runs in RESULTS.md instead.
