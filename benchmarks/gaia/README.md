# GAIA Benchmark

## Description

Runs the [GAIA benchmark](https://huggingface.co/datasets/gaia-benchmark/GAIA)
against a live Surogate agent, attributes each failure to a harness component,
and produces a ranked fix list. It is a measurement tool for the harness, not
a test of the model: GAIA tasks need real tool use — search, file reads,
vision, browsing — so a task fails when the harness fails to act, and the
trace says which part gave up.

Scores and the open regression: [RESULTS.md](RESULTS.md).

**The benchmark is a client, not a library.** It talks to the harness over its
HTTP API and never imports `surogates` or `surogate_ops`
(`tests/test_isolation.py` enforces this). That is deliberate — measuring the
harness through the same surface real callers use is the only way the numbers
mean anything, and an in-process import would bypass the API, the session
store and the tool router, which is most of what is being measured.

### Layout

| File | Responsibility |
| --- | --- |
| `dataset.py` | Load GAIA validation from HF; deterministic 110/55 split |
| `client.py` | Async harness API client. HTTP only, no business logic |
| `runner.py` | One task to one rollout; concurrency, timeout, trace persistence |
| `scorer.py` | Vendored official GAIA scorer + lenient matcher |
| `detectors.py` | Stage-1 deterministic failure detectors |
| `attribute.py` | Stage-2 LLM root-cause attribution |
| `report.py` | Regressions, scores, failure histogram, ranked fix list |
| `cli.py` | `run` / `analyze` / `report` |

Each run writes `runs/<run_id>/`: `outcomes.json` plus per-task
`events.jsonl`, `meta.json` and `trajectory.md`. All analysis reads those
stored traces — you never re-run a task to ask a new question of it. `runs/`
is gitignored; the traces are local evidence, not repo content.

### The split

Frozen in `gaia_bench/splits/` from the live dataset (165 validation rows, 38
with attachments), seed 20260726. Iterate on dev; touch holdout only to
measure.

| Split | L1 | L2 | L3 | Attachments |
| --- | --- | --- | --- | --- |
| dev (110) | 33 | 60 | 17 | 27 |
| holdout (55) | 20 | 26 | 9 | 11 |

The split is random rather than stratified by level, so holdout skews slightly
easier than dev (36% vs 30% Level 1). The dev-vs-holdout gap is the
overfitting signal, and an easier holdout biases that gap toward looking
healthy.

## How to run

### Setup

The benchmark keeps its own venv. Never run `uv sync` from the repo root while
working here — it reinstalls the pinned `surogates` wheel over the local dev
install, which is the tree you are trying to measure.

```bash
cd benchmarks/gaia
uv venv .venv --python 3.12
uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/pytest          # no network required
```

### Bring up the harness

The benchmark needs two live services: a local ops server, and the harness in
shared mode pointed at it. The harness fetches per-agent runtime config from
the ops server at request time, so ops starts first.

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

Three unrelated credentials, which is easy to get wrong:

| Variable | Authenticates | Issued by |
| --- | --- | --- |
| `SUROGATES_SA_TOKEN` | benchmark → surogates harness | your surogates admin |
| `SUROGATES_PLATFORM_API_TOKEN` | harness → ops server (config lookup) | ops |
| `HF_TOKEN` | dataset download | huggingface.co |

Only `HF_TOKEN` is about GAIA itself, and it must belong to an account that
accepted the terms at https://huggingface.co/datasets/gaia-benchmark/GAIA —
the dataset is gated and loading fails without it.

`SUROGATES_SA_TOKEN` is required because the harness restricts `/v1/api/*` to
service-account principals; a normal user token gets 403 on every route the
benchmark uses. The raw token is returned exactly once by
`POST /v1/admin/service-accounts` (admin permission) and is unrecoverable
afterwards.

`GAIA_BASE_URL` and `GAIA_AGENT_ID` keep the `GAIA_` prefix on purpose:
`SUROGATES_API_URL` and `SUROGATES_AGENT_ID` are already consumed by the
harness's own settings, so reusing those names cross-talks when the harness
and the benchmark share a shell.

### The loop

```bash
gaia-bench run --split dev --limit 10        # pilot: learn cost and shape
gaia-bench run --split dev                   # baseline over 110
gaia-bench analyze dev-006                   # ranked fix list
# ... fix the harness, restart it, re-run ...
gaia-bench run --split dev --only-failing dev-006   # fast re-check
gaia-bench run --split dev                   # full re-measure
gaia-bench report dev-007 --compare dev-006
gaia-bench run --split holdout               # only when reporting
```

### Reading a result honestly

- **Pin the model.** Sessions run under the `surogate` / `surogate-pro` tier
  sentinels, which resolve per request. A run whose calls were served by a
  different model is not comparable — check the served model before
  attributing a delta to harness changes (`RESULTS.md` shows a run where this
  bit).
- **A single run is provisional.** Tasks flip on their own. Re-run an affected
  subset 3× before believing a fix.
- **`--only-failing` cannot show you what you broke.** It is a filter, never
  the basis for a claim. Only a full dev re-measure counts.
- **Strict vs lenient.** Lenient accepts a correct answer in the wrong format.
  A gap between them is a prompt fix; no gap means the remaining failures are
  real.

### Known gaps

- `analyze` reports which failures need LLM attribution but does not call an
  LLM. `attribute()` is implemented and tested against an injected completion
  function; wiring it to a concrete endpoint is a one-function change once a
  classifier model is chosen.
- The split is random rather than stratified by level (see above).
