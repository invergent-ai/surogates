# EnterpriseOps-Gym Benchmark

## Description

Runs [EnterpriseOps-Gym](https://github.com/ServiceNow/EnterpriseOps-Gym)
([paper](https://arxiv.org/abs/2603.13594) — ServiceNow's stateful
enterprise simulation: 1,150 tasks over 8 domain gyms — Teams, CSM,
Email, ITSM, Calendar, HR, Drive, Hybrid — with 512 tools over 164
database tables and up to 34-step workflows) against a Surogate agent,
verified by upstream's **hidden SQL checks on final database state**.
Fully deterministic scoring — no judge.

It is a measurement tool for the harness, not a test of the model —
same philosophy as `benchmarks/claweval`, whose shape this follows:
each domain gym is a live MCP server the platform's own mcp-proxy
connects to, so scores reflect *our* MCP path, tool routing and context
management. The number is therefore **not comparable to the public
leaderboard** — different scaffold by design.

Scores: [RESULTS.md](RESULTS.md) — append a row **and the failed-task
list** after every counted run.

**The benchmark is a client, not a library.** It talks to the harness
over its HTTP API and to the ops server over its REST API, and never
imports `surogates` or `surogate_ops` (`tests/test_isolation.py`
enforces this).

### How a task runs

Sequential by design (the MCP attachment is agent-scoped):

1. **Seed.** The vendored upstream helper creates a fresh task database
   on the local gym server from the task's seed SQL; the **gym proxy**
   (a local header-injecting forwarder, `proxy.py`) starts stamping its
   ``x-database-id`` on every request — the gym scopes all state by
   that header, and the platform's mcp-proxy cannot add custom headers
   itself.
2. **Expose + register.** A cloudflared quick tunnel (started once per
   run, same machinery as claweval) fronts the proxy; the ops registrar
   creates a per-task MCP row (`eog-<task>`) and attaches it to the
   agent. Fresh name per task — nothing can leak tools across tasks.
3. **Roll out.** One session: the task's policy system prompt plus its
   user prompt, streamed to a terminal state with the sibling
   benchmarks' reconnect discipline.
4. **Verify.** The vendored ``VerifierEngine`` runs the task's hidden
   SQL directly against the local gym (same database id). Task success
   = **every verifier passed** — upstream's Task Success Rate.
5. **Teardown.** MCP row detached + deleted, database deleted, even on
   failure. `eogbench cleanup` removes crash residue.

### Layout

| File | Responsibility |
| --- | --- |
| `vendor.py` | Locate + verify the pinned EnterpriseOps-Gym checkout (`PIN`) |
| `dataset.py` | Task JSONs (vendored sample or `EOG_TASKS_DIR`) |
| `proxy.py` | Local forwarder injecting the per-task `x-database-id` |
| `tunnel.py` / `registrar.py` | Claweval's proven exposure + ops machinery |
| `client.py` | Async harness API client. HTTP only, no business logic |
| `runner.py` | Seed → register → session → verify → teardown |
| `report.py` | Task Success Rate overall and per domain; failed list |
| `cli.py` | `run` / `report` / `cleanup` |

### Tasks

The public repo ships a per-domain sample under `data/revised/`
(vendored; loaded by default — 13 tasks, 102 SQL verifiers, all
`database_state`). The full 1,150-task set is on HuggingFace
([ServiceNow-AI/EnterpriseOps-Gym](https://huggingface.co/datasets/ServiceNow-AI/EnterpriseOps-Gym));
drop it into the same `<domain>/task_*.json` layout and point
`EOG_TASKS_DIR` at it. Multi-gym *hybrid* tasks are refused loudly (out
of scope until the proxy handles several gyms in one task).

## How to run

Three moving parts beyond the venv: **docker** (the domain gym
servers), **cloudflared** (prod runs), and the vendored checkout.

### Requirements

Values, placed in `benchmarks/enterpriseops_gym/.env` (git-ignored):

| Variable | What |
| --- | --- |
| `SUROGATES_SA_TOKEN` | harness `/v1/api/*` auth (same token as the siblings) |
| `EOG_BASE_URL` | prod `https://cloud.surogate.ai` (default `http://localhost:8000`) |
| `EOG_AGENT_ID` | agent under test — **lean, no web/sandbox**: tasks are solved through the gym tools only |
| `EOG_PROJECT_ID` | ops project owning the MCP registrations |
| `EOG_OPS_USER` / `EOG_OPS_PASSWORD` (or `EOG_OPS_TOKEN`) | ops login — same semantics as claweval's registrar, Firebase accounts included |
| `EOG_ADAPTER_PUBLIC_URL` | optional: your own tunnel instead of a quick tunnel |
| `EOG_TASKS_DIR` / `EOG_GYM_URL` | optional overrides (task set / single gym URL) |

### Setup

```bash
cd benchmarks/enterpriseops_gym
git clone https://github.com/ServiceNow/EnterpriseOps-Gym.git vendor/EnterpriseOps-Gym
git -C vendor/EnterpriseOps-Gym checkout "$(cat PIN)"   # audited upstream commit

uv venv .venv --python 3.12
uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/pytest          # offline suite; vendored-interface tests need the clone
```

Start the gym server(s) for the domains you run (upstream's images,
ports per their README):

```bash
docker pull shivakrishnareddyma225/enterpriseops-gym-mcp-csm:latest
docker run -d -p 8001:8001 shivakrishnareddyma225/enterpriseops-gym-mcp-csm:latest
# teams: 8002, email: 8004, ... -- see vendor README's port table
```

### Running

From `benchmarks/enterpriseops_gym/`:

```bash
set -a; source .env; set +a
.venv/bin/eogbench run --domains csm --limit 1    # smoke: seed + tunnel + register + verify
.venv/bin/eogbench run                            # every task in the tasks dir
.venv/bin/eogbench report <run_id>
.venv/bin/eogbench cleanup                        # stray MCP rows after a crash
```

Run ids auto-increment per sequence: unfiltered runs are counted
(`full-00x`); anything filtered by `--domains`, `--limit` or `--tasks`
is a pilot in `smoke-00x`. Tasks run one at a time with an 1800 s cap.

**After every counted run: append a row AND its failed-task list to
[RESULTS.md](RESULTS.md).**

## Discipline

- **Failed tasks are the product** — the failed-task table (with the
  failing verifier names) goes into RESULTS.md verbatim.
- **Single runs are noisy**; compare means, one config change per run,
  never claim a fix from one run.
- **Pin the model** — check the served model in the traces; and **pin
  the checkout** — verifiers at another commit check different SQL, and
  the runner refuses a PIN mismatch.
- **Leave no residue** — teardown runs even on failure; `cleanup` after
  crashes. Never delete run folders; summarize dead runs in RESULTS.md.
