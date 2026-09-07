# Prompt optimiser (GEPA)

## Description

Rewrites one harness prompt fragment with [GEPA](https://github.com/gepa-ai/gepa),
scoring every candidate by running real GAIA tasks through the harness.
GEPA proposes, the benchmark scores, and the fragment that survives
selection is written out for review.

It optimises **platform prompt text**, not per-agent skills — the fragments
under `surogates/harness/prompts/`, which ship to every agent on every
channel. That reach is the constraint the whole design answers to: a
rewrite that wins on 17 research tasks and regresses everyone else is a
loss, so guards sit inside the selection set and the holdout is never
touched during a search.

**This produces a search result, not a measured improvement.** A winning
candidate still owes the GAIA benchmark's own bar before anyone believes
it — see [Discipline](#discipline).

### Layout

| File | Responsibility |
| --- | --- |
| `sets.py` | Partition prior GAIA runs into train / val / guard, drop flippers |
| `harness.py` | Build a prompts tree per candidate; restart the worker against it |
| `evaluate.py` | Roll out tasks, score, build reflection feedback, gate on platform health |
| `optimize.py` | GEPA wiring, objective/background, CLI |

The GAIA benchmark is used as a **library**, not over its CLI. Its run ids
auto-increment off the entry count of `benchmarks/gaia/runs/`, so several
hundred evaluations driven through `gaia-bench run` would permanently shift
every future run id. Importing it puts the traces under `benchmarks/gepa/runs/`
instead. The no-product-imports rule still holds here, enforced by
`tests/test_isolation.py`: the optimiser writes a file and starts a
process, it does not link against the tree it measures.

## The design

### Sets: only tasks that agreed across runs

A single run cannot tell a real failure from a coin flip — GAIA's README
records tasks flipping 7/10 → 4/10 between runs hours apart. Across the
four local dev runs, the 110 tasks partition as:

| | count | role |
| --- | --- | --- |
| passed every run | 49 | regression guard |
| failed every run | 29 | the training signal — the seed scores 0 |
| disagreed | 32 | **excluded from every set** |

Flippers are dropped, not down-weighted. They are the mechanism by which
noise is accepted as progress: against coin flips, a candidate that changes
nothing still "improves" some of the time.

The 29 stable failures split by whether a deterministic detector could
explain them. The 17 that carry a behavioural flag (`no_final_answer`,
`no_tool_use`, …) are reflected on; the 12 that carry none are held back
into the selection set alongside 12 guards. GEPA therefore selects on
tasks it never reflected on, mixed with tasks that must not break.

### Score: strict pass, and nothing else

1.0 from the official GAIA scorer or 0.0. No partial credit for "produced
an answer" or "called a tool" — those are exactly what the deterministic
detectors flag, so paying for them buys a candidate points for emitting any
string at all. The behavioural detail reaches the reflection LM as side
information instead, where it can inform a rewrite without being something
to game.

### The feedback never contains the answer

The reflection LM reads the feedback and then writes the prompt. A gold
answer reaching it is a direct route to a fragment that has memorised the
benchmark. `side_info()` is passed the task's *level* and no other task
metadata, so the leak is structurally impossible rather than merely
avoided — and `tests/test_evaluate.py` holds that line.

### A sick platform is not a bad candidate

Sessions that error, time out, or come back empty from the provider say
nothing about the prompt. Scoring them 0 corrupts the acceptance anchor for
every later comparison, which is how an earlier optimisation run here was
lost. When a quarter of a batch fails infrastructure-shaped, the run aborts
with `PlatformUnhealthy` rather than recording a verdict.

## How to run

### Requirements

A **local** harness, not prod: the optimiser restarts the worker between
candidates. Bring up ops and the harness exactly as in
[../gaia/README.md](../gaia/README.md#against-a-local-harness-instead), then
**stop the worker** — this tool starts and stops it itself, and refuses to
run while another one is alive, because a leftover worker silently serves
part of the batch with the shipped prompt.

On top of the GAIA variables (`SUROGATES_SA_TOKEN`, `GAIA_BASE_URL`,
`GAIA_AGENT_ID`, `HF_TOKEN`), the reflection LM needs the same
OpenAI-compatible endpoint `gaia-bench analyze` uses:

```
GAIA_JUDGE_BASE_URL=https://api.surogate.ai/proxy/services/_model/<model id>/v1
GAIA_JUDGE_KEY=<sk-agent key scoped to that model>
GAIA_JUDGE_MODEL=<model id>            # optional; defaults to claude-sonnet-5
```

### Setup

Its own venv, like the benchmark next door. Never `uv sync` from the repo
root while working here — it reinstalls the pinned `surogates` wheel over
the local dev install, which is the tree being measured.

```bash
cd benchmarks/gepa
uv venv .venv --python 3.12
uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/pytest          # offline suite, no network
```

### Running

```bash
# what the runs imply, without spending anything
.venv/bin/promptgepa sets --runs dev-018,dev-021,dev-022,dev-023

# the search
set -a; source ../gaia/.env; set +a
.venv/bin/promptgepa optimize --runs dev-018,dev-021,dev-022,dev-023

# land the winner in the tree, then read the diff
.venv/bin/promptgepa apply runs/opt-001
```

Each run writes `runs/opt-NNN/`: `best_fragment.md`, `result.json`, the
per-candidate traces and scores under `candidates/`, GEPA's own state under
`gepa/`, and the worker log under `harness/`.

The budget defaults to `--proposals 18` × the size of the selection set,
because every candidate is scored on the whole set. Setting a flat
`--budget` too low is the standard way to misuse this API: the search stops
after one proposal and reports the seed back as the winner. The CLI warns
when only one candidate was proposed.

Expect roughly **$320–400** of rollouts for a default run (18 sweeps of a
24-task selection set, plus reflection minibatches), and 6–9 hours at the
default `--concurrency 4`.

**Do not raise concurrency without watching memory.** `--concurrency 8`
OOM-killed a 31 GB box mid-run, taking the ops server, the harness API and
the search with it: agent sessions hold headless browsers (5 of 18
concurrent tasks had one open), and the local stack shares the machine.
Four is the GAIA benchmark's own default and what its recorded runs used.

### When a run dies

There is no resume in this release of `gepa`, so the on-disk evaluation
cache is what stands in for one. It is keyed `(candidate, example)` and
lives under the run dir, so relaunching against the same one replays every
pair already scored instead of paying for it again:

```bash
.venv/bin/promptgepa optimize --runs dev-018,dev-021,dev-022 \
    --run-dir runs/opt-001          # same dir: cached pairs are free
```

Cache hits do not consume `max_metric_calls`, which is why the run also
carries a proposal cap and a wall-clock timeout — without them a converged
search could spin on cache hits forever. `--no-cache` turns it off.

To stop a run **gracefully**, touch the stop file GEPA watches rather than
signalling it:

```bash
touch runs/opt-001/gepa/gepa.stop
```

A killed run cannot leave a worker behind — the worker is started with
`PR_SET_PDEATHSIG`, so the kernel kills it when the optimiser dies. That
matters more than it sounds: a surviving worker keeps consuming the queue
with the candidate prompt of an experiment that no longer exists.

### Pointing it somewhere else

```bash
# a different fragment
--fragment guidance/working_principles

# a single failure class rather than every flagged failure
--train-flags no_tool_use,no_final_answer

# a different set of source runs, or explicit paths
--runs dev-021,dev-022 --runs /elsewhere/runs/x,/elsewhere/runs/y
```

`sets` refuses rather than returning an empty training set when the runs
contain no stable failure, or none matching `--train-flags` — a search with
nothing to improve reports the seed as "best" after burning the budget,
which reads like a result and is not one.

## Discipline

- **A search result is not a measurement.** The optimiser selects on 24
  tasks. Before believing a candidate: `apply` it, then run **3 candidate
  and 3 seed full-dev runs, interleaved in one session**. Interleaving is
  not optional — identical config lost 9 points across two days in
  `RESULTS.md`, so candidate-now-versus-seed-last-month measures the
  provider, not the prompt.
- **Holdout stays sealed.** The 55-task holdout is the only overfitting
  signal that has not been contaminated by the search. Run it once, at the
  end, and record the row in `../gaia/RESULTS.md`.
- **Read the diff before committing.** The fragment ships to every agent on
  every channel. A candidate that mentions GAIA, a specific site, or a
  specific answer is a defect regardless of its score — the background
  prompt forbids it, and the proposer is not obliged to comply.
- **Stop the worker first.** The tool refuses to start while another is
  running, but it cannot detect one on a different machine pointed at the
  same Redis.
- **Never delete `runs/`.** Run ids count directory entries, so a freed id
  gets reused and two different searches end up sharing a name.
