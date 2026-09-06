# E-Commerce Bench (ECBench)

## Description

Runs [E-Commerce Bench](https://ecbench.github.io/)
([QwenLM/E-CommerceBench](https://github.com/QwenLM/E-CommerceBench),
Apache-2.0 — a deterministic year-long e-commerce simulation: the agent
runs an online store for up to 365 simulated days through 18 tools,
negotiating with 576 suppliers of which 152 are fraudulent, and is
scored by end-of-horizon total assets) against a Surogate agent.

It is a measurement tool for the harness, not a test of the model —
same philosophy as the sibling benchmarks. Upstream drives its own
agent loop; here the surogates harness runs the loop, and the whole
simulation runs **inside the session sandbox**: the vendored environment
is uploaded into the session workspace and the agent drives it through
its terminal (`python3 ecsim.py ...`). What gets measured is the
harness's long-horizon agentic loop — terminal tool use, context
management over hundreds of days, plan persistence. The number is
therefore **not comparable to the public ECBench leaderboard** —
different scaffold by design.

Scores: [RESULTS.md](RESULTS.md) — append a row and the per-episode
table after every counted run.

**The benchmark is a client, not a library.** It talks to the harness
over its HTTP API and never imports `surogates` or `surogate_ops`
(`tests/test_isolation.py` enforces this). Like `benchmarks/workspace_bench`
and unlike `benchmarks/claweval` it needs **no tunnel, no MCP
registration, no ops-plane writes, and no LLM judge**: files go in over
`workspace/upload`, the sandbox mounts the workspace, state comes back
over `workspace/download`, and scoring is deterministic arithmetic.

### How an episode runs

1. **Stage.** The pinned upstream `tools/` + `data/` trees (~1.7 MB, 41
   files) and our `ecsim.py` CLI are uploaded into a fresh session's
   workspace.
2. **Roll out.** One prompt: store-proprietor role, the goal (maximize
   total assets), and the `ecsim` protocol. The agent runs
   `python3 ecsim.py init`, lists tools, and acts day by day with
   `ecsim.py call '[{"tool_name": ..., "tool_args": {...}}]'`. Every
   invocation is a fresh process — state pickles to `sim_state.pkl`, so
   there is no daemon and nothing to keep alive.
3. **Settle.** The agent ends with `ecsim.py finalize` (deferred
   returns pipeline + settlement → `final_state.json`). The runner then
   downloads `final_state.json`, `calls.jsonl` (the audited tool
   traffic) and `sim_state.pkl`.
4. **Score** (offline, `ecbench score`). Prefers `final_state.json`;
   when the agent forgot to finalize, the settlement is **recomputed
   locally** from the pickle against the same pinned code — identical
   result, not a zero. Upstream's `snapshot_final_state` is idempotent.

### Determinism and the NPC renderer

Upstream fixes every negotiation outcome in a deterministic kernel and
uses an LLM only to phrase supplier replies. Here the default renderer
is a template that surfaces the kernel's decision verbatim — zero
tokens, zero keys in the sandbox, still deterministic. Setting
upstream's own `GPT_API_KEY` / `GPT_BASE_URL` / `NPC_MODEL` in the
sandbox environment restores LLM-rendered dialogue. This is the one
bridging approximation; it changes the prose the agent reads, never the
prices, accept/reject decisions, demand, or returns (all seeded).

### Layout

| File | Responsibility |
| --- | --- |
| `vendor.py` | Locate + verify the pinned E-CommerceBench checkout (`PIN`) |
| `ecsim.py` | The in-sandbox CLI (uploaded per episode, stdlib-only imports) |
| `staging.py` | Vendored checkout → upload plan (`sim/` + `ecsim.py`) |
| `client.py` | Async harness API client. HTTP only, no business logic |
| `runner.py` | One episode to one session; upload, stream, collect artifacts |
| `scorer.py` | Deterministic settle: final_state.json or local recompute |
| `report.py` | Mean/median assets, multiple of stake, per-episode table |
| `cli.py` | `run` / `score` / `report` |

Each run writes `runs/<run_id>/`: `rollout.json` plus per-episode
`events.jsonl`, `meta.json`, `final_state.json`, `calls.jsonl`,
`sim_state.pkl`, then `outcomes.json` and `report.md`. `runs/` is
gitignored: local trace evidence, not repo content.

## How to run

**Against production** is the default. The agent under test **must have
the sandbox/terminal tools enabled** and its sandbox must be able to
`pip install pandas numpy` (the prompt tells the agent to do so if the
import fails). Prefer a lean agent without web browsing — the whole
business lives in the simulator. Nothing runs remotely except the
sessions; the orchestrator is local, so the machine must stay awake for
the duration.

### Requirements

Values, placed in `benchmarks/ecbench/.env` (git-ignored). No judge, no
HuggingFace, no extra binaries.

| Variable | What | Where it comes from |
| --- | --- | --- |
| `SUROGATES_SA_TOKEN` | harness `/v1/api/*` auth | minted once inside the prod cluster — see "Minting `SUROGATES_SA_TOKEN`" in `benchmarks/gaia/README.md` (same token works for all benchmarks) |
| `ECBENCH_BASE_URL` | harness API base | prod `https://cloud.surogate.ai` (default `http://localhost:8000`) |
| `ECBENCH_AGENT_ID` | the agent under test | the agent's page in ops/Studio |
| `ECBENCH_HOME` | E-CommerceBench checkout | default `vendor/E-CommerceBench` |

### Setup

The benchmark keeps its own venv. Never run `uv sync` from the repo
root while working here — it reinstalls the pinned `surogates` wheel
over the local dev install.

```bash
cd benchmarks/ecbench
git clone https://github.com/QwenLM/E-CommerceBench.git vendor/E-CommerceBench
git -C vendor/E-CommerceBench checkout "$(cat PIN)"   # audited upstream commit

uv venv .venv --python 3.12
uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/pytest          # offline suite, no network
```

The checkout is pinned by `PIN` (tracked): the world, kernel and
settlement rules at any other commit simulate a different economy, and
the runner refuses to start on a mismatch.

### Running

From `benchmarks/ecbench/`:

```bash
set -a; source .env; set +a                              # load credentials
.venv/bin/ecbench run --max-days 5 --episodes 1          # smoke: protocol + settle
.venv/bin/ecbench run --max-days 30 --episodes 1         # pilot month, cost check
.venv/bin/ecbench run --episodes 3                       # counted: 3 full-year episodes
.venv/bin/ecbench score <run_id>                         # settle + outcomes.json
.venv/bin/ecbench report <run_id>                        # per-episode table
```

Run ids auto-increment per sequence: **full-horizon (365-day) runs are
counted** and take the `year-00x` sequence; anything shorter is a pilot
in `smoke-00x`. Pass `--run-id` to name one explicitly.

**Cost warning — read before a year run.** Upstream episodes reach
~4,000 turns and 120k-token transcripts on their scaffold; on prod this
is a many-hour, provider-heavy session per episode. Always run a
`--max-days 5` smoke and a `--max-days 30` pilot first, extrapolate the
cost, and only then commit to `--episodes 3`. The default per-episode
wall-clock cap is 4 h (`--wall-clock-cap`); a capped episode still
scores — the settle runs on whatever day it reached, exactly like an
upstream `max_turns` termination.

Episodes run **sequentially by default** (`--concurrency 1`):
workspace-bench `dev-001` showed the tier rate-limiting under sustained
parallel load, and a killed episode here loses hours, not minutes.

### Against a local harness instead

Same two services as `benchmarks/workspace_bench` (ops server + harness
in shared mode; no mcp-proxy). See "Against a local harness instead" in
`benchmarks/gaia/README.md`, then:

```bash
export SUROGATES_SA_TOKEN=<service-account token>
export ECBENCH_BASE_URL=http://localhost:8000
export ECBENCH_AGENT_ID=<agent under test>
```

## Discipline

- **The per-episode table is the product.** Assets alone say little; a
  bankruptcy, a stalled day counter, or a session that never ran
  `finalize` each point at a different part of the harness. Copy the
  table into RESULTS.md verbatim.
- **Episodes are noisy.** The world is fixed and fully deterministic —
  all variance is the agent. Counted runs use ≥3 episodes and report
  mean + range; never claim a change from one episode.
- **Pin the model.** Sessions run under the tier sentinels; check the
  served model in the traces before comparing runs.
- **One config change per run** — including `--max-days`, the NPC
  renderer mode, and the PIN. A run at a different pin simulated a
  different economy; never compare across pins.
- **Never delete run folders** — the id sequence takes the highest
  existing number per prefix; deleting the newest frees its id for
  silent reuse. Summarize dead runs in RESULTS.md instead.
