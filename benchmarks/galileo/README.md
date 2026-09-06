# Galileo Agent Leaderboard Benchmark

## Description

Runs [Agent Leaderboard v2](https://huggingface.co/datasets/galileo-ai/agent-leaderboard-v2)
(Galileo — 500 multi-turn tool-calling scenarios across banking,
healthcare, insurance, investment and telecom: a persona pursues 6–8
interconnected goals against a 20-tool domain catalog) against a
Surogate agent, replicating upstream's simulation pipeline locally and
scoring **Action Completion (AC)** and **Tool Selection Quality (TSQ)**
with an LLM judge.

It is a measurement tool for the harness, not a test of the model —
same philosophy as the sibling benchmarks. Upstream binds tools
natively into its own agent loop and scores with Galileo's closed
platform metrics; here the surogates harness runs the loop over its
real chat surface, and AC/TSQ are reimplemented from their published
definitions. The number is therefore **not comparable to the public
leaderboard** — different scaffold and different judge by design; our
runs are only comparable to each other.

Scores: [RESULTS.md](RESULTS.md) — append a row **and the failed-scenario
list** after every counted run.

**The benchmark is a client, not a library.** It talks to the harness
over its HTTP API and never imports `surogates` or `surogate_ops`
(`tests/test_isolation.py` enforces this). No tunnel, no MCP
registration, no ops-plane writes: the orchestrator is local and drives
plain multi-turn sessions.

### How a scenario runs

1. **Open.** A fresh session gets one message: upstream's agent system
   prompt (verbatim, including the `CONVERSATION_COMPLETE` convention),
   the domain's 20-tool catalog as JSON schemas, the fenced
   ```` ```tool_call ```` calling format, and the persona's first
   message.
2. **Loop.** After each agent turn the orchestrator either (a) answers
   `tool_call` blocks with schema-conforming responses from the **tool
   simulator** (upstream's prompt, verbatim) sent back as a TOOL
   RESULTS message, or (b) lets the **user simulator** (upstream's
   prompt, verbatim — persona + remaining goals) take the next user
   turn. Caps: 5 user turns (upstream's `MAX_TURNS`) and 16 agent
   messages as a loop guard. Malformed tool_call blocks come back as
   explicit errors, mirroring what native binding would have rejected.
3. **Judge** (separate command, offline from stored transcripts). Two
   structured completions per scenario: AC — for every user goal, was
   the action actually performed with tool evidence (explanations and
   unconfirmed claims don't count); TSQ — for every tool call, right
   tool, right arguments, right moment. Unanswered items are failed
   with explicit evidence, never dropped.

**Scaffold differences, stated plainly:** tools reach the agent as a
text protocol instead of native binding (the agent under test also has
its own system prompt underneath ours); user and tool simulators run on
one endpoint at temperature 0 where upstream used gpt-4.1 /
gpt-4.1-mini; AC/TSQ are our judge prompts, not Galileo's metrics.
Constant within our runs — which is what harness measurement needs.

### Layout

| File | Responsibility |
| --- | --- |
| `dataset.py` | Load scenarios/personas/tools from HF (ungated); frozen 100/400 split |
| `protocol.py` | Preamble, tool_call parsing, TOOL RESULTS formatting |
| `sim.py` | User + tool simulators (upstream prompts, verbatim) |
| `client.py` | Async harness API client. HTTP only, no business logic |
| `runner.py` | One scenario to one multi-turn session; transcript capture |
| `judge.py` | AC/TSQ structured judging from stored transcripts |
| `report.py` | AC/TSQ per domain, turn counts, failed-scenario list |
| `cli.py` | `run` / `judge` / `report` |

Each run writes `runs/<run_id>/`: `rollout.json` plus per-scenario
`events.jsonl` and `meta.json` (full transcript + tool calls), then
`scores.json` per scenario and `outcomes.json` after judging. `runs/`
is gitignored: local trace evidence, not repo content.

### The split

Frozen in `galbench/splits/v2.json`: **dev 100** (20 per domain) /
**holdout 400**, seed 20260906. `tests/test_dataset.py` re-derives it
from a committed fixture, so silent drift fails the suite. Iterate on
dev; touch holdout only to report a final number.

## How to run

**Against production** is the default. Use a **lean, chat-only agent**:
the scenario's tools are simulated by the orchestrator, so any real
harness tool (web, sandbox) is pure contamination — claweval's
`general-004` documented exactly that failure. Nothing runs remotely
except the sessions; the machine must stay awake for the duration.

### Requirements

Values, placed in `benchmarks/galileo/.env` (git-ignored). The dataset
is ungated — no HF token needed.

| Variable | What | Where it comes from |
| --- | --- | --- |
| `SUROGATES_SA_TOKEN` | harness `/v1/api/*` auth | minted once inside the prod cluster — see benchmarks/gaia/README.md (same token works for all benchmarks) |
| `GALILEO_BASE_URL` | harness API base | prod `https://cloud.surogate.ai` (default `http://localhost:8000`) |
| `GALILEO_AGENT_ID` | the agent under test | the agent's page in ops/Studio |
| `GALILEO_SIM_BASE_URL` | OpenAI-compatible endpoint for user-sim, tool-sim AND judge | platform model proxy: `https://api.surogate.ai/proxy/services/_model/<deployed model id>/v1` |
| `GALILEO_SIM_KEY` / `GALILEO_SIM_MODEL` | its key / model id | the `sk-agent` key vaulted for that deployment |

**Token budget — read before a counted run.** Every scenario spends
simulator calls (one per user turn, one per tool call — typically
10–20 small completions) plus two judge calls. A full 100-scenario dev
run is roughly 1,500–2,500 completions on the sim endpoint. Smoke with
`--limit 2` first and extrapolate from the deployment's usage page.

### Setup

The benchmark keeps its own venv. Never run `uv sync` from the repo
root while working here.

```bash
cd benchmarks/galileo
uv venv .venv --python 3.12
uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/pytest          # offline suite, no network
```

### Running

From `benchmarks/galileo/`:

```bash
set -a; source .env; set +a                              # load credentials
.venv/bin/galbench run --split dev --limit 2             # smoke: protocol + sims
.venv/bin/galbench run --split dev                       # counted: 100-scenario dev run
.venv/bin/galbench judge <run_id> --split dev            # AC/TSQ grading
.venv/bin/galbench report <run_id> --compare <previous_run_id>
.venv/bin/galbench run --split dev --domains banking     # domain probe (smoke sequence)
.venv/bin/galbench run --split dev --scenarios banking-009
```

Run ids auto-increment per sequence: full-split runs are counted
(`dev-001`, `holdout-00x`); anything filtered by `--limit`,
`--scenarios` or a `--domains` subset is a pilot in `smoke-00x`.
Judging skips scenarios that already have `scores.json` unless
`--overwrite` is passed, so a crashed judging pass resumes for free.
Default concurrency is 2 — scenarios are long multi-turn sessions and
the tier's rate-limit behaviour under sustained load is a documented
failure mode.

**After every counted run: append a row AND its failed-scenario list to
[RESULTS.md](RESULTS.md).**

### Against a local harness instead

Same two services as the siblings (ops server + harness in shared
mode; no mcp-proxy). See "Against a local harness instead" in
`benchmarks/gaia/README.md`, then set `GALILEO_BASE_URL=http://localhost:8000`
with the same remaining variables.

## Discipline

- **Failed scenarios are the product.** The report's failed-scenario
  table goes into RESULTS.md verbatim; score movements without trace
  evidence are noise.
- **Single runs are noisy** — agent, both simulators and the judge are
  all stochastic surfaces even at temperature 0. Compare means of ≥3
  runs; one config change per run.
- **Iterate on dev; touch holdout only to report a final number.**
- **Pin the model** — check the served model in the traces, and record
  the sim/judge deployment with every row; changing either re-baselines
  the numbers.
- **Never delete run folders** — the id sequence takes the highest
  existing number per prefix. Summarize dead runs in RESULTS.md instead.
