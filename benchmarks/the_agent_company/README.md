# TheAgentCompany Benchmark

## Description

Runs [TheAgentCompany](https://github.com/TheAgentCompany/TheAgentCompany)
([paper](https://arxiv.org/abs/2412.14161) — CMU's 175 workplace tasks
inside a simulated software company: a self-hosted GitLab, ownCloud,
Plane and RocketChat stack the agent must browse, code against and
communicate through, graded by per-task programmatic checkpoints)
against a Surogate agent, graded by **upstream's own evaluators run in
upstream's own task images**.

It is a measurement tool for the harness, not a test of the model —
same philosophy as the sibling benchmarks; the number is **not
comparable to the public leaderboard** (different agent scaffold by
design; upstream's baselines run OpenHands).

Scores: [RESULTS.md](RESULTS.md) — append a row **and the
below-full-completion table** after every counted run.

**The benchmark is a client, not a library.** It talks to the harness
over its HTTP API and never imports `surogates` or `surogate_ops`
(enforced in tests). No MCP, no ops-plane writes — but this benchmark
does need the **company service stack** running somewhere the agent's
tools can reach (see below): that stack *is* the benchmark.

### How a task runs

1. **Prompt.** One session gets the task's `task.md` with the canonical
   service hostname (`the-agent-company.com`) rewritten to
   `TAC_HOSTNAME`. The agent needs **browser + terminal** tools; the
   work happens against the hosted services and in the session
   workspace (mounted at `/workspace` for grading).
2. **Roll out.** Streamed to a terminal state with the sibling
   benchmarks' reconnect discipline; 3600 s cap — these are long tasks.
3. **Collect.** The whole session workspace is downloaded into the
   run's `workspace/` directory.
4. **Grade** (`tacbench grade`). For each task, one
   `docker run` of upstream's task image with the collected workspace
   mounted read-only at `/workspace` and `--add-host` pointing the
   canonical hostname at the stack; inside, the task's own
   `evaluator.py grade_checkpoints()` runs and prints its Result as
   JSON. Checkpoints that inspect services (RocketChat messages, GitLab
   state) grade the live stack, so **grade in the same session as the
   run, before resetting the environment.** A task whose image is
   missing or whose evaluator crashes is **ungradable with the
   reason**, never zero.

Sequential by design: tasks mutate shared services, and upstream resets
the stack between tasks — parallel sessions would contaminate each
other.

### The service stack (operator-provided)

Upstream's `servers/` compose brings up GitLab, ownCloud, Plane,
RocketChat and the API server. Host it where the agent under test can
reach it:

- **Local harness**: run the stack on the same machine; the sandbox and
  the services share the host network. The natural mode.
- **Prod agent**: host the stack on a machine with a public address and
  set `TAC_HOSTNAME` to it. The benchmark adds no tunnels of its own —
  a prod-reachable stack is the operator's choice and responsibility
  (the fixture data is synthetic, but the services are real software).

Task images for grading are built once with upstream's
`evaluation/generate_task_images.py`; point `TAC_IMAGE_REGISTRY` at
where they land.

### Layout

| File | Responsibility |
| --- | --- |
| `vendor.py` | Pinned checkout (`PIN`) |
| `dataset.py` | Task discovery, checkpoints/points parsing, hostname substitution |
| `client.py` | Async harness API client. HTTP only, no business logic |
| `runner.py` | Session per task; workspace collection |
| `grade.py` | Upstream evaluator in the task image; Result parsing |
| `report.py` | Upstream partial-credit score, per-category table |
| `cli.py` | `tasks` / `run` / `grade` / `report` |

Scoring formula is upstream's: per task,
`0.5 × full_completion + 0.5 × points_earned/points_total`, averaged
and reported as a percentage. 175 tasks, 767 checkpoint points at the
current PIN.

## How to run

### Requirements

Values, placed in `benchmarks/the_agent_company/.env` (git-ignored):

| Variable | What |
| --- | --- |
| `SUROGATES_SA_TOKEN` | harness `/v1/api/*` auth (same token as the siblings) |
| `TAC_BASE_URL` | prod `https://cloud.surogate.ai` (default `http://localhost:8000`) |
| `TAC_AGENT_ID` | agent under test — **browser + terminal capable** |
| `TAC_HOSTNAME` | where the agent reaches the company services |
| `TAC_HOSTNAME_IP` | same host's IP (evaluator containers' `--add-host`) |
| `TAC_IMAGE_REGISTRY` | registry/namespace of the built task images |

### Setup

```bash
cd benchmarks/the_agent_company
git clone https://github.com/TheAgentCompany/TheAgentCompany.git vendor/TheAgentCompany
git -C vendor/TheAgentCompany checkout "$(cat PIN)"   # audited upstream commit

uv venv .venv --python 3.12
uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/pytest                                      # offline suite

# The stack + task images, per upstream docs (servers/README, evaluation/):
#   servers/ docker compose up; evaluation/generate_task_images.py
.venv/bin/tacbench tasks                              # 175 tasks, points each
```

### Running

From `benchmarks/the_agent_company/`:

```bash
set -a; source .env; set +a
.venv/bin/tacbench run --tasks admin-arrange-meeting-rooms   # smoke
.venv/bin/tacbench run --prefixes sde --limit 5              # category probe
.venv/bin/tacbench run                                       # all 175, sequential
.venv/bin/tacbench grade <run_id>       # in the SAME environment session
.venv/bin/tacbench report <run_id>
```

Run ids auto-increment per sequence: unfiltered runs are counted
(`full-00x`); anything filtered is a pilot in `smoke-00x`. A full run
is 175 long-horizon sessions — hours and real cost; pilot first, and
reset the service stack between counted runs per upstream's docs.

**After every counted run: append a row AND the below-full-completion
table to [RESULTS.md](RESULTS.md).**

## Discipline

- **The below-full table is the product** — points, status and first
  failure signal per task go into RESULTS.md verbatim.
- **Grade before resetting the stack** — service-inspecting checkpoints
  read live state; grading after a reset scores a different world.
- **Single runs are noisy**; compare means, one config change per run.
- **Pin the model** and **pin the checkout** — evaluators at another
  commit grade differently, and the runner refuses a PIN mismatch.
- **Never delete run folders** — the id sequence takes the highest
  existing number per prefix. Summarize dead runs in RESULTS.md instead.
