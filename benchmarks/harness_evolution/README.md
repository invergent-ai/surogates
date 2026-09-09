# Harness evolution

A bounded harness optimizer for **Standard** and **Pro**, with offline planning
and tests. It runs
the Surogates harness through the ClawEval, Workspace-Bench,
EnterpriseOps-Gym, DABstep, and GAIA clients. A frontier proposer edits an explicit set of product source files;
fresh paired benchmark runs decide whether a candidate becomes the search
frontier. Model weights stay fixed.

This implements a [StarHarness-style](https://arxiv.org/html/2608.24804v1)
hill-climbing protocol, not the authors'
unreleased package. It extends the patterns in `../gepa`: independent
benchmark clients, search/selection separation, regression checks, and
invalidating unhealthy runs. It does not import or modify the GEPA package.

**No experiment deploys or edits the working checkout.** Outputs are source
snapshots, patches, and reports. The accepted frontier is a development
candidate; inspect the patch and finish evaluation before shipping it.

## What is implemented

- Two configurable model bindings, with model/checkpoint IDs and settings
  recorded in experiment inputs. Runtime model IDs are checked against
  `llm.request` events; the deployment hook must pin checkpoint revisions
  and apply the declared reasoning settings.
- Catalog extraction from saved benchmark results and reproducible grouped,
  stratified task partitioning. Existing Workspace, DABstep, and GAIA holdout IDs are protected.
- Committed source snapshots: local edits, environment files, benchmark
  graders, and answers are excluded from candidate source.
- Allow-listed existing-file edits, Python syntax validation, applicable
  unified patches, and a single-search-task improvement gate.
- Both tiers evaluated with freshly interleaved frontier/candidate controls.
  Claw uses the existing safety/completion pass criterion; Workspace uses
  per-task rubric fraction. EnterpriseOps requires every SQL verifier to pass;
  DABstep uses its frozen reference key; GAIA uses strict answer matching.
  Benchmark means are macro-averaged. These are
  internal comparison metrics, not pooled public leaderboard scores.
- Pro gain threshold, majority-of-repetitions requirement, Standard and
  task-family regression floors, per-task Claw safety regression checks,
  explicit regression guards, and a latency
  bound. Missing scores/timing and infrastructure/grader errors invalidate
  comparisons; ordinary task-budget timeouts remain failures.
- Journal, reviewable cumulative `best.patch`, resume after interruption,
  proposal and elapsed-time budgets, graceful stop, and a one-time final test.
- A parameterized deployment-hook protocol, a Docker Compose hook, and
  benchmark CLIs executed in their own virtual environments.

## Setup and offline planning

Use a separate environment; do not `uv sync` at the product repository root.

```bash
cd benchmarks/harness_evolution
uv venv .venv --python 3.12
uv pip install --python .venv/bin/python -e '.[dev]'
.venv/bin/pytest -q
.venv/bin/harness-evolve plan examples/pilot.json
```

`plan` does not start containers, contact models, or run benchmarks. It lists
task counts, the maximum rollout count, editable files, and missing live
configuration. The example manifest has **three illustrative Workspace tasks**;
it is not a representative evaluation suite. Replace it before a real search.
Environment/model IDs are intentionally parameters, not inferred from local
development or production configuration.

Build a real catalog from repeated **development** runs of the target models:

```bash
.venv/bin/harness-evolve catalog \
  --run workspace_bench=../workspace_bench/runs/dev-001 \
  --run claweval=../claweval/runs/dev-001 \
  --output catalog.local.json
.venv/bin/harness-evolve split catalog.local.json \
  --seed 42 --holdout-fraction 0.2 --output tasks.local.json
```

Catalog extraction emits identifiers and diagnostic descriptors, never
answers/rubrics. Review families, difficulty, and failure modes. Add a shared
`group` identifier for related task variants **before splitting**; groups
cannot cross partitions, including across benchmarks. `sealed_holdout: true`
keeps existing reservations. Already-inspected tasks are development data,
not a new pristine holdout. Small strata may be unbalanced; inspect the
manifest rather than treating its seed as evidence of representativeness.

Mark stable previously successful selection tasks with `guard: true`.
Holdout task records remain in the evaluator's private manifest. Only search
events are supplied to the proposer. During search, the journal exposes
accept/reject status and hypotheses to the proposer, not selection task IDs,
per-task scores, rubric feedback, or file paths.

## Configuration

Copy `examples/pilot.json` to an experiment-specific `.local.json` and adjust
paths relative to that file. Configure:

| Parameter | Purpose |
| --- | --- |
| `repository`, `revision` | Product checkout and base commit; `HEAD` resolves once |
| `tasks` | Frozen manifest containing the selected benchmarks and all partitions |
| `dataset_revisions` | Immutable 40-character Hugging Face commits for Workspace, EnterpriseOps, DABstep and GAIA; Claw uses its existing `PIN` |
| `benchmark_data` | EnterpriseOps imported `tasks_dir` and `seed_root`; DABstep private `answer_key` file |
| `benchmark_profiles` | Operator-defined tool profile names; receipt must attest profile names and distinct tier agents |
| `models.pro`, `models.standard` | Model, immutable revision, actual `served_model` recorded by the harness, reasoning settings |
| `allowed_files` | Exact existing harness/tool `.py` or `.md` files the proposer may replace |
| `benchmark_pythons` | Each existing benchmark's own interpreter |
| `runtime.start`, `runtime.stop` | Operator-owned argv arrays; no shell interpolation |
| `proposer` | Endpoint/key environment variable names and a proposer model ID |
| `policy` | Proposal/repetition/time budgets and promotion thresholds |

The proposer endpoint uses the existing OpenAI-compatible Chat Completions
interface. It returns a hypothesis, a failing search task, and complete file
replacements; it receives no filesystem or execution tools. For offline
replay or human-authored candidates, replace the proposer configuration with
`{"recorded": ["proposal-001.json", "proposal-002.json"]}`. Example proposal:

```json
{
  "hypothesis": "Check the output exists before declaring completion",
  "smoke_task": "workspace_bench:100",
  "smoke_tier": "pro",
  "files": {
    "surogates/harness/prompts/guidance/working_principles.md": "COMPLETE REPLACEMENT FILE, INCLUDING ITS FRONTMATTER\n"
  }
}
```

Credentials are read from environment variables and never included in the
proposer packet. Synthetic benchmark traces can still contain whatever the
agent read; inspect a search packet before choosing an external proposer.

## Runtime integration

A runtime is created per evaluation batch and always torn down, including
after partial startup. The default Compose hook needs these parameters:

- `EVOLVE_COMPOSE_FILE`: your dedicated test stack's Compose file.
- `EVOLVE_PRO_AGENT_ID`, `EVOLVE_STANDARD_AGENT_ID`: distinct test agents.
- `EVOLVE_PROJECT_ID`: the test Ops project.
- Existing benchmark service-account and grader variables documented by
  `../claweval` and `../workspace_bench`. Claw also needs its pinned vendor
  checkout via `CLAWEVAL_HOME` and the judge configuration. Set judge model
  IDs explicitly and keep judge deployments and benchmark virtual environments
  fixed throughout the experiment; runtime receipts cannot verify their weights.

The Compose definition is environment-specific and is **not provisioned by
this package**. Its contract is:

1. Mount `${EVOLVE_SOURCE_DIR}` read-only as the product source in every
   harness service. Explicitly use it as `PYTHONPATH`; do not accidentally
   import the image's installed wheel instead. No mount may expose the
   controller, benchmark code, answer keys, or experiment artifacts.
2. Provide `api:8000` and `ops:8888`, each published to a single loopback
   address. The hook discovers the actual ports. Use health checks so
   `docker compose up --wait` verifies readiness.
3. Use project-scoped databases, Redis queues, workspace/memory storage, and
   mock services. Pass `${EVOLVE_MODEL_BINDINGS_JSON}` to an init job that
   provisions the configured test agents and their pinned model/settings
   bindings. The hook sets this parameter from the experiment's `models`
   object. No shared production workers.
4. Let Compose own all service lifetimes. The hook removes only its generated
   `evolve-<uuid>` project and its volumes. No background host worker may
   survive an evaluation batch.

The supplied hook does not validate arbitrary Compose configurations or
prove container/network isolation. The operator owns that deployment
boundary. The controller checks source hashes, private endpoint addresses,
distinct agent bindings, model IDs observed in traces, and exact task
coverage. It cannot prove a hosted alias still serves identical weights.

For a different orchestrator, provide start/stop hooks taking `{request}`
and `{receipt}` file paths. `{python}` and `{repository}` are also available.
Start receives `runtime_id`, `source_dir`, `source_hash`, and the declared
`models`. It must deploy/pin those bindings and emit:

```json
{
  "runtime_id": "evolve-<the supplied UUID>",
  "source_hash": "<the supplied source hash>",
  "models": {"pro": {"...": "exact declared binding"}, "standard": {"...": "exact declared binding"}},
  "base_url": "http://127.0.0.1:18000",
  "ops_base_url": "http://127.0.0.1:18888",
  "project_id": "test-project",
  "agents": {"pro": "pro-agent-id", "standard": "standard-agent-id"}
}
```

Stop must be idempotent and must clean up by `runtime_id` even if startup
failed before writing a receipt. Receipts are trusted deployment attestations.
The runtime protocol is intentionally separate from proposal generation.

## Run, inspect, resume, finalize

```bash
.venv/bin/harness-evolve run pilot.local.json --run-dir runs/pilot-001
.venv/bin/harness-evolve run pilot.local.json --run-dir runs/pilot-001 --resume
touch runs/pilot-001/STOP
```

Remove `STOP` before resuming. A failed/in-flight proposal consumes its
proposal slot; partial comparisons are never promoted or reused. Completed
frontiers and their search evidence survive resume. Changes to config, task
manifest, recorded proposals, controller code, baseline source, or evaluator
invalidate resume. After a hard kill, resume conservatively charges downtime
against the elapsed-time budget. Cleanup has its own timeout; provider request
timeouts bound inactivity rather than strictly enforcing a dollar/token budget.

Every accepted candidate gets a new snapshot; the original checkout stays
untouched. Read `report.md`, `best.patch`, and private
`attempts/<n>/comparison.json`. Runtime logs and per-task benchmark artifacts
are under `evaluations/`. Search traces include failures as diagnostic
evidence; they are not filtered into an SFT dataset during this phase.

Run the holdout explicitly once after selecting the frontier:

```bash
.venv/bin/harness-evolve final-test pilot.local.json --run-dir runs/pilot-001
```

This measures seed and frontier on both models, writes `final-baseline.json`
and `final-candidate.json`, adds `final-summary.json` and a holdout table to
the report, and permanently seals further search. It marks
the holdout consumed before execution, so even interrupted final tests
cannot silently become another search signal. A new research question needs
a new uncontaminated evaluation protocol.

The rollout budget includes controls: with 20 selection tasks, 3 paired
repetitions, 2 models and 10 candidates, selection alone can require **2,400
task rollouts**, plus search/smoke/final runs. `plan` shows the upper bound.
Dollar costs and provider-side token ceilings are deployment-specific and
are not enforced by this first version. Latency measurements cover task
execution, not container startup or rubric-judge cost.

## Limits

This version supports five benchmark adapters and a single hill-climbing
frontier. It does not include tree search, automatic benchmark-family
annotation, automatic production rollout, or weight training.
Repeated selection gains are provisional, especially on
small task sets; final results need their own uncertainty analysis.

## Enterprise suite

`examples/enterprise.json` adds the recommended suite: EnterpriseOps, Claw,
Workspace and DABstep for search/selection, plus GAIA selection guards and
holdout tasks. It is an **illustrative manifest**, with placeholder EnterpriseOps
and Claw IDs and a few existing split IDs for the other benchmarks. Replace
these with a representative task manifest before starting an experiment.

```bash
.venv/bin/harness-evolve plan examples/enterprise.json
```

Import one pinned EnterpriseOps release using the benchmark's own environment:

```bash
cd ../enterpriseops_gym
uv pip install --python .venv/bin/python -e '.[dev,data]'
.venv/bin/eogbench import-tasks --revision "$EOG_DATASET_COMMIT" \
  --mode oracle --output /path/to/private/eog-import
```

The import publishes an identifier-only `catalog.json`, a hashed `import.json`,
and normalized task files. It inventories unsupported rows with reasons;
multiple gyms and non-SQL verifiers are excluded explicitly. The public oracle
release had 649 rows at the survey date; that is not a promise that every row
is supported or that the full 1,150-task benchmark is public. Pick a single
tool mode per experiment. Related variants retain a shared task group.

Set `benchmark_data.enterpriseops_gym.tasks_dir` to that import and `seed_root`
to the directory containing its referenced seed SQL paths. Set
`benchmark_data.dabstep.answer_key` to a reviewed key in the existing
`dabbench` format (`entries[task_id].answer` and `.source`). These local
references are derived from accepted submissions, not official hidden answers.
The controller copies requested EnterpriseOps tasks/seeds and the DABstep key
into `private_data`, hashes them, and detects changes on resume and evaluation.
The candidate must have no mount or network route to this private directory.

For GAIA, provide `HF_TOKEN` with accepted dataset access and select tasks
supported by the deployed tools. Unsupported-capability omissions invalidate
controller coverage. Task rows and attachment downloads use the same pinned
revision. DABstep tasks and context files also share a pinned revision.

The Compose hook additionally accepts:

- `EVOLVE_BENCHMARK_AGENTS_JSON`: mapping from benchmark to distinct `pro` and
  `standard` agent IDs. Its init job must provision each declared tool profile
  and model binding. Example: `{"enterpriseops_gym":{"pro":"eog-pro","standard":"eog-standard"}}`.
- `EVOLVE_EOG_SERVICES_JSON`: mapping from task domain to a Compose service and
  internal port, for example `{"csm":{"service":"gym-csm","port":8001}}`.
  Domain services must belong to the disposable project and publish loopback
  ports. The hook discovers actual gym URLs and includes them in the receipt.

`EVOLVE_BENCHMARK_PROFILES_JSON` is passed to the init job from the config.
Profile names are deployment parameters, not built-in presets. EnterpriseOps
needs a gym-only profile: no shell/browser path to gym administration or seed
files. The per-task proxy filters tool discovery and execution, applies task
identity/auth context, and rejects administrative HTTP paths. Claw needs its
mock-tool profile; Workspace/DABstep need their artifact/analysis tools; GAIA
needs its research and file tools. Standard and Pro use the same profile for
each benchmark. Keep benchmark dependencies, vendor checkouts and judges fixed.

Custom hooks return `benchmark_profiles`, `benchmark_agents`, and, for
EnterpriseOps, a `gym_urls` map alongside the ordinary receipt. Provide the
benchmark's Ops authentication and adapter exposure settings separately. The
MCP proxy must be able to reach the local benchmark adapter; a Docker container's
loopback does not automatically reach the host's loopback. The operator-owned
stack must provide that connectivity. No environment is provisioned by `plan`.

The integrations are covered by offline parser, HTTP proxy, isolation, grading,
partition and replay tests. Live gym fidelity and performance remain to be
validated once environment and model IDs are selected. TheAgentCompany and
Agents' Last Exam remain second-stage work; Galileo and ECBench require the
adapter/evaluator changes described in [the survey](../BENCHMARK_SURVEY.md).
