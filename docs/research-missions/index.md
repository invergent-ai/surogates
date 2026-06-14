# Research Missions

A **research mission** turns a long-horizon optimization goal — "improve this
benchmark", "beat the leaderboard overnight" — into a cumulative tree search.
A coordinator grows a durable **Idea Tree** of hypotheses; ephemeral executors
implement and evaluate each hypothesis in an isolated git worktree; verified
gains merge into a protected trunk only after an independently re-run held-out
eval confirms the improvement. It is the Surogates port of
[Arbor](https://github.com/RUC-NLPIR/Arbor): the method, hosted on the existing
[missions](../tasks/index.md#missions) machinery.

Research missions are launched with [`/auto-research`](../commands/index.md#auto-research),
a thin alias over `/mission` that creates a **research-kind** mission. The
deterministic spine (the tree, the dispatch gates, the merge gate, the wake-time
harvest) lives in the harness; the *judgment* (ideation quality, decide policy,
executor briefs) ships as the `research` skill bundle. Determinism is enforced
by code; the intelligence is steered by skills.

This document covers the architecture, the lifecycle of one cycle, the three
tools, the durable state model, steering, and the operational guardrails.

---

## Quickstart

A research mission optimizes **any git repo in the session workspace whose eval
prints `{"score": <float>}` on its last line**, with a **dev** split (iterate)
and a held-out **test** split (merge decisions). Here is the smallest end-to-end
run.

> You never touch the workspace yourself — it lives in the session sandbox. You
> chat; the agent runs everything there and reports back.

### 1. Create a tiny benchmark

Paste this whole message into a normal chat session. It tells the agent to build
the benchmark and report the baseline. Copy everything inside the box — it's
plain text with no Markdown fences, so it passes the message scanner cleanly.

```text
Create a tiny benchmark for a research mission, then run the baseline eval and tell me the score. Run these commands in the sandbox exactly as written:

mkdir -p /workspace/bench && cd /workspace/bench
printf 'def predict(t):\n    return "neg"\n' > solver.py
cat > eval.py <<'PY'
import json, sys, solver
split = sys.argv[2] if len(sys.argv) > 2 else "dev"
rows = [json.loads(l) for l in open(f"data/{split}.jsonl")]
hit = sum(solver.predict(r["text"]) == r["label"] for r in rows)
print(json.dumps({"score": round(hit / len(rows), 3)}))
PY
python3 - <<'PY'
import json, os
POS = ["great","excellent","wonderful","amazing","brilliant","fantastic","superb","delightful"]
NEG = ["terrible","awful","boring","horrible","dreadful","bad","poor","dull"]
def make(start, n):
    rows = []
    for k in range(n):
        i = start + k
        pos = i % 2 == 0
        word = (POS if pos else NEG)[(i // 2) % 8]
        if i % 8 in (0, 5):                      # ~25% negated; flips the label
            text, label = f"not {word}", ("neg" if pos else "pos")
        else:
            text, label = f"the movie was {word}", ("pos" if pos else "neg")
        rows.append({"text": text, "label": label})
    return rows
os.makedirs("data", exist_ok=True)
open("data/dev.jsonl","w").write("\n".join(map(json.dumps, make(0, 64))))
open("data/test.jsonl","w").write("\n".join(map(json.dumps, make(64, 64))))
PY
git init -q && git config user.email bench@local && git config user.name bench && git add -A && git commit -q -m bench
python3 eval.py --split dev
```

The agent should reply with `{"score": 0.5}` — the always-"neg" baseline. Unlike
a memorized toy set, this benchmark **generalizes**: dev and test are drawn from
the same vocabulary, so a real mechanism beats 0.5 on the held-out split too. A
keyword lexicon scores ≈0.75, and adding negation handling (`not great` → neg)
reaches ≈1.0 — so the merge gate has something genuine to validate and merge.

### 2. Launch the run

Send (the `Rubric:` block is required — it's what the run is graded against):

```text
/auto-research repo=/workspace/bench baseline=0.5 baseline_test=0.5 max_iterations=20 Improve classification accuracy by editing solver.py. Dev eval: python eval.py --split dev. Held-out eval: python eval.py --split test.

Rubric: Satisfied when the held-out test score beats 0.5 with at least one merged experiment and a final report; never on dev scores or prose alone.
```

### 3. Watch it run

The coordinator proposes hypotheses, hands each executor an isolated copy of the
repo to try on the **dev** split, and merges a change into trunk **only** after
it re-runs the **held-out test** itself and confirms the gain. Expect it to land
the keyword lexicon first (held-out ≈0.75 > 0.5 → merged), then improve on it
with negation handling (≈1.0), pruning the dead ends. It writes `REPORT.md` when
done. Steer any time with plain chat, or `/auto-research pause | resume | cancel`.

Verified gains are real commits on the run's `trunk` branch in your repo;
promote them when satisfied (`git merge research/run-…/trunk`).

---

## 1. What makes it work — the four enforced mechanisms

1. **Tree memory as system of record.** Run state lives in the `idea_nodes`
   table, not the conversation. A *constraints block* (tree shape, root insight,
   pruned lessons, validated findings, budget) is re-read at the start of every
   cycle, so the loop survives context compression.
2. **Real-experiment discipline.** Executors iterate on a **dev** split; a change
   reaches trunk only after the merge tool *independently re-runs* the
   **held-out test** split. The coordinator cannot self-report a score onto
   trunk. Failed and timed-out experiments still spend budget — a timeout is
   evidence, not a free retry.
3. **Isolated, reversible experiments.** Every hypothesis is a branch in a
   throwaway git worktree off a protected trunk. The branch survives worktree
   removal; trunk advances only through a verified merge.
4. **Insight backpropagation.** After every experiment its lesson is propagated
   up the ancestor chain — deterministically at harvest (crash-safe) and
   LLM-synthesized inside the coordinator's tool calls — so later ideas start
   from what the whole subtree has learned.

---

## 2. Architecture

```
user ── /auto-research repo=… baseline=… <goal>  + Rubric: …
        │  (intake skill refined the goal into this command first)
        ▼
research-kind mission  (Mission row + research_runs row + idea_nodes ROOT)
        │  session.config: active_mission_id, active_research_run_id,
        │                  coordinator, strict_coordinator, research_coordinator
        ▼
Coordinator session  (strict — reads allowed, code edits/terminal stripped)
   tools: delegation suite + idea_tree + dispatch_experiments + merge_experiment
        │ dispatch_experiments(node_keys=[…])  → server-side worktree + brief
        ▼
spawn_task executors  ("arbor-executor", one worktree each, dev-split eval)
        │ worker_complete(metadata={node_key, score, insight, result, branch})
        ▼
pre-LLM harvest (deterministic, fail-open): fold finished experiments into the
   tree, concat-propagate, clean up worktrees, inject digest + constraints +
   any convergence intervention before the coordinator's next turn
        ▼
research mission evaluator: SKIP while experiments are in flight; otherwise the
   rubric judge over the SQL tree leaderboard, gated so `satisfied` requires a
   machine-written held-out improvement AND a finished report task
```

One run ⇔ one Mission ⇔ one coordinator session ⇔ one Idea Tree ⇔ one git repo
under `/workspace`. Executors share the coordinator's sandbox pod and its
S3-durable `/workspace`.

---

## 3. The Idea Tree and run state

Two sidecar tables (the `missions` table is never altered):

- **`research_runs`** — one row per run. `meta` (JSONB) mirrors Arbor's
  `tree.meta` over a closed key set: `eval_cmd`, `eval_cmd_test`,
  `metric_direction`, `eval_timeout`/`eval_retries`, `baseline_score`/
  `test_baseline_score`, `trunk_score`/`test_trunk_score`, `max_cycles`,
  `max_tree_depth`, `max_parallel`, `merge_threshold`, `hitl_mode`,
  `protected_paths`, `required_outputs`, and `convergence_*` thresholds. The
  machine-score keys (`test_trunk_score`, `trunk_score`, `test_baseline_score`)
  are writable **only** by the merge / baseline paths — `idea_tree(set_meta)`
  from the coordinator rejects them, so progress cannot be faked.
- **`idea_nodes`** — one row per hypothesis. Dotted-decimal `node_key`
  (`ROOT`, `1`, `1.2`); `status ∈ pending | running | done | failed | merged |
  pruned`; absolute dev `score`; `insight`; `code_ref` (branch); `task_id`
  (the experiment-ledger link to the executor task).

Both tables arrive via `create_all` — no Alembic migration. The DB is the
source of truth; a markdown twin and `REPORT.md` under `/workspace/.arbor/` are
audit/display artifacts.

**Cycle budget.** `cycles_spent` counts nodes in a terminal state
(`done`/`failed`/`merged`/`pruned`) — failed experiments spend budget. The
mission's `max_iterations` defaults to `2 × max_cycles`.

---

## 4. The three tools

All three are HARNESS-routed and visible **only** on the coordinator session
while a research run is active — executors stay tree-blind.

- **`idea_tree`** — `view` (the constraints block / a compact leaderboard),
  `add` (machine-warns on a non-four-line hypothesis), `update`, `prune`
  (recursive, lesson backpropagated), `set_meta` (closed keys; machine-score
  keys rejected), `record_from_task` (fold an executor result by task id, then
  LLM-synthesize ancestors), `requeue` (infra-failure escape hatch — does NOT
  refund the spent cycle), `propagate` (LLM-synthesize a node's ancestors), and
  `report` (render `REPORT.md`).
- **`dispatch_experiments(node_keys=[…])`** — validates the cycle budget, tree
  depth, leaf-ness, parallelism, and duplicate keys; creates the worktree
  **server-side** (`git worktree add` off trunk, trunk created lazily on first
  dispatch); builds the executor brief (the dev `eval_cmd` is rendered; the
  held-out `eval_cmd_test` is **never** put in front of an executor); and spawns
  one `arbor-executor` task per node with `max_attempts=1`. `action="baseline"`
  measures the unmodified repo on dev to seed `baseline_score`.
- **`merge_experiment`** — the bypass-proof gate. `start(node_key)` launches the
  held-out eval **detached** (so no sandbox exec is held open across it);
  `status(node_key)` reads the result, applies the direction-aware improvement +
  `merge_threshold` + protected-paths guards, and on success does `git merge
  --no-ff` and writes `test_trunk_score`. **The schema accepts no score
  argument** — the held-out number is machine-measured here, with an
  `eval_retries` policy for flaky evals and a staleness re-run for evals
  orphaned by a pod recycle.

---

## 5. The arbor cycle, turn by turn

1. **Intake** (`/arbor-research <goal>`, a normal full-tool session): discover
   the repo / eval / dev+test splits, measure the baseline on both splits, and
   emit a ready-to-send `/auto-research` command with the Research Contract and
   a machine-anchored rubric. Intake never starts the run — the user sends the
   command.
2. **Create**: `/auto-research` creates the research mission, writes the
   baselines server-side, stamps the session config, and preloads the
   `arbor-coordinator` skill.
3. **INIT** (coordinator turn 1): `idea_tree(set_meta …)` with the contract
   values, then OBSERVE → IDEATE → dispatch.
4. **IDEATE** (hard-gated): load `arbor-ideate`, write the first-principles
   PROBE BLOCK, then `idea_tree(add)` 1–3 four-line hypotheses.
5. **DISPATCH**: `dispatch_experiments(node_keys=[…])`, then **end the turn** —
   the harvest folds results before the next wake.
6. **Executors** run in their worktrees, evaluate on dev, and
   `worker_complete` with structured metadata (and optionally a `FAIL`/`RESULT`
   board note for siblings to reuse).
7. **Harvest + OBSERVE + DECIDE** (next wake): the deterministic pre-LLM harvest
   folds finished experiments, propagates insights, cleans up worktrees, and
   injects the digest + constraints block + any convergence intervention. The
   coordinator (read tools restored for OBSERVE) decides:
   `merge_experiment(start)` for a winner, `idea_tree(prune)` for a dead end.
8. **FINALIZE** (budget spent / convergence STOP / target hit): merge the best,
   `idea_tree(report)`, then spawn one report task that creates the artifact and
   completes with `metadata={"report": true}`.

---

## 6. Convergence steering

A convergence detector watches score velocity and consecutive non-improving
experiments. On a plateau it escalates **WARNING → PARADIGM SHIFT → STOP** and
injects an intervention (with the Exploit / Combine / Leap suggestions and the
list of exhausted parents) into both the harvest digest and the evaluator
feedback. At PARADIGM SHIFT the next idea must change approach family; at STOP
the coordinator finalizes unless it can justify a genuinely novel direction.
Thresholds are tunable via `convergence_*` meta keys; the check is fail-open.

---

## 7. Human-in-the-loop

`meta.hitl_mode` (shown in the constraints block) sets how much the coordinator
asks:

| Mode | Behavior |
|---|---|
| `auto` | Fully autonomous (default). |
| `direction` | `ask_user_question` for the direction at the start of each IDEATE round. |
| `review` | `ask_user_question` for approval before dispatch and before finalizing a merge. |

Mid-run chat is a nudge, not a pause. `/auto-research pause`, `resume`, and
`cancel [--cascade]` control the run (the control verbs delegate to the same
mission handlers). Questions surface through the [agent inbox](../agent-inbox/index.md)
over web / Telegram / Slack and survive pod churn.

---

## 8. Resume and durability

Every layer is durable: the run, tree, and mission rows are in Postgres; the
conversation is the append-only event log (replay is resume); tasks survive in
the task layer; branches survive in git; worktree directories are
reconstructible. After any crash the harvest folds whatever finished at the next
wake and the evaluator's continuation restarts the cycle. A run that outgrows a
single mission chain continues over the same durable tree.

Research-specific events on the coordinator session — `research.defined`,
`research.dispatched`, `research.harvested`, `research.merged`,
`research.pruned`, `research.converged`, `research.report` — are surfaced on the
mission dashboard's activity feed.

---

## 9. The `research` skill bundle

Hub-published under `skills/research/`:

| Skill | Role | Loaded |
|---|---|---|
| `arbor-research` | Intake → Research Contract → `/auto-research` command | user types `/arbor-research` |
| `arbor-coordinator` | The OBSERVE→IDEATE→SELECT→DISPATCH→DECIDE protocol | preloaded on `/auto-research` |
| `arbor-ideate` | Hard-gated ideation (PI mindset, probe, four-line format) | `skill_view` at IDEATE |
| `arbor-merge-discipline` | DECIDE doctrine (merge/prune/combine/finalize) | `skill_view` at DECIDE |
| `arbor-executor` | Implement+evaluate one hypothesis in a worktree | preloaded on executor task workers |

The `arbor-executor` sub-agent (`AGENT.md`) must be available through one of the
[sub-agent](../sub-agents/index.md) layers — a per-agent Hub bundle (the
recommended ops feature-pack path, behind a per-agent `research_enabled` flag),
an org/user agents bucket file, or an `agents` DB row.

---

## 10. Operational guardrails

- **Strict coordinator.** The coordinator runs in strict mode: code edits,
  terminal, web, and browser are stripped. A research carve-out restores
  read-only file tools (`read_file`/`search_files`/`list_files`) for OBSERVE
  forensics; writes and execution stay stripped.
- **No score injection.** `merge_experiment` has no score parameter; a held-out
  improvement is machine-measured. The evaluator demotes a prose `satisfied`
  to `needs_revision` unless `test_trunk_score` improved on `test_baseline_score`
  AND a report task is done; it demotes a noisy `failed`/`blocked` unless the
  budget is genuinely exhausted.
- **No iteration burn while in flight.** The evaluator skips entirely while any
  experiment is running.
- **Fail-open intelligence.** LLM synthesis and the convergence check are
  wrapped so a provider outage degrades to the deterministic result and never
  breaks the loop.
- **Pod limits.** In-pod experiments are bounded by the ~1 h sandbox pod
  deadline; the executor skill mandates checkpoint-to-`/workspace` and scopes
  experiments accordingly. Long (>1 h) training via the ops training-runs path
  is a later increment.

See [Commands](../commands/index.md#auto-research) for the slash-command
reference and [Tasks → Missions](../tasks/index.md#missions) for the underlying
mission machinery.
