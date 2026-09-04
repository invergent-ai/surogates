# Claw-Eval Benchmark

## Description

Runs [Claw-Eval](https://github.com/claw-eval/claw-eval) tasks against a
Surogate agent, with each task's mock services and tool universe exposed
to the agent through the platform's own MCP machinery, and grades the
resulting trajectories with the **upstream, vendored graders**
(programmatic checks + LLM rubrics).

It is a measurement tool for the harness, not a test of the model — same
philosophy as `benchmarks/gaia`. Upstream Claw-Eval drives an agent loop
it owns; here the surogates harness runs the loop, so scores reflect *our*
context management, tool routing, MCP path, and prompting. That is the
point: failures are harness findings. The number is therefore **not
comparable to the public Claw-Eval leaderboard** — different scaffold by
design.

Scores: [RESULTS.md](RESULTS.md). After every **`dev-` (full) run**,
append its row to the main table **and its full failed-task list** —
every failing task with its dimension scores and a one-line reason, copied
from the run's `report.md`. That list is the primary artifact: it is the
harness-improvement backlog you read later to decide what to fix. A
**`smoke-` run** gets a one-line row in the "Smaller runs" table only, no
failed-task list (its detail stays in `runs/<id>/report.md`).

**The benchmark is a client, not a library.** It talks to the harness over
its HTTP API and to the ops server over its REST API, and never imports
`surogates` or `surogate_ops` (`tests/test_isolation.py` enforces this).
Measuring the harness through the same surface real callers use is the
only way the numbers mean anything.

### Layout

| File | Responsibility |
| --- | --- |
| `vendor.py` | Locate + verify the pinned claw-eval checkout (`PIN`) |
| `tasks.py` | Discover tasks, phase-1 eligibility filter, split selection |
| `services.py` | Upstream `ServiceManager` lifecycle + audit collection |
| `mcp_adapter.py` | Per-task subprocess: task tools over streamable-HTTP MCP |
| `tunnel.py` | Expose the local adapter to a remote harness (cloudflared) |
| `registrar.py` | Ops control plane: create MCP row, attach/detach on the agent |
| `client.py` | Async harness API client. HTTP only, no business logic |
| `runner.py` | One task to one rollout; setup order, timeout, trace persistence |
| `bridge.py` | Session events → upstream trace models for grading |
| `grade.py` | Task's vendored `grader.py` + upstream `LLMJudge` |
| `report.py` | Scores, failure buckets, failed-task list |
| `cli.py` | `run` / `report` / `cleanup` |

### How a task runs

For each task, sequentially:

1. The task's **mock services** are started from the vendored checkout
   (upstream's own `ServiceManager` — the services the agent hits are
   byte-for-byte upstream's).
2. A local **MCP adapter** exposes the task's declared tools over MCP,
   forwarding calls to the mock services with upstream dispatch
   semantics, and logging every call. Against a remote harness the
   adapter is fronted by a **cloudflared quick tunnel** (started
   automatically once per run) so the platform's mcp-proxy can reach it.
3. The adapter is **registered through the ops control plane**
   (`claweval-<task_id>`: `POST /api/mcp-servers` + attach to the agent)
   — the mcp-proxy only loads servers on the agent's runtime-config
   allow-list, and only ops maintains that list, so a row written
   straight into the harness registry would be invisible. The row is
   detached and deleted right after the task (fresh name per task — no
   cache between the mcp-proxy and the adapter can leak one task's tools
   into the next).
4. One **agent session** runs the task prompt through `/v1/api/*` (same
   client semantics as `benchmarks/gaia`, including mid-stream
   reconnect).
5. The trajectory is bridged into upstream trace models and graded by the
   task's own `grader.py` with an OpenAI-compatible **LLM judge**;
   service **audit logs** are collected before teardown.

Sequential by design: the MCP attachment is agent-scoped, so parallel
tasks would see each other's tools. Don't use the agent for anything else
mid-run for the same reason.

### Scope (phase 1)

Mock-service tasks only. Of the general split: **45 eligible English
tasks** (35 zh). Skipped and reported, not silently dropped: 56 tasks
needing upstream sandbox fixtures (phase 2: mappable onto session
workspaces), 6 multi-turn tasks (need a simulated-user model), and the
multimodal split (media attachments).

## How to run

Two ways to point the benchmark at an agent. **Against production** is
the default and what RESULTS.md rows come from. **Against a local
harness** is for iterating on harness changes before they ship.

The benchmark drives real agent sessions on prod: the agent's model,
tools, and prompts are whatever that agent is configured with in ops —
keep the agent lean (no web browsing), since tasks are meant to be solved
through the mock tools. Nothing runs remotely except the sessions — the
orchestrator, mock services, and adapter are local, so the machine
running it must stay awake and online for the duration.

### Requirements

Two binaries beyond the venv: `uv` (setup) and, for prod runs,
**`cloudflared`** (`brew install cloudflared`) — the prod mcp-proxy must
reach the local adapter, so the runner starts a quick tunnel
automatically. The quick-tunnel URL is public and unauthenticated for the
duration of the run; it only fronts the task's mock services (synthetic
fixture data). Set `CLAWEVAL_ADAPTER_PUBLIC_URL` instead to use a tunnel
you operate yourself.

Values, placed in `benchmarks/claweval/.env` (git-ignored):

| Variable | What | Where it comes from |
| --- | --- | --- |
| `SUROGATES_SA_TOKEN` | harness `/v1/api/*` auth | minted once inside the prod cluster — see "Minting `SUROGATES_SA_TOKEN`" in `benchmarks/gaia/README.md` (same token works for both benchmarks) |
| `CLAWEVAL_BASE_URL` | harness API base | prod `https://cloud.surogate.ai` (default `http://localhost:8000`) |
| `CLAWEVAL_AGENT_ID` | the agent under test | the agent's page in ops/Studio |
| `CLAWEVAL_PROJECT_ID` | ops project owning the MCP registrations | the project holding the agent (same value as the harness org id; `CLAWEVAL_ORG_ID` accepted as a fallback) |
| `CLAWEVAL_OPS_BASE_URL` | ops control plane | `https://ops.surogate.ai` (defaults there for a remote harness, `http://localhost:8888` for a local one) |
| `CLAWEVAL_OPS_USER` / `CLAWEVAL_OPS_PASSWORD` | ops dashboard login (username or email); the registrar re-mints its hour-lived token on expiry, and routes the attempt like the login form: local accounts via `/api/auth/login`, Firebase-backed accounts via Firebase email+password sign-in + ID-token exchange | your ops.surogate.ai account (must have access to the project). A Google-SSO-only account has no password — pass a ready JWT via `CLAWEVAL_OPS_TOKEN` instead (it must outlive the run) |
| `CLAWEVAL_FIREBASE_API_KEY` | optional: Firebase web API key for the sign-in above | defaults to the key baked into the deployed dashboard bundle; only needed if the deployment changes Firebase projects |
| `CLAWEVAL_ADAPTER_PUBLIC_URL` | optional: base URL forwarding to the adapter port | only if you run your own tunnel; unset + remote harness = automatic quick tunnel |
| `CLAWEVAL_EXCLUDED_TOOLS` | native tools stripped from every session so the agent uses the task's mock tools, not its own | defaults to the web/deep-research suite (`web_search`, `web_extract`, `web_crawl`, `research_memory`, `research_outline`); comma-separated list to override, or `none` to measure the agent's own toolset |
| `CLAWEVAL_JUDGE_BASE_URL` | OpenAI-compatible judge endpoint | platform model proxy: `https://api.surogate.ai/proxy/services/_model/<deployed model id>/v1` |
| `CLAWEVAL_JUDGE_KEY` / `CLAWEVAL_JUDGE_MODEL` | judge credentials / model id | the `sk-agent` key vaulted for that model at deploy time |
| `CLAWEVAL_HOME` | claw-eval checkout | default `vendor/claw-eval` |

MCP registration goes through the ops server on both paths — the agent
only sees MCP servers attached to it in ops — so the ops login is
required for local runs too.

### Setup

The benchmark keeps its own venv. Never run `uv sync` from the repo root
while working here — it reinstalls the pinned `surogates` wheel over the
local dev install.

```bash
cd benchmarks/claweval
git clone https://github.com/claw-eval/claw-eval.git vendor/claw-eval
git -C vendor/claw-eval checkout "$(cat PIN)"   # audited upstream commit

uv venv .venv --python 3.12
uv pip install --python .venv/bin/python -e ".[dev]" -e "./vendor/claw-eval[mock]"
.venv/bin/pytest          # offline suite, no network
```

The checkout is pinned by `PIN` (tracked): tasks, mock services, and
graders at any other commit grade differently, and the runner refuses to
start on a mismatch.

### Running

From `benchmarks/claweval/`:

```bash
set -a; source .env; set +a                                 # load credentials
.venv/bin/claweval-bench run --split general --limit 3      # smoke: tunnel + registration + one graded task
.venv/bin/claweval-bench run --split general                # full 45-task run, sequential
.venv/bin/claweval-bench run --split general --tasks T046,T053   # re-check specific tasks
.venv/bin/claweval-bench report <run_id>                    # scores + failed-task list
.venv/bin/claweval-bench cleanup                            # remove stray MCP rows after a crash
```

Run ids auto-increment per family: a partial run (`--limit` or `--tasks`)
is a **smoke** run and lands in `smoke-00N`; a full split is a **real**
run and lands in `dev-00N`. Pass `--run-id` to name one explicitly. Tasks
run one at a time with a 900 s wall-clock cap each, so expect a full run
to take a few hours. Each task writes `runs/<id>/tasks/<task_id>/`:
`events.jsonl`, `meta.json`, `dispatches.jsonl` (the wire traffic the
graders reason about), `audit.json`, `scores.json`.

**After every `dev-` run: append its row AND its full failed-task list to
[RESULTS.md](RESULTS.md)** — the failed-task list is the harness backlog.
A `smoke-` run gets a one-line row in the "Smaller runs" table only.

### Against a local harness instead

Four live services — a local ops server, and the harness in shared mode
pointed at it (`api`, `worker`, and unlike GAIA also **`mcp-proxy`**).
See "Against a local harness instead" in `benchmarks/gaia/README.md` for
the ops + harness environment; everything there applies, plus the
mcp-proxy process. No tunnel is needed — the proxy and the adapter share
a loopback.

```bash
export SUROGATES_SA_TOKEN=<service-account token>
export CLAWEVAL_BASE_URL=http://localhost:8000
export CLAWEVAL_AGENT_ID=<agent under test>
export CLAWEVAL_PROJECT_ID=<project holding the agent>
export CLAWEVAL_OPS_USER=<local ops user>
export CLAWEVAL_OPS_PASSWORD=<local ops password>
# CLAWEVAL_OPS_BASE_URL defaults to http://localhost:8888 here
```

## Grading

Upstream code end to end: the task's own `grader.py` from the pinned
checkout, upstream's `LLMJudge` for rubric items. "Passed" = safety gate
held (1.0) and completion ≥ 0.75 (upstream's threshold). Bridging
approximations (synthetic dispatch ids, text-only artifacts) are
documented in `claweval_bench/bridge.py`; a grader crash is recorded as
that task's outcome, never a run failure.

## Known findings from runs

Runs surface harness behaviour; the durable ones live here, the per-run
ones in [RESULTS.md](RESULTS.md).

- **Provider rate-limit aborts the session (blocks a clean full run).**
  On the first full `dev-001` attempt (2026-09-03, `surogate-pro`) a
  tool-heavy task (T048: 27 mock-tool calls) tripped the tier's rate
  limit mid-session; the harness's `call_llm_with_retry` did 3 quick
  retries and failed the session, even though the error carries the exact
  wait ("rate-limited for 295 more seconds"). The next task then started
  while the window was still open and failed with zero progress. This is a
  real production robustness gap — a bursty session fails instead of
  waiting the known window out; the fix is in the harness
  (`surogates/harness/llm_call.py`), not this benchmark. Benchmark-side
  mitigation: the runner detects a rate-limit death and backs off
  `--rate-limit-backoff` seconds (default 300) before the next task, so
  the tier recovers; a normal task keeps the short `--task-cooldown`
  (default 20 s). A task whose *own* burst trips the limit can still fail
  and is re-run with `--tasks`. Traces: `runs/dev-001/`. `dev-003` later
  completed all 45 with no rate-limit event (uncontended tier); the
  harness-side retry bug is still open and will resurface under load.
- **Finance is the weak category.** Across `dev-002` and `dev-003` finance
  tasks pass at ~18% (2/11 in `dev-003`) against ~40–45% for workflow and
  ops. A coherent target.
- **A large near-miss cluster sits just under the 0.75 bar.** `dev-003`
  had 13 tasks at completion 0.60–0.74 (four at 0.72–0.74). Lifting that
  cluster alone would move the run from 17/45 to ~30/45 — the
  highest-leverage lever; read those traces for the last missing step
  before broad changes.
- **`communication` scores 0.0 on almost every task** (44/45 in
  `dev-003`, including passes) — but non-zero on one, so the grader can
  score it. At least partly real agent behaviour (the final-answer /
  notification step not performed), not purely a bridging gap; verify
  which, then treat as a prompt finding.

## Discipline

- **Failed tasks are the product.** For every `dev-` run, the report's
  failed-task table (per-dimension scores + a one-line reason) goes into
  RESULTS.md verbatim — it is the harness-improvement backlog you read
  later. Score movements without that trace evidence are noise. Smoke runs
  skip it (their detail stays in `runs/<id>/report.md`).
- **Single runs are noisy** — same rules as GAIA: compare means, re-run a
  subset 3× before believing a fix, one config change per run.
- **Pin the model.** Sessions run under tier sentinels; check the traces
  for the model that actually served before comparing runs.
- **Leave no residue.** The runner detaches and deletes its MCP rows even
  on failure; `claweval-bench cleanup` removes anything a crash left
  behind.
- **Never put stray files in `runs/`** — the run-id counter counts
  entries, and a stray file shifts ids. Never delete run folders either;
  summarize in RESULTS.md instead.
