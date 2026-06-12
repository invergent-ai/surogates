# Arbor on Surogates — Final Integration Proposal

**Status:** Design proposal (no code yet) · **Synthesis of:** hybrid (winning direction) + grafts from native-module and skill-suite, with all judge-caught factual errors corrected and re-verified against `/work/surogates/` and `/work/surogates/study/Arbor/`. · **Revised 2026-06-12** after a post-synthesis adversarial verification pass (native merge-gate fallback, exec-timeout semantics, `failed` node status, eval retries, v3 training surface, line-ref corrections).

---

## 1. Executive summary

**What Arbor is.** Arbor (`/work/surogates/study/Arbor/`) is an autonomous-research agent that runs an OBSERVE→IDEATE→SELECT→DISPATCH→DECIDE loop: a persistent coordinator LLM maintains a durable Idea Tree of hypotheses, dispatches ephemeral executor agents into isolated git worktrees to run real experiments against a dev split, and merges a branch into a protected trunk only after a machine-run, independently re-executed held-out test eval shows direction-aware improvement. Its power comes from four enforced mechanisms — tree-as-memory, insight backpropagation, the bypass-proof merge gate, and worktree isolation — not from its (replaceable) agent runtime.

**Recommended integration.** Host Arbor as a `kind=research` mission inside the existing surogates missions subsystem, launched via a new **`/auto-research`** slash command — a thin alias that creates a research-kind mission (`/mission` parsing itself stays untouched): the strict-coordinator mission session is Arbor's Coordinator, `spawn_task` workers in git worktrees are its Executors, and the mission evaluator loop is its cycle driver. The deterministic spine is three new HARNESS builtin tools (`idea_tree`, `dispatch_experiments`, `merge_experiment`) backed by two new DB tables (`research_runs`, `idea_nodes` — new tables only, no `missions` ALTER), plus a deterministic pre-LLM harvest hook modeled on the board mixin; all judgment (ideation quality, select/decide policy, executor briefs) ships as five Hub-published skills adapted from Arbor's own battle-tested prompt suite. Resume, HITL, pause/cancel, dashboards, and crash recovery are inherited from existing machinery at zero new code.

---

## 2. What makes Arbor work — the irreducible core to preserve

From `study/Arbor/README.md` and verified in source:

1. **Tree memory as system of record** (`src/coordinator/idea_tree.py`): state lives in a small, auto-persisted, LLM-readable tree; the conversation is disposable. The constraints block (`get_constraints_block`, idea_tree.py:358-435 — TREE SHAPE / ROOT INSIGHT / PRUNED LESSONS / VALIDATED FINDINGS) is re-read every IDEATE, making the loop immune to context compression.
2. **Real-experiment discipline**: absolute dev-split scores for iteration; a held-out eval independently re-run *inside* the merge tool whenever `eval_cmd_test` is configured (`src/coordinator/tools/git_ops.py:348-460`). Verification caveat: native *does* accept a `test_score` argument (logged for comparison only, `git_ops.py:325-331, 407-411`) and falls back to the LLM-reported score when no `eval_cmd_test` exists (`git_ops.py:413-420`) — the port removes both the argument and the fallback, making it stricter than native. "Failed runs spend budget" and "timeout is evidence" (`executor_run.py:44-54, 596-646`).
3. **Worktree isolation**: every hypothesis is a branch in a throwaway worktree off a protected trunk; branch survives worktree removal; trunk advances only through the verified merge; direct `git merge` is blocked (`bash.py:106`).
4. **Insight backpropagation** (`tools/tree_ops.py:518-597`): lessons are abstracted up the ancestor chain after every experiment, then re-injected into every IDEATE and every executor brief — failures become constraints, successes become priors.
5. (Supporting) **Quality-gated ideation**: the hard-gated probe/mechanism/kill-filter skill flow (`src/skills/idea_drafting.md`) with the exact 4-line hypothesis contract — machine-warned at add time in the skill-suite helper (`arbor_state.py` `validate_hypothesis`); native `TreeAddNode` accepts any string and leaves the contract to the skill.

The port must keep 1-4 as *code-enforced* properties and 5 as a skill with a cheap machine check. Everything else in Arbor (ReAct runtime, context layers, checkpointing, EventBus, CLI/dashboard) is duplicated by the surogates harness and is deliberately not ported.

---

## 3. Capability mapping

| Arbor runtime component | Surogates primitive that hosts it | Key file pointers |
|---|---|---|
| Coordinator (persistent ReAct agent, never writes code) | The mission coordinator session, `strict_coordinator=True` — the "never writes code" rule becomes structural (strip set verified to include terminal/file-write/web/browser/`create_artifact`) | `surogates/tools/builtin/coordinator.py:83-119`; `surogates/harness/loop.py:3016-3035` |
| Coordinator's read-only OBSERVE (reads failure logs before ideating) | New `research_coordinator` filter branch: strict strip set minus `{read_file, search_files, list_files}` (reads yes; writes/terminal still stripped) | one branch inside `_tool_filter_for_session`, `loop.py:3029` |
| Idea Tree + `tree.meta` (`idea_tree.json`) | `idea_nodes` + `research_runs` tables (Postgres, create_all-safe) + `idea_tree` HARNESS tool; markdown twin rendered to `/workspace/.arbor/idea_tree.md` | `surogates/db/models.py` (new), new `surogates/tools/builtin/arbor.py`; port of `study/Arbor/src/coordinator/idea_tree.py:28-435` |
| `TreeView/Add/Update/Prune/SetMeta/Propagate` | `idea_tree(action=view\|add\|update\|prune\|set_meta\|propagate\|record_from_task\|requeue\|report)` | same |
| `RunExecutor(Parallel)` + dispatch gating (cycle cap, pending-only, leaf-only, depth) | `dispatch_experiments(node_keys)` HARNESS tool → server-side worktree creation + brief build + `create_task_and_spawn(max_attempts=1, agent_def="arbor-executor")` | port of `executor_run.py:41-54, 116-220, 248-365, 807-828`; `surogates/tasks/spawn.py:37-92` |
| Executor agent | `spawn_task` worker session (Task row: retries off, `result_metadata` JSONB, attempt history) with `arbor-executor` AgentDef + preloaded skill | `surogates/tasks/{tools,spawn,dispatcher,completion}.py`; `surogates/db/models.py:939-1046` |
| Executor report → LLM extraction (`_parse_executor_report`) | `worker_complete(summary, metadata={node_key, score, insight, result, branch})` — structured at the source; `generate_structured` extraction only as fallback | `surogates/tasks/tools.py:666-747`; `surogates/harness/structured_output.py:17` |
| Harvest (fold results into tree after every executor) | **Deterministic pre-LLM wake hook** `harness/loop_arbor.py`, modeled on the board mixin (called like `maybe_emit_board_update` at `loop.py:1304`); runs no matter what the coordinator LLM does | `surogates/harness/loop_board.py` (pattern); new `loop_arbor.py` |
| Insight backprop (`propagate_insights`) | Deterministic concat-propagate at harvest (crash-safe, no LLM in hot path) + LLM synthesis (verbatim prompt port of `tree_ops.py:555-571`) inside tool-call paths: `record_from_task`, merge finalize, prune, `propagate` | new `surogates/arbor/propagate.py` |
| `GitMergeBranch` (independent B_test re-run, direction-aware gate) | `merge_experiment(start/status)` HARNESS tool: detached worktree + **detached** eval (`nohup` → `result.json`) so no sandbox exec is held open (per-exec timeouts, pod churn); **no score argument in the schema**; sole writer of `test_trunk_score` | port of `git_ops.py:109-561`; detached-launcher precedent `surogates/coding_agents/pod_runner.py` |
| Cycle loop + `max_cycles` | Mission evaluator `needs_revision` continuations + `/auto-research max_iterations=N …` (store already accepts the param) + hard `cycles_spent` refusal inside `dispatch_experiments` | `surogates/missions/store.py:48-58`; `missions/evaluator.py:318-416`; `harness/loop_mission_evaluator.py:30-60` |
| Convergence detector | Stats computed from `idea_nodes` into the research judge prompt + deterministic force/stop directives in continuations | port of `study/Arbor/src/coordinator/convergence.py:72-344` (~100 LOC core) |
| `RunTraining` (stream/stall/walltime) | `terminal(background=true, notify_on_complete=true)` + `process(poll/wait)` + staged `parse_log.py`; >1h experiments → ops training-runs jobs (v3) | `surogates/tools/builtin/terminal.py`; `tools/utils/process_registry.py` |
| SearchAgent (background novelty scout) | Async `spawn_task` search worker with `web_search`/`web_extract` (coordinator's own `web_search` is stripped — verified) | `surogates/tools/builtin/web_search.py` |
| `AskUser` / HITL modes / pause | `ask_user_question` (verified NOT in the strip set) → inbox `input_required` → web/Telegram/Slack; `/mission pause/resume/cancel --cascade`; mid-mission chat = nudge | `tools/builtin/ask_user_question.py`; `missions/commands.py:397-446` |
| Checkpoint / `messages.jsonl` replay | Native event-log replay every wake + DB tree + S3-durable `/workspace` + task attempt system. **Nothing to build.** | `harness/loop.py:671` (wake/replay) |
| EventBus / dashboard / REPORT.md | `events` table + SSE + mission dashboard (task DAG) + research mode (§4.7); report rendered server-side by `idea_tree(report)` to `/workspace/.arbor/REPORT.md`, surfaced as artifact **by a worker task** (the coordinator's `create_artifact` is stripped and is never restored — see §4.6) | `session/events.py`; `tools/builtin/artifact.py` |
| Skills + `LoadSkill` hard gate | Hub-published skill bundles + `skill_view` `<HARD-GATE>`/LOAD_RECEIPT prose (process-suite precedent) | `skills/process/brainstorming/SKILL.md` (pattern); `harness/slash_skill.py` |

---

## 4. Recommended architecture

### 4.1 Design thesis

The single load-bearing verified fact: under `/mission` (and therefore under `/auto-research`, which sets the same flags), `strict_coordinator` subtracts `COORDINATOR_IMPLEMENTATION_TOOLS` (terminal, all file I/O, web, browser, `create_artifact`, `worker_*`) and the filter has **no re-add mechanism** (`_tool_filter_for_session`, `loop.py:3016-3035`). Therefore Arbor's pure-skill fallback (a state script driven via `terminal`) cannot drive a strict coordinator, and routing tree operations through `delegate_task` child sessions makes an LLM relay out of deterministic 50ms operations. The tree, the dispatch gates, and the merge gate must be **HARNESS builtin tools** — handlers receive `session_factory`, `llm_client`, `sandbox_pool` (`harness/tool_exec.py:590-657`), and new tool names are not in the strip set, so a strict coordinator keeps them automatically. Judgment stays in skills; guarantees live in tools — exactly Arbor's own enforcement split.

### 4.2 Components

```
user ── /arbor-research <goal> ──► intake skill (PRE-mission, normal full-tool session:
        │                          discover repo/eval/splits, measure baseline on B_dev + once
        │                          on B_test, one compact clarification checkpoint,
        │                          compose the /mission command for one-click send)
        ▼
/auto-research max_iterations=60 <Research Contract>  +  Rubric: <machine-anchored>
        │   alias of /mission create with research kind forced:
        │   Mission row + research_runs row + idea_nodes ROOT
        │   session.config: active_mission_id, coordinator, strict_coordinator,
        │                   research_coordinator, preloaded arbor-coordinator
        ▼
Coordinator session (mission session; reads allowed, writes/terminal stripped)
  tools: delegation suite + idea_tree + dispatch_experiments + merge_experiment
        │ dispatch_experiments(node_keys=[...])   ← hard gates: pending/leaf/depth/budget
        ▼                                            server-side worktree + persisted brief
spawn_task workers ("arbor-executor" AgentDef, max_attempts=1, preloaded arbor-executor
  skill), one per node, each in /workspace/.arbor/worktrees/<key> on its own branch
        │ worker_complete(summary, metadata={node_key, score, insight, result, branch})
        ▼
harness/loop_arbor.py harvest (pre-LLM, deterministic, fail-open, on coordinator wake):
  fold terminal tasks → node done|failed/score, concat-propagate, worktree cleanup (branch kept),
  inject [research harvest] digest + constraints block at end-of-history
        ▼
research-kind mission evaluator (table-lookup dispatch at the existing judge hook):
  SKIP while experiments in flight (no verdict, no iteration burn)
  → LLM rubric judge over SQL tree leaderboard + machine-written scores + convergence stats
  → needs_revision continuation (next cycle) | deterministically-verified satisfied | terminal
```

One research run ⇔ one Mission ⇔ one coordinator session ⇔ one idea tree ⇔ one git repo at `/workspace/{repo}`. All task children share the root session's sandbox pod and S3-durable `/workspace` (`sandbox/pool.py:22-48`).

### 4.3 Data and state model

**`research_runs`** (new table — sidecar, so `missions` is never ALTERed; presence of the row IS the kind dispatch):
- `id UUID PK`, `org_id`, `mission_id UUID UNIQUE` (FK to `missions`, like `Task.mission_id`; re-pointed on mission chaining), `session_id`, `agent_id`
- `repo_path`, `trunk_branch`, `branch_prefix`, `status (init|active|finalizing|completed|failed|cancelled)`, timestamps
- `meta JSONB` — closed key set ported verbatim from `idea_tree.py:124-141`: `baseline_score, trunk_score, test_baseline_score, test_trunk_score, eval_cmd, eval_cmd_test, eval_timeout, eval_retries, eval_retry_base_delay, eval_retry_max_delay, metric_direction, dataset_info, protected_paths, required_outputs, max_cycles, max_tree_depth, max_parallel, merge_threshold, hitl_mode, convergence thresholds, merge_eval (in-progress stamp)`. Unknown keys rejected at the store layer. **All meta writes go through `ResearchStore` as per-key `jsonb_set` UPDATEs** — no read-modify-write of the whole blob, eliminating the concurrent-writer race the judges flagged on hybrid's `missions.config`. Machine-score keys (`test_trunk_score`, `test_baseline_score` post-baseline) are writable **only** by `merge_experiment` / the baseline-record path; `idea_tree(set_meta)` rejects them.

**`idea_nodes`** (new table): `id`, `org_id`, `run_id FK research_runs`, `node_key` (dotted decimal `"ROOT"`, `"1.2"`; UNIQUE `(run_id, node_key)`), `parent_key`, `depth`, `hypothesis` (4-line format), `status ∈ pending|running|done|failed|merged|pruned` (CheckConstraint; `failed` = crashed/timed-out task recorded by harvest with a null score — spends budget exactly like `done`, but keeps convergence stats and the evaluator leaderboard able to distinguish infra death from scored-worse, matching native's status set), `insight`, `result`, `score float|null` (absolute B_dev, never delta), `code_ref` (branch), `related_work`, **`task_id UUID|null`** (the experiment ledger: dispatch writes it, harvest joins on it — Task rows already carry status, attempts, `result_metadata`, worker session ids), `dispatched_at/completed_at`, timestamps. Index `(run_id, status)`.

Both tables arrive free via `Base.metadata.create_all` (`db/engine.py:93-117`) — no hand-rolled DDL, no Alembic concern, zero ripple into ops-side `Mission` model consumers.

**Workspace artifacts** (S3-durable, survive pod recycling): `/workspace/.arbor/{worktrees/<key>/, experiments/<key>/{executor_prompt.md, report.md, metrics.json}, merge-eval/<key>/result.json, eval_logs/, idea_tree.md, REPORT.md}`. The DB is the single source of truth; files are audit/display artifacts.

**Cycle accounting**: `cycles_spent = COUNT(idea_nodes WHERE status IN (done, failed, merged, pruned))` — failed/timed-out experiments count (Arbor's "failed runs spend budget"; matches native `_completed_cycles`, `executor_run.py:44-54`). `max_iterations` defaults to `2 × max_cycles` at create.

### 4.4 The three native tools (the deterministic spine)

All three: `ToolLocation.HARNESS` entries in `tools/router.py:44` (unlisted tools default to SANDBOX and fail as "Unknown tool") + registration in `tools/runtime.py` + a routing regression test in `/work/surogates/tests/` (hard platform rule). Visibility: gated in `orchestrator/worker.py::_filter_effective_tools` — present only when `session.config["active_research_run_id"]` is set and `session.task_id is None` (coordinator only; **executors stay tree-blind**, preserving Arbor's "no second shared-state protocol" rule, `mle_kaggle.yaml:41-46`).

**`idea_tree`** (~550 LOC) — multiplexed actions (precedent: `todo`, `process`):
- `view(format=constraints|compact|node|pending)` — line-for-line port of the constraints block and compact leaderboard. The anti-amnesia artifact, re-read every cycle.
- `add(parent_key, hypothesis)` — depth cap; machine-warns on non-4-line hypotheses (port of `arbor_state.py` `cmd_add` check).
- `update` (MUTABLE_FIELDS whitelist), `prune(node_key, reason)` (recursive, `[Pruned: <reason>]` appended; triggers LLM backprop in-tool), `set_meta` (closed keys, machine-score keys rejected), `propagate(node_key)` (LLM synthesis, verbatim prompt from `tree_ops.py:555-571` via injected `llm_client`).
- `record_from_task(task_id)` — the coordinator's **correction channel** (Arbor's review-the-extraction doctrine, `prompts.py:421-423`): reads `Task.result`/`result_metadata` from the DB row, never coordinator prose; runs LLM backprop.
- `requeue(node_key, reason)` — explicit escape hatch when a failure was infrastructural (pod recycle) rather than experimental; resets node to `pending` without refunding the spent cycle (a human-driven refund path arrives with the v3 research API; until then requeue always spends). This is the infra-vs-experiment distinction grafted onto `max_attempts=1`.
- `report` — renders the final report (test scores primary, top-scored nodes, root insight, compact tree — port of `report/generator.py:131-203` + `orchestrator.py:1053`; native renders the top-10 scored nodes and leaves the root insight in `tree.json` — we keep top-10 and additionally surface the root insight as a report section) AND persists it server-side to `/workspace/.arbor/REPORT.md` via `sandbox_pool`.

**`dispatch_experiments(node_keys: list[1..4], extra_context?, action?)`** (~300 LOC) — Arbor's RunExecutor automation, natively:
- Per node, validates: `pending` + leaf + depth cap; **refuses when `cycles_spent >= max_cycles`** or when in-flight count would exceed `max_parallel` (the hard budget gate the judges required — budget enforcement no longer rides the mission iteration cap alone).
- Creates the worktree **server-side** via sandbox exec (`git worktree add -b {branch_prefix}/n<key>-<slug>-<sha8> /workspace/.arbor/worktrees/<key> <trunk>`, collision retry — port of `executor_run.py:116-182`): isolation exists before the worker's first token, not as prompt-law.
- Builds the executor brief (port of `executor_run.py:248-365`: worktree path, git-isolation rules, hypothesis, Evaluation Info with `{cwd}`/`{node_id}` substituted, ancestor insights root→parent, report contract). **Divergence from Arbor, stricter:** `eval_cmd_test` is stored DB-only and is *never rendered into any executor-visible text* — no DO-NOT-USE tag to ignore. Brief persisted to `experiments/<key>/executor_prompt.md` (auditable).
- Creates the Task via the factored helper `tasks/service.py::create_task_and_spawn(...)` (extracted from `_spawn_task_handler` so the LLM tool and this path share one implementation) with `agent_def_name="arbor-executor"`, `max_attempts=1` (a failed experiment is evidence, not a retryable crash — the tool default of 3 silently re-runs trainings), mission_id stamped as today; writes `node.task_id`, `status=running`, `code_ref`.
- `action="baseline"`: creates/checks out the trunk branch and spawns a baseline task when intake didn't measure one; harvest writes `baseline_score`/`test_baseline_score` (the only non-merge writer of a test score). Keeps the coordinator delegate-only even during INIT.

**`merge_experiment`** (~350 LOC) — the bypass-proof ring, split so no sandbox exec is held open across the eval:
- `start(node_key)`: validates node `done` + branch exists; via sandbox exec creates a **detached throwaway worktree** of the branch and launches `eval_cmd_test` **detached** (`nohup … & → result.json`), stamps `meta.merge_eval = {node_key, started_at}`; returns immediately. This keeps long evals out of sandbox execs entirely: K8s execs run at `spec.timeout + 5` (`kubernetes.py:188` — per-call configurable, ≈185s at the terminal default, `TERMINAL_TIMEOUT=180`), a synchronous eval would hold the per-pod exec lock for its full duration, and any exec dies with pod churn (`activeDeadlineSeconds=3600`); the detached file-based handoff survives all three.
- `status(node_key)`: reads `result.json`. Absent → "still running", **unless `now - started_at > eval_timeout + grace`** → reports the eval stale (orphaned by the 1h pod recycle, `activeDeadlineSeconds=3600`, kubernetes.py:39) and offers an idempotent re-`start` (judge-required staleness cutoff). Present → JSON `{"score": …}` parse (no LLM fallback in v1; the eval contract requires a JSON score block, stated at intake; a nonzero-exit or unparsable eval is re-`start`ed per `meta.eval_retries` with backoff before being reported as failure — port of native's eval-retry policy); then finalizes in-tool: direction-aware `is_improvement` vs `test_trunk_score or test_baseline_score` (port of `idea_tree.py:193`; below-`merge_threshold`-but-improving merges with a logged warning — Arbor's soft-threshold semantics); `protected_paths` diff guard + `required_outputs` guard; refuse main/master; `git merge --no-ff` on trunk, abort+restore on conflict; on success the **tool writes** `meta.test_trunk_score` and node `status=merged`, then runs LLM backprop.
- **The schema accepts no score argument.** The LLM physically cannot self-report into a merge — *stricter* than native Arbor, which accepts a `test_score` argument (logged for comparison, `git_ops.py:325-331, 407-411`) and falls back to the LLM-reported score when no `eval_cmd_test` is configured (`git_ops.py:413-420`), and stricter than the skill-suite fork, whose `--test-score` flag skips the re-run entirely when passed (`arbor_state.py:889-913`). No `eval_cmd_test` configured → hard error, no LLM-reported fallback (no legacy fallbacks).
- Concurrency note: all shared-`.git` mutations (worktree add/remove, merge) happen on coordinator-side tool paths, which are serialized by the session's exclusive wake lease plus the pod exec lock; executors touch only their own worktree directories. Tree state itself is mutated only through DB transactions, so cross-replica integrity does not depend on the per-process `SandboxPool` asyncio lock.

### 4.5 Harvest: the deterministic keystone (`harness/loop_arbor.py`)

New mixin mirroring `BoardMixin` (`harness/loop_board.py`; call site beside `maybe_emit_board_update`, `loop.py:1304`). At coordinator wake, **before the LLM call**, when `active_research_run_id` is set, and **fail-open** (try/except wrapper, same discipline as the mission-evaluator hook, `loop_mission_evaluator.py:37-44`):

1. Find `idea_nodes` with `status=running` whose linked Task is terminal.
2. Fold each deterministically: `task.result_metadata.score/insight/result` taken verbatim; metadata absent → one bounded `generate_structured(ExperimentReport)` extraction over `task.result` (head+tail 12k cap, Arbor's rule), failing open to `score=null, result=text[:500]`. Crashed/timed-out task → node `failed`, `insight="Timed out/crashed: …"` — budget consumed, timeout is evidence.
3. **Deterministic concat-propagate** up the ancestor chain (capped, `arbor_state.py:495-518` style) so the constraints block is never stale — the LLM-synthesis backprop is deliberately kept *out of the wake hot path* and runs inside tool calls (`record_from_task`, merge finalize, prune, `propagate`) instead (the judges' "move the LLM backprop out-of-band" graft).
4. Remove the worktree, keep the branch; persist `experiments/<key>/{report.md, metrics.json}`; refresh `idea_tree.md`.
5. Inject a `[research harvest]` digest + constraints block + any convergence intervention at end-of-history (board seq-cursor pattern — appended, never inserted mid-list, so the provider prefix cache stays stable).

This makes harvest idempotent, crash-safe, and coordinator-LLM-independent: a dead executor plus a lazy or compacted coordinator can no longer strand `running` nodes or let the judge grade a stale leaderboard. `record_from_task` remains as the correction tool, not the load-bearing mechanism.

### 4.6 Control flow of one hosted arbor cycle, turn by turn

1. **Intake (pre-mission, full tools).** User: `/arbor-research improve model F1 in ./repo`. Slash expansion inlines the intake skill into a *normal* session: cheap discovery, identify B_dev/B_test, measure the baseline (B_dev, and B_test once), one compact clarification checkpoint (metric/ambition/scope/permissions/budget/HITL mode/smoke), then present the Research Contract panel plus a ready-to-send fenced command — `/auto-research max_iterations=60 <contract>` with the rubric template:
   > *Satisfied only when research_runs.meta.test_trunk_score (written only by merge_experiment) improves on test_baseline_score per metric_direction [by >= ambition], with >= 1 merged node — OR budget exhausted with an explicit no-improvement root insight — AND the final report task is done. Never satisfied on prose claims or dev-split scores. Any selection decision based on B_test output = blocked. needs_revision feedback must name the next structural step (expand X / prune Y / paradigm shift / merge / finalize).*

   Running intake before strict mode flips dissolves Arbor's INIT problem: baselines are measured with real tools, then frozen into the run.
2. **Create.** The `/auto-research` slash match (builtin block beside `/mission`, ~loop.py:924) routes to `handle_research_mission_create`: Mission row (`MissionStore.create(..., max_iterations=N)` — the param exists, `store.py:48-58`; only the call site and the `mission.defined` event hardcode 20 today), `research_runs` row + ROOT node, session config stamped (`active_mission_id`, `active_research_run_id`, `coordinator`, `strict_coordinator`, `research_coordinator`, preloaded `arbor-coordinator`), research kickoff emitted only after the slash-reply cursor advance (the documented cursor-race contract, `commands.py:113-149` — untouched).
3. **Turn 1 (coordinator, IDEATE).** `idea_tree(set_meta …)` with the measured numbers (if intake skipped the baseline: `dispatch_experiments(action="baseline")` instead) → `idea_tree(view constraints)` → `skill_view("arbor-ideate")` (`<HARD-GATE>` + LOAD_RECEIPT) → `add` 2-3 four-line hypotheses (machine-checked) → `dispatch_experiments(node_keys=["1","2"])`: budget/leaf/depth validated, worktrees created server-side, briefs persisted, two Tasks spawned with `max_attempts=1`, nodes `running`. Coordinator ends its turn. **Evaluator: research policy sees in-flight experiments → SKIP — no judge call, no iteration burned.** `_mission_has_pending_work` defers session completion (`loop.py:1933`).
4. **Executors** (parallel worker sessions sharing the root pod; sibling foreground execs serialize on the pod lock, so heavy work runs via `terminal(background=true, notify_on_complete=true)` + `process(wait)`): UNDERSTAND → IMPLEMENT → VALIDATE on 2-3 examples → full B_dev eval → commit on the branch → `worker_complete(summary=<report>, metadata={node_key, score, insight, result, branch})`. `WORKER_COMPLETE` events wake the coordinator.
5. **Wake (harvest + OBSERVE + DECIDE).** Pre-LLM harvest folds the nodes, concat-propagates, removes worktrees (branches kept), injects digest + fresh constraints block. The coordinator — read tools restored under `research_coordinator` — may read failure logs/eval output for real OBSERVE forensics, optionally `record_from_task` corrections, then DECIDE: promising → `merge_experiment(start "1")`; hopeless → `idea_tree(prune)`. Turn ends; evaluator fires on the `task_terminal` trigger (30s rate-limited; one evaluation covers everything since the last one): research prompt = rubric + **SQL tree leaderboard over `idea_nodes`** (top-N direction-aware, merged list, pruned count, machine-written meta scores — replacing the 20-recent-tasks block the missions brief itself flags as the wrong shape) + convergence stats + in-flight tasks + coordinator response → `needs_revision` + research continuation (`[Continuing your research mission] iteration i/max — view constraints → OBSERVE → IDEATE (hard gate) → dispatch → DECIDE; convergence: <stats/intervention>`) → self-enqueue. Synthetic continuations stay breaker-invisible (the crash-loop invariant, `orchestrator/dispatcher.py:468-557`).
6. **Continuation wake.** `merge_experiment(status "1")` → merged (tool wrote `test_trunk_score`; trunk advanced, so subsequent worktrees branch from the new HEAD automatically), or a structured refusal that becomes tree evidence, or stale → re-`start`. Next IDEATE cycle proceeds from the updated constraints block. Plateau: convergence warn/force interventions arrive in digests and judge feedback (Exploit/Combine/Leap); force-level adds a mandatory paradigm-shift directive listing exhausted parents.
7. **Finalize.** Budget/convergence-stop/target → continuation carries the FINALIZE directive: merge best, `idea_tree(report)` (REPORT.md persisted server-side), then spawn one **report task** whose worker calls `create_artifact(kind="markdown")` from REPORT.md and completes with `metadata={report: true, test_trunk_score}`. *(This corrects hybrid's falsified step: terminal verdicts clear only `active_mission_id` — verified at `evaluator.py:359-364` — while `strict_coordinator` persists in session config, so the coordinator never regains `create_artifact`; the artifact must be worker-side.)* The report task's completion triggers the final evaluation; the research policy honors `satisfied` only after **deterministic verification**: machine-written `test_trunk_score` present and improving (or explicit no-improvement root insight with budget spent) AND report task done. Judge `failed`/`blocked` verdicts are demoted to `needs_revision` unless deterministically corroborated (budget exhausted, repo missing, eval contract never established after N continuations) — a single noisy verdict can no longer kill a 40-cycle run. Terminal → `active_mission_id` cleared, session freed for chat; tree, branches, artifacts queryable forever.

**HITL (via channels).** `meta.hitl_mode ∈ {auto, direction, review}`: *direction* → `ask_user_question` at each IDEATE start; *review* → approval ask before dispatch and/or merge finalize (v2 prompt-level; structural inbox `governance_gate` inside `dispatch_experiments` as a v3 option). Questions land as durable inbox `input_required` items delivered over web/Telegram/Slack and survive pod/process churn (the handler polls the event log by `tool_call_id`). Mid-mission chat is a native nudge, not a pause. `/mission pause` (evaluator off, workers continue), `resume`, `cancel --cascade` (Redis interrupts to running executors). Executors use `worker_block` → `unblock_task(additional_context)`.

**Long-run jobs.** In-pod experiments are bounded by the 1h pod deadline: the executor skill mandates checkpoint-to-`/workspace` discipline and scopes v1/v2 to experiments ≲45min; merge evals already outlive any single exec via the detached split. v3 adds the real >1h path: the executor submits training to surogate-ops training-runs (dstack/Modal) and a poll/collect task pattern reports the score via `worker_complete` — the exact verifier-task pattern the missions system was designed around — plus a per-run sandbox `activeDeadlineSeconds` override knob. Caveat (verified): training-runs is ops-side only today — this leg includes a new agent-callable submission API plus executor credential plumbing in surogate-ops, not just harness wiring.

**Resume across wakes.** Every layer is already durable: tree + run + mission rows in Postgres; the conversation is the append-only event log (replay IS resume — no checkpoint.json/messages.jsonl port); tasks survive in the task layer; branches survive in git; worktree dirs are reconstructible (`git worktree prune` + re-add). After any crash: harvest folds whatever finished at the next wake, `tasks_tick()` finalizes tasks whose worker session died (harvest folds them as `failed`), the evaluator's continuation restarts the cycle. Arbor's running→pending requeue is deliberately *not* automatic for crashed tasks (failed runs spend budget); `idea_tree(requeue)` is the explicit infra-failure escape hatch. Runs exceeding one mission chain over the durable tree: a fresh `/auto-research resume=<run>` re-points `research_runs.mission_id`, and the coordinator continues from `view constraints` ("do NOT restart from INIT").

### 4.7 Mission dashboard: research mode

The dashboard is the SDK's `MissionDashboard` (`sdk/agent-chat-react/src/components/missions/mission-dashboard.tsx`), hosted by `web/src/features/missions/mission-page.tsx`; it polls the adapter every 5s (`getMission`/`getMissionTasks`/`getMissionWorkers`, stops at terminal status) and renders a hero card (status pill, iteration `i/max` progress, last verdict, pause/resume/cancel) plus Tasks / Activity / Workers / Metadata tabs. Research mode is an **additive layer on this component, activated by data presence** — no fork, no second dashboard.

**Detection.** `GET /v1/missions/{id}` gains an optional `research_run_id` field (server-side lookup on `research_runs.mission_id`); `AgentChatMissionSummary` gains the matching optional field. Absent → the dashboard renders exactly as today (standard missions untouched).

**Research hero strip** (inside the hero card under the iteration row, research mode only): the score journey `baseline → trunk (dev) → test` with `metric_direction` arrows; the cycle budget `cycles_spent / max_cycles` shown beside (not instead of) evaluator iterations — the two budgets are distinct and both matter; merged/pruned/failed node counts; a convergence chip (`none|warn|force`); an "eval running" pulse while `meta.merge_eval` is stamped.

**Idea Tree tab** (new `mission-research-tab.tsx`, labeled "Idea Tree" with a node `CountBadge`, gated on adapter support exactly like the Activity tab's `feed.supported`): an indented collapsible list ordered by node key — per row a status chip (`pending/running/done/failed/merged/pruned` tones matching the existing status palette), `node_key`, dev `score`, the first hypothesis line (full 4-line block + insight in the hover tooltip, the dashboard's existing clamp+tooltip pattern), a `code_ref` branch chip, and a task cross-link reusing `onOpenTranscript` with the experiment's worker session (join `idea_nodes.task_id` → the tasks payload already carried by the dashboard). The ROOT row pins the root insight. Terminal runs keep the tab — the tree is queryable forever.

**Activity tab — free from v1.** `useMissionEvents` already streams the coordinator session's event feed, so the `research.*` events (defined/dispatched/harvested/merged/pruned/converged/report) emitted from v1 onward appear there with zero SDK work. Until v3, the Activity tab *is* the research observability surface; the tree tab and hero strip are the v3 additions.

**Data layer.** The adapter gains optional `getResearchRun({runId})` and `getResearchTree({runId})` (backed by v3's `GET /v1/research/runs/{id}` and `/tree`); `MissionDashboard.refresh()` adds both calls to its existing `Promise.all` when `research_run_id` is present, sharing the 5s cadence and terminal-stop. Optional-method gating follows the `feed.supported` precedent (tab hidden when the adapter lacks the surface), not the fail-loud `requireMissionApi` one — older adapters keep working.

**Ship vehicle.** SDK components + types land in `@invergent/agent-chat-react` (version bump + npm publish; the web app consumes from the registry per the npm transition), the adapter implementation in the surogates web app, the API route in `surogates/api/routes/research.py`.

---

## 5. What gets built vs reused

### Reused unchanged (~85-90% of the system)
Missions store/evaluator wiring/dashboard/pause/cancel-cascade; the injectable judge seam and free-function `should_evaluate`/`apply_verdict` (`loop_mission_evaluator.py:30-60`, `evaluator.py:90-137, 318-416`); session-completion deferral; task layer (spawn/dispatcher/completion/worker self-tools/attempt history); `delegate_task`; sandbox pool + K8s sandbox (git in image); terminal/process/background; board; inbox/channels/`ask_user_question`; skills plumbing (5-layer merge, staging, slash expansion, Hub publishing); event log/replay; `generate_structured`; crash-loop breaker semantics.

### New files/modules
| Item | Contents |
|---|---|
| `surogates/arbor/{__init__,models,store,propagate,convergence,prompts,evaluator_policy}.py` | Pydantic models (`ExperimentReport` etc.); `ResearchStore` (run/node CRUD, constraints block, cycle accounting, `is_improvement`, per-key `jsonb_set` meta writes); backprop (concat + LLM synthesis); convergence core; executor-brief/kickoff/continuation/report builders; research evaluator policy |
| `surogates/tools/builtin/arbor.py` | `idea_tree`, `dispatch_experiments`, `merge_experiment` registration + handlers (incl. sandbox-exec git helpers) |
| `surogates/harness/loop_arbor.py` | `ArborHarvestMixin` (pre-LLM fold, fail-open, digest injection — BoardMixin pattern) |
| `surogates/tasks/service.py` | `create_task_and_spawn(...)` factored from `_spawn_task_handler` (minimal: insert + eager spawn), used by the LLM tool and `dispatch_experiments` |
| Tests (flat in `/work/surogates/tests/`) | `test_arbor_routing.py` (TOOL_LOCATIONS regression — hard rule), store/dispatch-gates/merge-gate (improvement, direction, protected paths, refusal, staleness)/harvest/evaluator-policy/mission-parse |
| Skill bundles `/work/surogates/skills/research/` | see layout below |
| Ops feature pack `surogate_ops/features/research_tree/agents/arbor-executor/AGENT.md` (+ enable flag) | deep-research precedent |

### Modified files
- `surogates/db/models.py` — `+ResearchRun`, `+IdeaNode` (**new tables only**; `missions` untouched)
- `surogates/tools/router.py` — **3 `TOOL_LOCATIONS` HARNESS entries** (`idea_tree`, `dispatch_experiments`, `merge_experiment`)
- `surogates/tools/runtime.py` — register `tools/builtin/arbor.py`
- `surogates/harness/loop.py` — 3 touches: harvest-hook call beside the board hook (~loop.py:1304), the `research_coordinator` read-only branch inside the existing `strict_coordinator` filter block (~loop.py:3029), and the `/auto-research` match in the builtin slash block beside `/mission` (~loop.py:924)
- `surogates/harness/loop_mission_evaluator.py` — kind dispatch by `research_runs` table lookup after the active-mission fetch (standard missions take the existing path, untouched)
- `surogates/missions/commands.py` — `parse_auto_research_command` (optional leading `max_iterations=N` / `resume=<run>` tokens, then the same `<description> Rubric: <rubric>` shape as `/mission`; control verbs `status/pause/resume/cancel` delegate to the existing `parse_mission_command`) + `handle_research_mission_create` wrapper around the existing create path (research_runs row + ROOT node, research config stamping, `arbor-coordinator` preload instead of `subagent-task-orchestrator`, research kickoff variant, passes `max_iterations` to `store.create` and the `mission.defined` payload). `/mission` parsing is untouched.
- `surogates/harness/slash_skill.py` — add `auto-research` to `_BUILTIN_SLASH_COMMANDS` (the reserved frozenset, `:32-40`) so a skill cannot shadow the command
- `surogates/orchestrator/worker.py::_filter_effective_tools` — visibility gate for the 3 tools (coordinator-only; force-strip from task workers)
- `surogates/tools/loader.py` + `surogates/tasks/spawn.py` — `AgentDef` gains optional `preloaded_skills` frontmatter, honored in `_build_task_worker_config` *(fixes hybrid's unbacked preload claim with a real, retry-safe mechanism: verified today the builder only auto-adds `subagent-task-worker`, spawn.py:84-87)*
- `surogates/session/events.py` — `research.*` event types (defined/dispatched/harvested/merged/pruned/converged/report)
- (v3) `surogates/api/routes/research.py` (run + tree endpoints) + `research_run_id` on the missions API summary + SDK research mode per §4.7 (`sdk/agent-chat-react/src/components/missions/mission-research-tab.tsx`, hero strip, adapter types/methods, npm publish) + `web/` adapter wiring

### Skill bundle layout (`/work/surogates/skills/research/`, Hub-published; 11 Arbor skills collapsed to 5)
```
research/
  DESCRIPTION.md
  arbor-research/SKILL.md        # entry, /arbor-research slash expansion (name not in the
                                 #   reserved set, which gains auto-research: clear/code/compress/
                                 #   deep-research/goal/loop/mission/auto-research)
                                 #   intake contract + baseline + composed /auto-research block; smoke mode
  arbor-coordinator/SKILL.md     # cycle protocol rewritten for idea_tree/dispatch_experiments/
                                 #   merge_experiment; depth semantics; B_dev/B_test law; failure
                                 #   triage table (mle_kaggle.yaml); resume rule  [preloaded by kind]
  arbor-ideate/SKILL.md          # idea_drafting + first_principles_probe port; probe block,
                                 #   4 moves, kill-filter, 4-line format  [HARD-GATE at IDEATE]
  arbor-executor/SKILL.md        # 7-step workflow, background/process policy, checkpoint
                                 #   discipline, never git merge; scripts/{run_eval.sh, parse_log.py}
                                 #   [preloaded on workers via AgentDef.preloaded_skills]
  arbor-merge-discipline/SKILL.md# DECIDE doctrine, combine-ideas recipe, final-report-uses-TEST rule
```
Prompt provenance: Arbor's own hardened suite (`study/Arbor/skills/`) backed by `src/coordinator/prompts.py:313-572`, `src/executor/prompts.py:239-401`, `src/skills/*.md` — ports, not authorship. Deterministic logic lives in tools, so skill iteration is prose-only (bundle republish, never a harness deploy).

**Deliberately not ported** (no legacy fallbacks): Arbor's Agent/ReAct runtime, its 4-layer context manager and IDEATE context surgery (harness `ContextCompressor` + DB tree make context loss non-fatal by construction), checkpoint/messages.jsonl, EventBus/CLI/WebUI, plugin YAML machinery (meta + skill text cover eval contract/protected paths/profiles), `arbor_state.py` itself (its guarantees move into the HARNESS tools).

---

## 6. Phased delivery plan

**v1 — One real arbor cycle end to end (~3-3.5 engineer-weeks).**
Tables + `ResearchStore`; all three tools **including the merge gate with the detached start/status split and staleness cutoff from day one** (the defining mechanism ships in v1 — not advisory); `create_task_and_spawn` factoring + AgentDef `preloaded_skills`; the `/auto-research` command (research-kind mission create, `max_iterations=N`/`resume=` parsing, reserved-set entry; `/mission` untouched); `research_coordinator` filter branch; harvest hook (metadata-first fold + concat-propagate); research evaluator policy (skip-while-in-flight, deterministic satisfied-verification, demoted failed/blocked, leaderboard prompt); `research.*` event emission (the existing dashboard Activity tab surfaces them for free, §4.7); first-cut `arbor-research`/`arbor-coordinator`/`arbor-executor` skills; routing + gate tests; **CI smoke-mode run** (mocked scores, no training — Arbor's own validation trick) as a protocol regression test. Exit test: a toy repo with a fast eval runs intake → propose → dispatch → harvest → **verified merge** → report-artifact → deterministically-verified satisfied, fully autonomously, and survives a worker-process restart and a pod recycle mid-run.

**v2 — Method fidelity & steering (~1.5-2 weeks).**
LLM-synthesis backprop in all tool paths; convergence stats + Exploit/Combine/Leap interventions in judge feedback and continuations; `arbor-ideate` hard-gate skill + the 4-line machine check; HITL direction/review modes; `record_from_task`/`requeue` polish; parallel dispatch (2-4) hardening; async search-scout task annotating `related_work` before merges; board FAIL/RESULT ticker across executors (verified free: `ensure_group_and_inherit` stamps the parent's `context_group_id` onto every spawned task worker — `tasks/spawn.py:141`, `board/groups.py` — and `orchestrator/worker.py:121-204` then force-adds `share_note`/`read_board`); `arbor-merge-discipline` skill; final-report polish; INIT fallback baseline action; real-run prompt tuning on a small Kaggle-style repo (budget 3-5 days inside this phase).

**v3 — Scale, UX, long runs (~2-2.5 weeks).**
`GET /v1/research/runs/{id}(/tree)` API + dashboard research mode (§4.7: `research_run_id` on the mission summary, hero score strip, Idea Tree tab, SDK adapter methods + npm publish, web adapter wiring); >1h experiments via surogate-ops training-runs (new agent-facing submission API + executor auth in surogate-ops) + poll/collect tasks; per-run sandbox deadline override; mission-chaining `resume=<run>` flow; optional FAST dispatch mode (one `delegate_task(goals=[...])` batch for sub-30-min experiment rounds — explicitly bounded: default DURABLE, batch loss on crash documented, never for long experiments); structural `governance_gate` review mode; evaluate a read-time protected-path guard via `governance/policy.py` from v1-v2 telemetry; ops feature-pack packaging + flag; geesefs git perf pass.

Total ≈ **6.5-8 engineer-weeks** to full Arbor-core parity, with a genuinely complete (merge-gated) research agent at the end of v1.

---

## 7. Key risks & mitigations

1. **Git on geesefs/S3 FUSE is slow and lightly proven for worktree churn.** Measure in the v1 smoke run on a realistic repo; keep repos modest/shallow; ready fallback: pod-local `/tmp` mirror with every experiment branch pushed back to the `/workspace` repo (branch-pushed ≙ Arbor's branch-survives-worktree invariant); serial dispatch default until proven.
2. **Pod lifetime (1h) and per-exec timeouts vs long experiments.** Merge evals: detached start/status + staleness re-run (solved in v1). Training: background+process + mandatory checkpoint discipline + ≲45min scoping until the v3 jobs path; `max_attempts=1` plus `idea_tree(requeue)` keeps pod deaths from silently re-running or unfairly burning the search.
3. **LLM judge noise killing or gaming the mission.** Satisfied requires deterministic verification of machine-written scores; failed/blocked demoted without deterministic corroboration; skip-while-in-flight removes most spurious firings; transport failures already don't burn iterations; `merge_experiment`'s no-score-argument schema means a drifting coordinator cannot fake progress.
4. **Coordinator protocol drift (skipping the ideate gate, forgetting harvest).** The dangerous actions are structural (dispatch validates budget/depth/status; harvest is a wake hook; merge re-runs the eval; tools hidden from workers); prompt gates guard only idea *quality* — Arbor's own enforcement split. CI smoke run catches protocol regressions.
5. **Executor self-reported B_dev scores wrong/fabricated.** Same trust level as native Arbor; fabrication cannot reach trunk (independent B_test re-run) or satisfy the rubric (machine-anchored). Optional later: verifier re-run of B_dev for merge candidates.
6. **Read-only coordinator carve-out reopens a sliver of the strict-mode incident class.** Scope is reads only (`read_file/search_files/list_files`); terminal, writes, web, browser stay stripped — the incident was the model *doing the work*, which still requires tools it does not have. The branch lives inside the existing strict block and applies only when `research_coordinator` is set.
7. **Shared-pod serialization caps real parallelism.** Accepted v1 trade-off (correctness via worktrees; long evals overlap as background processes; `max_parallel ≤ 4`). True per-executor pods (non-inherited workspaces + remote clone) are explicitly out of scope.
8. **Skill-bundle pinning friction.** Verbatim pinned snapshots mean every skill edit needs a bundle republish; deterministic logic is in tools precisely so skill edits are prose-only; bake `seed-builtin-skills`/republish + smoke run into the dev loop.
9. **Harvest hook in the wake hot path.** Strictly fail-open (BoardMixin/evaluator-hook precedent), metadata-first (no LLM in the common case), LLM extraction bounded to one call per folded node, LLM backprop excluded from the hook entirely.

---

## 8. Explicitly rejected alternatives

**Pure skill-suite port (zero harness code).** Rejected because `strict_coordinator` strips `terminal`, so every deterministic tree/merge/harvest operation must round-trip through `delegate_task` LLM child sessions — an RPC shim that regresses exactly the operations Arbor made deterministic back into prose discipline, at a permanent token/latency tax. Its merge "guarantee" was verified overstated (the forked script's `--test-score` flag, when passed, skips the B_test re-run entirely, and the merge delegate holds `terminal`; native itself falls back to an LLM-reported score only when `eval_cmd_test` is absent), the 20-iteration cap forces manual mission chaining every few hours, FAST-mode batches evaporate on coordinator crashes, and `max_attempts` defaults silently re-run failed trainings. Its best ideas — intake-emits-the-mission-block, the rubric template language, worker-side report artifact, mission chaining, dispatch-mode economics — are grafted here.

**Full native subsystem (`/research`, parallel evaluator, new package mirroring `missions/`).** Rejected on footprint and risk, not fidelity: four `loop.py` touch points, a third evaluator loop beside `/goal` and `/mission` with a mutual-exclusion matrix to maintain, a hot-path spawn refactor, new API/web surfaces, ~7-8 weeks with nothing usable for ~3 — on the most battle-hardened multi-tenant path in the platform — while its v1 merge gate ran the B_test eval synchronously inside a sandbox exec (holding the per-pod exec lock for the eval's duration, dying with pod churn) and its harvest hook put chained LLM calls into every wake. Its genuinely superior mechanics — deterministic harvest-at-wake, dispatch budget gates, server-side worktrees, eval_cmd_test never rendered, skip-verdicts-while-in-flight, read-only OBSERVE, new-tables-only schema, `max_iterations=2×max_cycles` — are all grafted here at a fraction of the diff.

**ALTERing `missions` with `kind`/`config` columns (hybrid's original schema).** Rejected: surogates has no Alembic (hand-rolled idempotent DDL on the table every production mission uses), the single JSONB `config` column becomes a read-modify-write race surface shared by three writers, and `Mission` pydantic changes ripple into ops-side `SurogatesClient` consumers and the dashboard. The `research_runs` sidecar (presence = kind) delivers the same dispatch with create_all-safe tables and per-key `jsonb_set` writes.

**Workspace-file tree (research memory-bank JSONL pattern) instead of DB tables.** Rejected: the strict coordinator cannot read or write workspace files, so file state would still need HARNESS tools in front of it — at which point Postgres gives transactional integrity across replicas, SQL leaderboards for the evaluator prompt, and the `idea_nodes.task_id` ledger join for free. The markdown twin keeps the human-readable artifact.

**Deterministic-only evaluator replacing the LLM rubric judge.** Rejected: it trades the rubric's per-run expressiveness (ambition thresholds, custom satisfaction criteria) and the judge's steering-quality feedback for a hardcoded policy. The hybrid keeps the LLM judge for `needs_revision` feedback but wraps it in deterministic gates (skip-while-in-flight, machine-verified satisfied, corroboration-gated terminal failure) — concrete-evidence discipline without losing expressiveness.

**Per-experiment dedicated pods for true parallel isolation.** Rejected for now: requires non-inherited workspaces and remote repo clones, breaking the shared-trunk model that makes merged gains propagate automatically; worktree isolation inside one pod is Arbor's own model and is correct, with throughput (not correctness) as the bounded resource.