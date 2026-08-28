# GAIA Benchmark

## Description

Runs [GAIA](https://huggingface.co/datasets/gaia-benchmark/GAIA) (General AI
Assistants — 165 gated validation tasks needing web research, file handling
and multi-step reasoning) against a Surogate agent and scores answers with the
vendored official strict scorer.

It is a measurement tool for the harness, not a test of the model: GAIA tasks
need real tool use, so a task fails when the harness fails to act, and the
trace says which part gave up.

Scores: [RESULTS.md](RESULTS.md) — append a row after every counted run.

**The benchmark is a client, not a library.** It talks to the harness over its
HTTP API and never imports `surogates` or `surogate_ops`
(`tests/test_isolation.py` enforces this). Measuring the harness through the
same surface real callers use is the only way the numbers mean anything; an
in-process import would bypass the API, the session store and the tool router,
which is most of what is being measured.

### Layout

| File | Responsibility |
| --- | --- |
| `dataset.py` | Load GAIA validation from HF; deterministic 110/55 split |
| `client.py` | Async harness API client. HTTP only, no business logic |
| `runner.py` | One task to one rollout; concurrency, timeout, trace persistence |
| `scorer.py` | Vendored official GAIA scorer + lenient matcher |
| `detectors.py` | Stage-1 deterministic failure detectors |
| `attribute.py` / `judge.py` | Stage-2 LLM root-cause attribution |
| `report.py` | Regressions, scores, failure histogram, ranked fix list |
| `cli.py` | `run` / `analyze` / `report` |

Each run writes `runs/<run_id>/`: `outcomes.json` plus per-task
`events.jsonl`, `meta.json` and `trajectory.md`. All analysis reads those
stored traces — you never re-run a task to ask a new question of it. `runs/`
is gitignored: local trace evidence, not repo content.

### The split

Frozen in `gaia_bench/splits/` from the live dataset (165 validation rows, 38
with attachments), seed 20260726. Iterate on dev; touch holdout only to
report a final number.

| Split | L1 | L2 | L3 | Attachments |
| --- | --- | --- | --- | --- |
| dev (110) | 33 | 60 | 17 | 27 |
| holdout (55) | 20 | 26 | 9 | 11 |

The split is random rather than stratified by level, so holdout skews slightly
easier than dev (36% vs 30% Level 1). The dev-vs-holdout gap is the
overfitting signal, and an easier holdout biases that gap toward looking
healthy.

## How to run

Two ways to point the benchmark at an agent. **Against production** is the
default and what RESULTS.md rows come from. **Against a local harness** is for
iterating on harness changes before they ship.

### Requirements

Four values, placed in `benchmarks/gaia/.env` (git-ignored):

| Variable | What | Where it comes from |
| --- | --- | --- |
| `SUROGATES_SA_TOKEN` | service-account token authenticating the benchmark to the platform | minted once inside the prod cluster — see below |
| `GAIA_BASE_URL` | prod harness API | `https://cloud.surogate.ai` |
| `GAIA_AGENT_ID` | the agent under test | the agent's page in Studio |
| `HF_TOKEN` | dataset download | huggingface.co token whose account has **accepted the GAIA terms** at the dataset page — the download fails without this |

The benchmark drives real agent sessions on prod: the agent's model, tools
(`web_search`, browser, sandbox), and prompts are whatever that agent is
configured with in ops. Nothing runs remotely except the sessions — the
orchestrator is local, so the machine running it must stay awake and online
for the duration.

#### Minting `SUROGATES_SA_TOKEN` (once, cluster access required)

There is no self-serve path — ops is the sole token-minting owner. From a
shell on the cluster (e.g. `root@surogate-master1`):

```bash
POD=$(kubectl -n surogate get pod -l app=surogate-server -o jsonpath='{.items[0].metadata.name}')
kubectl -n surogate exec -i "$POD" -- python - <<'EOF'
import asyncio
from uuid import UUID
from surogate_ops.core.config.loader import load_config
from surogate_ops.core.config.server_config import ServerConfig
from surogate_ops.core.surogates_client import SurogatesClient

ORG_ID = UUID("<project id of the project holding the agent under test>")

async def main():
    cfg = load_config(ServerConfig, "/home/surogate/.surogate/config.yaml")
    sc = SurogatesClient(cfg.surogates_database_url, encryption_key=cfg.encryption_key)
    try:
        issued = await sc.ensure_service_account_token(
            ORG_ID, "benchmarks",
            credential_name="surogates_api_token:benchmarks")
        print(issued["token"])
    finally:
        await sc.close()

asyncio.run(main())
EOF
```

Idempotent — re-running returns the same token. The `benchmarks` service
account is shared by all benchmark runs in the org and revocable at any time.

### Setup

The benchmark keeps its own venv. Never run `uv sync` from the repo root while
working here — it reinstalls the pinned `surogates` wheel over the local dev
install, which is the tree you are trying to measure.

```bash
cd benchmarks/gaia
uv venv .venv --python 3.12
uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/pytest          # offline test suite, no network
```

### Running

From `benchmarks/gaia/`:

```bash
set -a; source .env; set +a                        # load credentials
.venv/bin/gaia-bench run --split dev --limit 10    # pilot: shape + cost check
.venv/bin/gaia-bench run --split dev               # full 110-task run
.venv/bin/gaia-bench report <run_id>               # score, flags, fix list
.venv/bin/gaia-bench report <run_id> --compare <previous_run_id>
.venv/bin/gaia-bench run --split dev --tasks a1b2c3d4,e5f6a7b8   # re-check specific tasks
```

Run ids auto-increment from the contents of `benchmarks/gaia/runs/` (`dev-001`,
`dev-002`, …), where each run's traces and report land. Expect ~100 minutes
and ~4 summed GPU-hours for a full dev run at the default concurrency of 4.

**After every counted run: append a row to [RESULTS.md](RESULTS.md).**

### Against a local harness instead

Two live services: a local ops server, and the harness in shared mode pointed
at it. The harness fetches per-agent runtime config from ops at request time,
so ops starts first.

```bash
surogate-ops server                   # :8888, must be up first
```

Then the harness, with the environment the VSCode launch configs
("Surogates [shared]: API Gateway" / "Worker") set:

```bash
export SUROGATES_CONFIG=$PWD/config.dev.yaml
export SUROGATES_RUNTIME_MODE=shared
export SUROGATES_PLATFORM_API_URL=http://localhost:8888
export SUROGATES_PLATFORM_API_TOKEN=<agent api token>
export SUROGATES_SA_TOKEN=<service-account token>
export GAIA_BASE_URL=http://localhost:8000
export GAIA_AGENT_ID=<agent under test>
export HF_TOKEN=<token with GAIA terms accepted>
```

`SUROGATES_PLATFORM_API_TOKEN` is the extra one here: it authenticates the
harness to the ops server for config lookup, and is unrelated to the two
tokens above. `GAIA_BASE_URL` and `GAIA_AGENT_ID` keep the `GAIA_` prefix on
purpose — `SUROGATES_API_URL` and `SUROGATES_AGENT_ID` are already consumed by
the harness's own settings, so reusing those names cross-talks when the
harness and the benchmark share a shell.

## Failure attribution (optional)

`gaia-bench analyze <run_id>` sends the unexplained failures to an LLM judge
and writes root causes back into the run's `outcomes.json`. It reads stored
traces only — no prod sessions. Needs an OpenAI-compatible endpoint:

```
GAIA_JUDGE_BASE_URL=https://api.surogate.ai/proxy/services/_model/<deployed model id>/v1
GAIA_JUDGE_KEY=<sk-agent key scoped to that model>
GAIA_JUDGE_MODEL=<model id>            # optional; defaults to claude-sonnet-5
```

Any deployed model on the platform works via the model proxy above (the
`sk-agent` key is vaulted per model at deploy time), as does any external
OpenAI-compatible endpoint.

## Discipline

- **Single runs are noisy.** Identical tasks have flipped 7/10 → 4/10 between
  runs hours apart. Compare means of ≥3 runs; never claim a fix from one run.
  `--only-failing` / `--tasks` are fast filters, never the basis for a claim.
- **Iterate on dev; touch holdout only to report a final number.** The frozen
  110/55 split lives in `benchmarks/gaia/gaia_bench/splits/` (task ids only).
- **Never put stray files in `runs/`** — the run-id counter counts entries,
  and a stray file shifts ids. Never delete run folders either (a fresh run
  can silently reuse the freed id); summarize in RESULTS.md instead.

- **Pin the model.** Sessions run under the `surogate` / `surogate-pro` tier
  sentinels, which resolve per request. A run whose calls were served by a
  different model is not comparable — check the served model before
  attributing a delta to harness changes. RESULTS.md records one where this
  bit.
- **Strict vs lenient.** Lenient accepts a correct answer in the wrong format.
  A gap between them is a prompt fix; no gap means the remaining failures are
  real.
