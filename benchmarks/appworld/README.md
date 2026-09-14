# AppWorld Benchmark

## Description

Runs [AppWorld](https://github.com/StonyBrookNLP/appworld)
([paper](https://arxiv.org/abs/2407.18901) — 9 simulated day-to-day
apps operable via 457 APIs, populated with ~100 people's digital lives,
and 750 tasks requiring rich interactive work) against a Surogate
agent, using **upstream's official MCP server** as the tool surface and
**upstream's stateful test suites** for grading. Deterministic — no
judge.

It is a measurement tool for the harness, not a test of the model —
claweval/EOG shape: the platform's own mcp-proxy connects to AppWorld's
MCP server, so scores reflect *our* MCP path, tool routing and context
management. The number is therefore **not comparable to the public
leaderboard** — different scaffold by design (upstream's headline
agents interact via a code REPL, not MCP).

Scores: [RESULTS.md](RESULTS.md) — append a row **and the failed-task
list** after every counted run.

**The benchmark is a client, not a library.** It talks to the harness
over its HTTP API and to the ops server over its REST API, and never
imports `surogates` or `surogate_ops` (enforced in tests). The
`appworld` package itself is a benchmark dependency (environment +
evaluator), pinned exactly in `pyproject.toml` — the version *is* the
PIN.

### How a task runs

Sequential by construction (one API server holds one task's database;
the MCP attachment is agent-scoped):

1. **Bind.** ``AppWorld(task_id, remote_apis_url=…)`` points the local
   API server at the task's initial databases and exposes the
   supervisor context.
2. **Expose + register.** A cloudflared quick tunnel (once per run)
   fronts the local AppWorld MCP server; the ops registrar creates a
   per-task MCP row (``aw-<task>``) and attaches it to the agent —
   fresh name per task.
3. **Roll out.** One session: the supervisor's identity plus the task
   instruction, streamed to a terminal state with the sibling
   benchmarks' reconnect discipline.
4. **Settle.** ``world.save()`` then ``world.evaluate()`` — upstream's
   own stateful test suite over the final app databases. Passed =
   every test passed (upstream's Task Goal Completion).
5. **Teardown.** MCP row detached + deleted, world closed, even on
   failure; `awbench cleanup` removes crash residue.

### Layout

| File | Responsibility |
| --- | --- |
| `client.py` | Async harness API client. HTTP only, no business logic |
| `tunnel.py` / `registrar.py` | The proven exposure + ops machinery |
| `runner.py` | Bind → register → session → save/evaluate → teardown |
| `report.py` | Task Goal Completion; failed-task list |
| `cli.py` | `run` / `report` / `cleanup` |

Splits are upstream's: `dev` (smokes), `test_normal` and
`test_challenge` (counted). Run ids: counted runs take the split's
sequence (`test_normal-00x`); anything with `--limit`/`--tasks` or on
`dev` lands in `smoke-00x`.

## How to run

Two local upstream servers plus (for prod) `cloudflared`:

```bash
# once: install data (~a few hundred MB, ungated)
.venv/bin/appworld install

# terminal 1 -- the environment/API server
.venv/bin/appworld serve apis --port 9000

# terminal 2 -- the MCP server fronting it
.venv/bin/appworld serve mcp http --remote-apis-url http://localhost:9000 --port 10000
```

### Requirements

Values, placed in `benchmarks/appworld/.env` (git-ignored):

| Variable | What |
| --- | --- |
| `SUROGATES_SA_TOKEN` | harness `/v1/api/*` auth (same token as the siblings) |
| `AW_BASE_URL` | prod `https://cloud.surogate.ai` (default `http://localhost:8000`) |
| `AW_AGENT_ID` | agent under test — **lean, no web/sandbox**: tasks are solved through the AppWorld tools only |
| `AW_PROJECT_ID` | ops project owning the MCP registrations |
| `AW_OPS_USER` / `AW_OPS_PASSWORD` (or `AW_OPS_TOKEN`) | ops login (same semantics as claweval's registrar) |
| `AW_APIS_URL` / `AW_MCP_URL` | the two local servers (defaults :9000 / :10000) |
| `AW_ADAPTER_PUBLIC_URL` | optional: your own tunnel instead of a quick tunnel |

### Setup

```bash
cd benchmarks/appworld
uv venv .venv --python 3.12
uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/pytest          # offline suite + appworld interface seams
.venv/bin/appworld install
```

### Running

From `benchmarks/appworld/`:

```bash
set -a; source .env; set +a
.venv/bin/awbench run --split dev --limit 1     # smoke: bind + tunnel + evaluate
.venv/bin/awbench run --split test_normal       # counted run
.venv/bin/awbench report <run_id>
.venv/bin/awbench cleanup
```

Tasks run one at a time with an 1800 s cap; a full `test_normal` run is
a long, provider-heavy day — pilot with `--limit` first and extrapolate
cost.

**After every counted run: append a row AND its failed-task list to
[RESULTS.md](RESULTS.md).**

## Discipline

- **Failed tasks are the product** — the failed-task table goes into
  RESULTS.md verbatim.
- **Single runs are noisy**; compare means, one config change per run.
- **Pin the model** — check the served model in the traces; the
  `appworld` version in `pyproject.toml` is the environment PIN — never
  compare across versions.
- **Leave no residue** — teardown runs even on failure; `cleanup` after
  crashes. Never delete run folders; summarize dead runs in RESULTS.md.
