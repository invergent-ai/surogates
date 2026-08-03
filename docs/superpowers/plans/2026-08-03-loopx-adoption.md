# What LoopX is, and what surogates should take from it

*Study target: `/work/surogate-ops/study/loopx` (MIT, ~305K LoC Python, single-maintainer, high velocity). Comparison target: `/work/surogates` (AGPL-3.0).*

---

---


## 0. Status

**Complete.** Every proposal in this report is implemented. Updated 2026-08-03.

| | Proposal | State | PRs |
| --- | --- | --- | --- |
| P8 | Wake receipt + recovery ceiling | merged | #175, #176 |
| P1 | Durable plan | merged | #185 |
| P6 | `session doctor` | merged | #187 |
| P7 | Ambient failure backoff | merged | #179 |
| P2 | Human gates — Ships 1, 2 | merged / open | #177, #189 |
| P3 | Per-objective spend | merged / open | #178, #190 |
| P5 | Approval as an expiring grant | open | surogates#188 + surogate-ops#336 |
| P4 | Stagnation detection | open | #191 |
| P9 | Evidence corroboration | open | #192 |

Also merged from the same audit, outside the proposal list: #180 (task
respawn loop), #181 (`system` field never reaching the LLM), #182 (inert
arbor meta keys), #183 (`policy_profile` accepted but unenforced).

**Merge order:** surogates#188 before surogate-ops#336 (the ops half needs the
`approval_grants` table). #190 and #191 both add a column to the same
`ALTER TABLE missions` statement and will conflict; whichever lands second
appends its column to the list.

### Deliberately not built

Each is flagged in its own PR rather than quietly dropped.

- **P2 Ship 3 — true suspend.** ~10 touchpoints, and the trap that
  `find_orphaned_sessions` re-enqueues exactly the event shape a suspended
  session leaves, making suspend an infinite re-wake loop.
- **P7 — the no-change tick gate.** The design objection stands: a
  quiet-thread trigger fires *because* nothing changed, so a no-change
  fingerprint suppresses the canonical case. Only the failure-backoff bug was
  real, and that shipped.
- **P9 — declared validator commands.** Executing declared argv from the
  evaluator needs sandbox access, timeouts and an argument-trust model. Arbor's
  `eval_cmd_test` is the shape to copy when it happens.
- **P9 — per-criterion structured rubric.** Its stated value was killing
  re-litigation churn, which #191 addresses by giving the judge its previous
  verdict and streak. A criteria table would be speculative until churn is
  shown to persist — and the proposed "latch settled criteria" design is
  anti-LoopX with no invalidation driver.
- **`policy_profile` enforcement** (surfaced by #183). Needs a decision on what
  each profile permits; `GovernanceGate.with_profile` already exists and name
  resolution is the only missing piece.

## 0b. Corrections this report needed

Implementation contradicted the report in three places. Recorded so the next
proposal does not inherit them.

1. **The `todo.updated` event was not "an optimisation".** The report said to
   fold existing `tool.result` events and that a dedicated event type was
   optional. Retrieval cost makes it the mechanism: `idx_events_session_type`
   is `(session_id, type)` with no `id`, so finding the newest todo result
   among `tool.result` rows means a JSONB filter plus a sort with heap access
   per row — and those payloads carry file contents, web pages and terminal
   output. A dedicated type turns the read into a handful of small rows.

2. **"Remove `todo` from both sets" was half wrong.** Dropping it from
   `CONCURRENCY_SAFE_TOOLS` is right. Dropping it from `SAGA_EXCLUDED_TOOLS`
   is not: saga compensation restores a sandbox checkpoint, and checkpoints
   are stashed only for file-mutating tools, so a journaled todo step would
   carry no `checkpoint_hash` and could only fail its rollback.

3. **Event-sourcing a tool introduces a failure mode the report did not
   name.** Once the prior list comes from a query, a read failure plus a
   `merge=true` write persists a truncated list — the original bug, made
   permanent. Any proposal that replaces in-memory state with a query needs
   the same guard: never write state you could not read.

Two process notes that generalise: the report's "gap confirmed" claims were
each verified against source before implementing, and one (`P1`'s "the agent
concludes it has no plan") was already known to be overstated. Second, unit
tests against a fake store never exercise the SQL — P1 needed integration
tests against real PostgreSQL to cover a `str` session id against a UUID
column.

---

## 1. What LoopX actually is

LoopX is a **deterministic, no-LLM control plane** that answers exactly one question per tick — *"is there a useful, permitted, evidenced state transition available right now, and if so which one?"* — and structurally refuses to let the agent answer it.

Its consistent method: convert every judgement that would normally live in prose or in the model's head into a typed, machine-evaluable fact with a named failure code.

| Prose judgement | LoopX's typed replacement |
|---|---|
| "blocked on X" | `resume_when=todo_done:<id>`, re-evaluated every turn (`control_plane/todos/contract.py:359-383`) |
| "the human approved" | a checkpointed authority whose `active` is recomputed on every read from six independent `inactive_reasons` (`boundary_authority.py:92-149`) |
| "I made progress" | a `delivery_outcome` enum reconciled against an on-disk delta; unbacked claims recorded as `repair_noop` (`state_refresh.py:193-269`) |
| "I'm done" | a separate validator process whose exit code the executor cannot forge (`control_plane/turn_driver/executor.py:385-517`) |

Architecture in ten lines:

1. Durable state is a **Markdown workbench** (`ACTIVE_GOAL_STATE.md`) whose todos carry machine-readable metadata in HTML comments, plus a per-goal JSON registry and an append-only `runs/index.jsonl`.
2. Every mutation is a **validated operator** that takes an `flock`, re-reads inside the lock, applies a typed transition with cross-field invariants, and rewrites atomically (`file_lock.py:13-26`, `todos.py:1477-1791`).
3. A **second, independent lint** (`contract.py:280-568`) re-parses the same file and emits ~18 named contract-error codes with file+line+remediation — because the file is hand-editable.
4. `quota should-run` is a **typed decision cascade**, not a rate limiter: health → operator gates → evidence waits → focus waits → *then* budget (`quota.py:471-522`).
5. `should_run` is the OR of five named allowances resolved by an ordered clobber chain (`control_plane/quota/decision_summary.py:166-328`), so a loop blocked for delivery can still be obligated to repair.
6. Budget is a **duty cycle** (`compute ∈ [0,1] → allowed_slots`) debited from an append-only `quota_slot_spent`/`quota_slot_voided` ledger, and only against an unspent, accountable delivery record (`quota.py:415-468`, `control_plane/quota/slot_accounting.py:131-169`).
7. Stopping is **evidence-gated in both directions**: an outcome floor catches runaway surface-only spend, and `terminal_no_followup` requires structurally complete closure (`control_plane/goals/goal_frontier.py:231-248`).
8. Scheduler cadence derives **only** from the final `interaction_contract`; contradiction collapses to a 3-minute repair tick, never a crash or a silent default (`control_plane/scheduler/arbitration.py:100-181`).
9. The agent runtime (Codex CLI / Claude Code / OpenCode) keeps its own loop; LoopX supplies a per-tick protocol file, an MCP `should_run` gate, and an auditor that vetoes self-declared completion.
10. Everything fails closed: missing validator, unresolvable host identity, unparseable contract, receipt-write failure — all halt rather than degrade.

**Maturity, honestly.** The `control_plane/` subtree is genuinely clean: pure functions over precomputed facts, ordered rule tables returning enums with asserted terminal fallbacks, fail-closed defaults, good tests. The top-level is not: `benchmark_ledger.py` 147K, `status.py` 121K, `quota.py` 109K, `todos.py` 82K; `_prepare_quota_should_run_item` is a 308-line function reassigning the same three flags at six points.

**Ceremony to name plainly:** 501+ distinct `*_v0` schema-version literals with essentially one consumer doing a string compare; hardcoded `boundary: {reads_raw_transcripts: False, ...}` self-attestation dicts that nothing validates at runtime; six separate Markdown todo parsers with divergent heading heuristics; ~20% of the 647 `examples/*-smoke.py` scripts never import `loopx` at all — they assert cross-references between markdown files. `todo_detail_cold_path_v0` has a protocol doc, a canonical-contract claim, and a *passing smoke test* — and no implementation. **The event-sourced kernel (`event_sourced_state.py`) is shadow-mode**: one non-example call site, and `build_event_store_migration_bridge` hardcodes `promotion_allowed: False`. Read it as a reference design, not as infrastructure.

License: **MIT**.

---

## 2. Where surogates already stands

surogates has the substrate LoopX had to invent from files, and in several places the better version.

| Concern | surogates has | LoopX equivalent | Who wins |
|---|---|---|---|
| Append-only log + deterministic replay | `events` table, BIGSERIAL ordering, `harness/loop_context_replay.py:189` | `runs/index.jsonl` + shadow `events.jsonl` | **surogates** |
| Single-writer exclusion | `session_leases`, atomic `ON CONFLICT … WHERE expires_at < now()` (`session/store.py:1561`), 60s TTL + 20s renewal | per-goal `flock` + optional `task_lease_v0` files | **surogates** |
| Progress cursor | `session_cursors.harness_cursor`, lease-token-guarded monotonic upsert (`store.py:1666`) | content-addressed revision hash of the whole state file | **surogates** |
| Crash recovery | orphan sweepers every 60s (`orchestrator/dispatcher.py:851-994`) | lazy TTL expiry, no reaper | tie |
| Durable objective | `missions` table + rubric judge firing on *evidence* triggers (`missions/evaluator.py:90`) | goal registry + `refresh-state` | **surogates** |
| Task DAG | `tasks`/`task_links`, atomic `UPDATE … RETURNING` claim, fan-in (`tasks/dispatcher.py:379`) | todo successor links in Markdown | **surogates** |
| Peer coordination | `board_notes` with LLM admission gate + TTL CLAIM leases (`board/store.py:59`) | Markdown claim field, no TTL, no sweeper | **surogates** |
| Non-forgeable evidence | Arbor `merge_experiment` re-runs the held-out eval itself (`tools/builtin/arbor.py:593`) | independent validator process, exit code only | tie |
| **Durable plan** | module-global dict (`tools/builtin/todo.py:147`) | content-addressed todos with a schema | **LoopX** |
| **Spend ceiling per objective** | none — `IterationBudget(max_total=90)` rebuilt per wake (`orchestrator/worker.py:1524`) | duty-cycle ledger | **LoopX** |
| **Human approval as a grant** | `overridable` never set `True` (`governance/policy.py:72`) — path is dead | scoped, expiring, recompute-on-read | **LoopX** |
| **Stagnation detection** | none outside Arbor | progress-granularity lattice + streak | **LoopX** |

The pattern is clear: **surogates is strong on durability of *what happened* and weak on durability of *what the agent intends, is permitted to do, and has already spent*.** That is exactly LoopX's axis.

---

## 3. Take these — CONFIRMED

Only one proposal survived adversarial verification unmodified.

### P8 — Wake receipt with a bounded recovery-attempt ceiling · effort **M** · value **medium**

**The gap (two independent holes, both verified against source).**

*Hole A — the orphan sweeper has no ceiling.* `/work/surogates/surogates/orchestrator/dispatcher.py:851-930` `_sweep_orphans_once` emits `HARNESS_RECOVERED`, releases the stale lease, releases the turn gate, and re-enqueues — unconditionally. The only skip is `channel == "browser_setup"` (:873). `find_orphaned_sessions` (`session/store.py:1744-1865`) matches any active session with an expired lease and stale `updated_at`, and `harness.recovered` is **not** in `session_end_event_types` (:1822-1827), so a re-swept poison session stays permanently eligible. A session that reproducibly OOM-kills its worker replaying a multi-day history (`harness/loop.py:1067-1068` fetches the full unbounded event history every wake) takes a worker down every 60s, forever.

The existing crash-loop breaker does **not** cover this: `_crash_fingerprint` = `category:sha256(detail)[:16]` (dispatcher.py:90-96), threshold 3 / 6h TTL, tripped only inside `except Exception` in `_process` (:683-733). A SIGKILL never reaches that `except`. The Task layer's `max_attempts` is also inert here — `attempt_count` only increments on spawn, and a session that never terminates never finalises the task.

*Hole B — unbounded refund on partial tool-call streams.* `harness/loop.py:2034-2047`: `if tool_calls_raw and usage_data.get("partial_tool_call")` → `discard()`, `self._budget.refund()`, `invalid_json_retries = 0`, `continue`. **No counter**, unlike every sibling recovery path (`incomplete_scratchpad_retries <= 2` at :1871, `thinking_prefill_retries <= 2` at :1892, `empty_response_retries` at :2159). `IterationBudget.refund` (`harness/budget.py:35-39`) unconditionally decrements, and the loop guard is `while self._budget.remaining > 0`. There is no wall-clock guard — `_turn_started_at` is used only for mtime filtering in `loop_artifact_completion.py:473-513`. **Non-termination is real, not theoretical.** It also resets a sibling counter, so a provider alternating truncated-args with malformed-JSON can ping-pong the two indefinitely.

**The LoopX mechanism.** `find_heartbeat_receipt` (`control_plane/quota/heartbeat_receipt.py:11-27`) reverse-scans for a prior `quota_should_run` matching `(goal_id, agent_id, run_id == turn_instance_id)`; a retry finds its own receipt and repairs rather than double-committing. `fail_heartbeat_receipt` (:47-74) forces `ok=False, should_run=False, state="blocked_health"` when the receipt cannot be written — failure to record the turn is a health blocker, never permission to proceed unrecorded. Both are live (`cli_commands/quota.py:639, 848, 899, 917`). The turn key is content-addressed from the decision (`control_plane/turn_driver/transaction.py:129-164`), journalled after each of seven phases, and re-entry on a `committed` journal replays with all-False effects (`executor.py:1088-1100`).

**Concrete change in surogates.**

*Part 2 first (genuinely S, ship immediately):* add `partial_tool_call_retries` to the counter block at `harness/loop.py:1437-1440`, cap at 3 at :2034, then fall through to `_request_final_summary`. **Also stop resetting `invalid_json_retries = 0`** (:2046) once the cap exists, or the ping-pong survives. One regression test.

*Part 1 (S/M, `orchestrator/dispatcher.py` only):* **the verifier's key correction — drop the Redis counter keyed on `(session_id, harness_cursor)`.** That key is fragile: `advance_harness_cursor` fires mid-turn from `tool_exec.py:538/1200/1269/1328/1596`, `loop_outcome_commands.py`, `loop_code_commands.py`, so a crash *after* a mid-turn advance mints a fresh key and silently resets the ceiling. Instead **count `harness.recovered` events since the session's last cursor-advancing event, straight from the `events` table** — no Redis, survives a flush, visible in the same log an operator is already reading, and removes a `get_harness_cursor` round-trip from the sweep loop.

Ordering matters: the ceiling check must run **before** the `HARNESS_RECOVERED` emit at dispatcher.py:876, or the tripping attempt still bumps `updated_at`. On trip: still release the turn gate (the dead owner leaked it), skip `enqueue_session`, emit `SESSION_FAIL` with `reason='recovery_loop'` mirroring the crash-loop payload shape (dispatcher.py:713-726), call `update_session_status(id, "failed")` — which removes the row from `find_orphaned_sessions` on both the status filter and the `session_end_event_types` filter — and emit `inbox.action_required`. No new plumbing needed there: `_INBOX_EVENTS` (`session/store.py:87-93`) auto-creates the row. Clear on user signal via the existing `_has_user_signal_since` (:561).

**Risk.** Threshold 3 is conservative — any forward progress advances the cursor and resets the count. The terminal fail is user-visible, so the inbox item must carry the fingerprint and last-event detail. No DB migration, no schema change, no prompt change.

**Be honest about provenance:** LoopX's receipt is an exactly-once/idempotency mechanism, not an attempt ceiling. LoopX bounds retries only by refusing to auto-retry a `failed` journal without `retry_failed`. The threshold-of-3 counter is a surogates invention in the spirit of that fail-closed default.

---

## 4. Take these with revision — NEEDS-REVISION

Eight proposals where the gap is real but the design was wrong. Ordered by (value ÷ revised effort).

### P1 — Durable plan · **M**

**Gap confirmed:** `tools/builtin/todo.py:147` `_session_stores: dict[str, TodoStore]` is a module-global, never persisted, never evicted; `format_for_injection` (:91) is dead (callers exist only in the vendored `study/hermes-agent/`). Compressor state is per-wake (`harness/context.py:609`, constructed at `orchestrator/worker.py:1556`).

**What the verifier refuted:** *"the agent concludes it has no plan"* overstates it. The todo list **is already durable** — `tool_exec.py:1175` emits `TOOL_CALL` with full arguments and `TOOL_RESULT` with the full `{"todos": [...]}`, and `loop_context_replay.py:243/258` replays both. After a pod switch the model still sees its last list in history. What actually breaks is narrower: (a) a `todo` *read* returns `{"todos": [], "total": 0}`, contradicting visible history; (b) `merge=true` against a cold store silently degrades to replace (`todo.py:52-79`), losing items; (c) after `CONTEXT_COMPACT` (`loop_context_replay.py:295`) the tool messages are gone and nothing re-injects. **A projection/read bug, not "the plan is lost".**

**Corrections to fold in:**
- **Ship the reducer, not the event type.** Fold over existing `tool.result` events with `name=='todo'` — `get_events` supports `types=` and is index-backed by `idx_events_session_type` (`db/models.py:444`). The in-repo precedent is `study/hermes-agent/run_agent.py:5971` `_hydrate_todo_store`. Folding *events* rather than rebuilt *messages* is the right upgrade, because `CONTEXT_COMPACT` replaces messages but never deletes events. A dedicated `todo.updated` type is an optimisation, not the mechanism — don't let it gate the fix.
- **Wrong injection slot.** `prefill_messages` is read once at wake start (`loop.py:1473`) and spliced after the system prompt (:1695) — worst place for a mutating list. Follow `loop_board.py:1-9`'s stated idiom ("Events append at the END of history — never inserted mid-list") : render at an iteration boundary (`loop.py:1607`) and re-inject right after `CONTEXT_COMPACT`.
- **`todo` is currently classified read-only** — in `CONCURRENCY_SAFE_TOOLS` (`tool_exec.py:300`, dispatched eagerly mid-stream) and `SAGA_EXCLUDED_TOOLS` (:376). Once it emits a durable event, a discarded stream can persist a plan mutation. Split read from write, or remove it from both sets. **Required, not optional.**
- **Don't content-address the id from content.** The model rewrites `content` while keeping the id; merge keys on id (`todo.py:55-66`), so a content hash forks the edit into a duplicate. Keep the model id primary (LoopX does the same — `state_projection.py:485-488`), derive only as fallback, never re-derive an existing id.
- Name collision: `todo` is already a `Task` status (`db/models.py:1299`). No DB migration — `events.type` is plain text with JSONB data, no CHECK. If streamed, extend `/work/surogates/web/src/types/session.ts:45`.
- **Drop the pydantic field-schema port.** LoopX's ~30-field `_TODO_METADATA_FIELD_SCHEMA` exists because its todos carry capability bindings and gates; surogates' item is `{id, content, status}` already validated at `todo.py:121-143`. **Keep only the `projection_gap` marker** (no todo events at all ≠ empty list) — that is the valuable import.
- Regression test: two wakes with distinct in-process stores, wake 2 issues `merge=true` and must not lose wake 1's items; plus a wake where `CONTEXT_COMPACT` precedes the read.

### P6 — Type `sessions.config` + a `session doctor` lint · **M**

**Gap confirmed:** the in-memory/DB resync hacks are exactly as described (`harness/loop_outcome_commands.py:186-200, 248-255, 375-384, 425-430`); outcome-vs-mission exclusivity is enforced only at creation (`missions/commands.py:324-332`), so a legacy row can violate it undetected; `inbox_checkin_interval_seconds` has no default and check-ins are silently off (`loop_artifact_completion.py:174-176`); `hard_stop_enabled` defaults False (`tool_guardrails.py:46`); `tool_loop_guardrails` has one read site and no API/docs (`loop.py:1455`).

**What the verifier refuted:** *"per-key read-modify-write races"* in the store helpers is **wrong**. `store.py:699-843` (`update_session_config_key`, `reconcile_session_config_key`, `clear`, `append_session_config_list`, `pop`) all take `with_for_update()` and say so in their docstrings. The genuinely unlocked writers are the **direct ORM assignments** at `missions/commands.py:348-365` and `:452-467`. Rewrite the motivation around those.

**Corrections:**
- Validating only in `update_session_config_key` covers a minority. `create_session(config=...)` accepts an arbitrary dict unvalidated (`store.py:190-234`) and is the entry point for `api/routes/sessions.py:429`, `website.py:755`, `prompts.py:221`, and surogate-ops (`/work/surogate-ops/surogate_ops/server/routes/sessions.py:711-761`). Validate via one `normalize_session_config(document)` called from `create_session` + every mutator; convert the two direct ORM writes to helpers.
- **"~18 keys" understates it.** `session.config.get(...)` alone yields 27 distinct keys; write-side adds ~20 more. Realistically 45-55 across harness/missions/board/channels/tasks/ambient/api + ops. With `extra` passthrough (correct for safety) the model catches almost nothing alone — it only pays off paired with a known-key registry emitting `unknown_session_config_key` as a **warning**.
- Reuse prior art: `OutcomeState.to_config/from_config` (`harness/outcomes.py:55-98`) and `ToolGuardrailConfig.from_mapping` (`tool_guardrails.py:41-115`) are already typed per-key parsers — both deliberately fail-soft, which is the hole. `guardrails_config_unknown_key` needs a new strict path.
- `pending_input_without_inbox_item` is not a config check — pending input derives from `inbox_items` (`session/interactive_input.py:23-60`). Keep it only if the doctor is scoped as *session* coherence, at which point it's cross-table and no longer S.
- **Ship in two steps.** Step 1 (S): read-only `session doctor` module + API route + CLI, no schema, no write-path change — pure diagnosis, cannot regress PROD, immediate payoff in the CLAUDE.md debugging flow. Step 2 (M): typed document + validation. Do **not** invert; rejecting writes on a 50-key untyped surface with live PROD rows is the risky half.

### P4 — Stagnation detection · **L**

**Gap confirmed and worse than stated.** `missions/evaluator.py:90-137` has exactly two triggers; `apply_verdict` (:318-416) writes only `last_evaluation_result/explanation/feedback` — a single-slot overwrite, no history (`db/models.py:1498-1506`). The only mission accumulator is `evaluator_parse_failures`. `/goal` likewise carries only `consecutive_parse_failures` (`harness/outcomes.py:69`). `Task.result_metadata` is unschematized JSONB set only on explicit `worker_complete(metadata=...)` (`db/models.py:1305-1312`). Nearest neighbours are not this: `tool_guardrails.py:51-59` counts identical *tool calls* within one wake; `turn_summarizer.py` is prose. The only real plateau detector is `arbor/convergence.py:147-179`, gated behind `research_run is not None`.

**What the verifier refuted in the LoopX description — three errors:**
1. **Wrong threshold source.** `build_long_task_cadence_hint` compares against `execution_profile_threshold(profile)` (`long_task_cadence.py:119,141`), which reads `degradation_policy.small_scale_streak_threshold`, **default 2** (`execution_profile.py:158-164`). It does *not* read `outcome_floor.surface_streak_threshold=3`. Two different thresholds were fused.
2. **The "teeth" are not wired to `_small_step_streak`.** `quota_with_handoff_outcome_floor` reads `handoff_readiness["post_handoff_outcome_gap_streak"]` (`quota.py:319`) — a *different* counter from `delivery_signals.py:83-99` — and is hard-gated on `if waiting_on != "codex": return quota` (:304-305), a LoopX handoff-topology precondition with no surogates analogue. `_small_step_streak` feeds only advisory markdown text (`presentation/renderers/status_markdown.py:1243-1251`). **There is no enforcement path from the surface-only streak to any block.**
3. **The floor is off by default and *is* the substring taxonomy the proposal says not to copy.** `outcome_gap_streak` returns 0 unless `outcome_markers`/`surface_only_hints` are non-empty — and `DEFAULT_EXECUTION_PROFILE` ships both empty (`execution_profile.py:22-23`). When configured, classification is `classification_contains_any(...)` substring matching over free text (`delivery_signals.py:60-71`).

**Corrections:**
- **"Required enum on `worker_complete`" does not cover the failure mode.** Most tasks never call it — `classify_attempt_outcome` treats a cleanly-ended session as COMPLETED and auto-extracts the result (`tasks/completion.py:47-83`; the tool is explicitly optional at `tasks/tools.py:216-222`). A "required" field on an optional tool leaves the column NULL on most rows. Either classify the natural-completion path too (`turn_summarizer` already runs on `summary_model` and could carry the label), or define NULL as streak-breaking and accept the detector only sees instrumented workers. **Decide this before porting anything.**
- **Don't port the floor "teeth" — they don't exist in that shape.** Port the lattice + streak (real, ~40 lines), then design enforcement natively. `_outcome_floor_blocker_already_projected` reads a LoopX todo-summary shape with no surogates analogue; duplicate-blocker detection must be rebuilt over `Task.blocked_reason` / inbox rows.
- **"Force an evaluation" is largely redundant.** In the target failure mode every terminal task already fires `task_terminal` (`evaluator.py:127-132`) each coordinator turn (`loop.py:2258`). The evaluator already runs — it has no memory. The load-bearing change is injecting the streak into the prompt/continuation template plus a hard terminal.
- Use 3 as a deliberate surogates choice, not "LoopX's threshold". `MissionStatus` already has `"blocked"` (`missions/models.py:16-24`) so no new status is needed.
- **Migration:** no Alembic. `run_migrations` is `Base.metadata.create_all` + retrofit DDL (`db/engine.py:98-123`), and `create_all` will **not** add a column to the existing `tasks` table — it goes in the retrofit ALTER block of `db/observability.sql` (which already ALTERs `tasks` at :356/:443 and `missions` at :415/:436).

### P3 — Per-objective spend ledger · **L**

**Gap confirmed:** `missions` has no token/cost/deadline/wall-clock column (`db/models.py:1446-1520`); `missions/store.py:171 increment_iteration` is exactly the mutable counter LoopX avoids; `SessionCostTracker` is compared to nothing (`harness/cost_tracker.py`), and its only consumers are the summary event and post-hoc commerce settlement (`loop_artifact_completion.py:545-697`). `orchestrator/worker.py:1524 IterationBudget(max_total=90)` is the sole prod construction, created per wake.

**What the verifier refuted:** *"90 is hardcoded with no config key and no per-agent override"* is **wrong in a way that changes the fix.** The override already exists and is already computed — the worker just ignores it. `harness/agent_resolver.py:170-176` sets `cfg["max_iterations"] = min(agent_def.max_iterations, 30)`; `tools/builtin/coordinator.py:331-338`, `delegate.py:402/472-477` and `tasks/spawn.py:58-64` all derive child budgets. But `grep -rn max_iterations surogates/orchestrator/` returns **zero hits** — children are separate enqueued sessions woken by worker.py:1524, so every child gets a flat 90. **The carefully-derived cap is dead config.** The one-line change is "make worker.py:1524 honour `session.config['max_iterations']`", not "add a config key" — strictly smaller, fixes a live bug, needs a regression test.

**Corrections:**
- **Don't frame this as importing LoopX** — surogates already ships the pattern for research missions: `arbor/store.py:346-355 cycles_spent()` = COUNT over records, no counter; ceiling in `research_runs.meta["max_cycles"]`, read at `loop_mission_evaluator.py:69-70`, enforced at the evaluator boundary via `adjust_research_verdict(budget_exhausted=...)` (`arbor/evaluator_policy.py:32-59`), surfaced in the judge prompt. Generalise that. Note `db/models.py:1436-1444`'s stated convention ("Research missions — sidecar tables; `missions` is never altered") and decide deliberately.
- **`budget_cost_usd` cannot be the enforcement quantity.** `harness/model_metadata.py:435-467` returns `(0.0, None)` with no catalog rate — "that is how a 1.48M-token session recorded estimated_cost_usd = 0" — and `loop.py:2960-2976` only logs. PROD runs tier sentinels (`surogate` / `surogate-pro`) with 0.0 prices. **Make `budget_tokens` primary** and treat cost as advisory, or fail closed when `priced_model is None`.
- Spend join is `sessions.task_id → tasks.mission_id`, not `tasks.mission_id` alone — `Task.current_session_id` only points at the in-flight attempt. Good news: the tree is exactly one level deep (`WORKER_EXCLUDED_TOOLS` strips `spawn_task`/`spawn_worker` at `coordinator.py:47-54`, and `tasks/spawn.py:63-88` doesn't copy `active_mission_id`), so the two-table sum is complete.
- **Trap:** `IterationBudget` is constructed in `harness_factory` but `wake` re-fetches the session — per-session sizing must be a ctor arg (as budget already is at worker.py:1952), never a factory-side overlay.
- A second objective layer has the identical shape — `harness/outcomes.py:318-335`. Cover it or explicitly scope it out.
- New `budget_exhausted` status must land in `missions/models.py:23`, `missions/store.py:21`, **and** `sdk/agent-chat-react/src/types.ts:271` + dist rebuild (a known release-breaker).
- Keep the best instinct verbatim: budget is checked **after** health and gates, never a reason to run.

### P2 — Suspend-and-resume for human gates · **L**

**Gap confirmed:** `tools/builtin/ask_user_question.py:54` `ASK_USER_QUESTION_MAX_WAIT_SECONDS = 30*60`; `_wait_for_response` (:228) parks the harness at 1 Hz renewing the lease.

**What the verifier refuted — three sub-claims:**
1. *"No park-and-resume primitive anywhere"* — surogates has **two**. Task layer: `tasks/tools.py:768-788` `worker_block` writes `status='blocked'` + reason, emits `TASK_BLOCKED`, publishes `INTERRUPT_CHANNEL` to terminate the session (lease and slot released); `unblock_task` (:412-456) flips to `ready` with `additional_context` "delivered as part of the next attempt's initial input". Session layer: the `action_required` path — `loop.py:3080-3094` emits and **ends the turn** (no park), `api/routes/inbox.py:388-400` answers and calls `_wake_session_from_request`. Suspend + durable record + resume-on-answer already ships; it just carries the answer as a user message. A third: `loop.py:869-885 _run_browser_setup`.
2. *"Burning a worker slot"* is presented as unsolvable — the fix already exists as an in-repo primitive `ask_user_question` simply doesn't call. `tools/builtin/delegate.py:376-395` releases the parent's `TurnConcurrencyGate` slot for a blocking wait ("the gate is meant to track active work, not sleeping waiters"), with `_reacquire_gate_with_backoff` at :67-95. **~8 lines** removes the per-tenant slot cost (cap 10, `runtime/turn_gate.py:40-100`).
3. *"A new replay branch is the only real work"* — **that branch already exists and is correct.** `loop_context_replay.py:256-266` turns any `TOOL_RESULT` into a `role: tool` message; the deferred-user-message logic (:230-238, 262-266) preserves ordering; `sanitize.py:99-104/146-160` already injects stubs for unmatched tool_calls.
4. *"Silent-and-lossy"* is overstated — confirmed on the web widget (`api/routes/ask_user_question.py:141-160` emits with no enqueue), but on channels a late answer is delivered as a normal message (`session/interactive_input.py:186-190`) — degraded, not dropped.

**Corrections — split into three shipments, cheapest first:**
- **Ship 1 (hours):** release + re-acquire the turn gate in `_wait_for_response`, copying `delegate.py:376-395` + `:67-95` verbatim (`turn_gate` is already in tool kwargs via `loop.py:471`). Keep the lease renewal. One file.
- **Ship 2 (S):** enqueue the session after both response paths emit `ASK_USER_QUESTION_RESPONSE` (`api/routes/ask_user_question.py:141`, `interactive_input.py:99-107`), reusing `inbox.py:130-140`. On timeout emit a real `TOOL_RESULT`. Fix `inbox_expire.py:26` to expire `input_required` past the wait cap regardless of session status (the frontend already renders "Expired" from a hardcoded mirror at `/work/surogate-ops/frontend/src/features/work/inbox-expiry.ts:6`).
- **Ship 3 (L):** the true suspend. Not one branch — it needs unwinding out of `tool_exec` and `loop.py` without going through `_complete_session` (`loop_artifact_completion.py:699-790`: destroys the sandbox pod, `memory_manager.on_session_end`, drains `TURN_SUMMARY`, settles **commerce and allowance reservations**). New status ripples through four hardcoded allowlists in `api/routes/sessions.py:589/1199/1236/1278`, `inbox_expire.py:21`, `ask_user_question.py:225`, `tasks/dispatcher.py`, `channels/identity.py`, `api/routes/events.py`, plus frontend `use-needs-input.ts:36` (filters `status === "active"` — suspended sessions would vanish from the very tray this serves). **Sharpest trap:** `find_orphaned_sessions` (`store.py:1744-1810`) re-enqueues a stale leaseless session whose latest event is an `llm.response` *with* tool calls — exactly the shape a suspended session leaves. Unhandled, suspend becomes an infinite re-wake loop.
- **Drop `gate_scope: advisory`** — it reinvents `action_required`. Note surogates deliberately went the *other* way: `loop.py:1994-2010` converts a non-blocking final answer *into* a blocking ask.
- **Keep** the mandated reply format (`user_gate.py:194-263`) — one string edit in `ASK_USER_QUESTION_DESCRIPTION` (:59-85), improving free-text parse in `channels/platforms/telegram_interactive.py`. Ship with Ship 2.
- Decide first: does the answer return as a tool result (needs Ship 3) or a user message (works today)? The task layer already chose the latter.

### P5 — Human approval as a durable, scoped, expiring grant · **L**

**Gap confirmed:** `governance/policy.py:72 overridable: bool = False`, never assigned True; reads only at `harness/tool_exec.py:1239,1253`. No `approvals` table. Approve emits only a `[governance decision] APPROVE …` user message (`api/routes/inbox.py:347`) — no grant, no re-dispatch.

**What the verifier corrected — one fatal, one bug-recreating:**
1. **FATAL:** *"on retry `GovernanceGate.check` consults live grants first"* cannot work. `check` is **synchronous** (`policy.py:205-317`) with no DB handle and no session context — `tool_exec.py:1224-1226` deliberately doesn't pass `session_id`. The gate is a frozen per-wake object from static config (`runtime/governance.py:41 _FLOOR_GATE = GovernanceGate()` module singleton; `with_profile` at :600-608 freezes it). Put the grant query in the **async caller** at `tool_exec.py:~1230`, which already has `store`, `session`, `lease` and awaits. Keep the gate pure.
2. **Recreates the bug being fixed:** `check()` returns early at `policy.py:296-304` for `self._open_policy or tool_name.startswith("mcp__")` — and open policy is the **production default** (:101). MCP/Composio tools — the highest-risk external-side-effect surface — take that early return unconditionally. **The approval check must sit above line 296**, next to the deny-list fast path at :247, or it is as unreachable as `overridable` is today. Regression test: an `mcp__*` tool under open policy still hits the gate.
3. **Approve must re-dispatch.** The inbox payload stores a **truncated** args excerpt (`_truncate_args`, `tool_exec.py:1245`) — persist full args on the grant row or re-read the original `tool.call` event.
4. **Dual-repo:** the respond handler exists twice — `api/routes/inbox.py:347-362` and `/work/surogate-ops/surogate_ops/server/routes/sessions.py:1986-1996`. Both must mint the grant.
5. **Cheaper than assumed:** no Alembic — a new table is a model addition (`db/engine.py:98-123`). But **add** to the estimate: `surogate_ops/server/models/agent.py:45` AgentPolicy + `_validate_tool_names` (which validates against `BUILTIN_TOOL_NAMES` and would 422 an `mcp__*` approval list), `services/agents_shared.py:324/477`, `surogates/runtime/governance.py:83-133`, and `frontend/src/features/agents/governance-policy.ts` + `work-agent-inbox-page.tsx`.
6. **Descope the dual-staleness port.** LoopX's `newer_event_count_7d` counts runs against a *goal*; the surogates analogue has no counter and `events.id` bumps on every LLM delta. Ship scope + `expires_at` + `max_uses`/`consumed_at` + recompute-on-read reasons first.
7. **Calibrate:** LoopX's boundary authority is **advisory** — the repair hint carries `"allowed": True, "notify": "DONT_NOTIFY"` (`projection_repair.py:204-212`) and steers the next action rather than hard-blocking. Porting it into `tool_exec` makes it a hard gate on a concurrent, leased, multi-worker path — strictly stronger than anything LoopX validates. `max_uses` must decrement **under the session lease** or two parallel calls spend a single-use grant.

**Keep verbatim:** recompute-on-read from an explicit reason list (`boundary_authority.py:120-148`) and near-miss surfacing (`projection_repair.py:237-262`). Those are the portable parts.

### P9 — Mission evidence ledger · **L**

**Gap confirmed, worse than stated.** `db/models.py:1488 rubric: Mapped[str] = mapped_column(Text)` — one free-text blob; the only judgement memory is four scalar `last_evaluation_*` columns. The rubric is never structured (`missions/commands.py:110-123` splits on `_RUBRIC_RE`; same at `harness/outcomes.py:143-150`). **`build_evaluator_prompt` (`evaluator.py:183-278`) does not inject the prior verdict, explanation, or feedback — not even the mission's own `last_evaluation_feedback`. Round N has zero knowledge of round N-1.** And `result` is truncated to 400 chars at :229 — the evidence isn't merely unschematized, it's a 400-char prose stub.

**Two design defects the verifier caught:**
1. **The latch is anti-LoopX and has no invalidation driver.** "Already-evidenced criteria stated as settled" is the *opposite* of the mechanism: LoopX re-runs the validator every turn; `executor.py:866-876` reuses a stored receipt only within one turn's journal on resume, never across turns. Under the proposal, "tests pass" latches in round 3, round 5 changes the code, and the judge is told it's settled. `superseded_by` is in the schema but nothing ever writes it. **Fix:** latch only monotone criteria (an artifact exists, a decision was recorded); re-run command/file-backed criteria each evaluation and hold the last receipt + timestamp, so the prompt says "criterion 3: last verified 2 rounds ago".
2. **A `tool.result` event id is not non-forgeable.** It proves *some* tool ran. The coordinator chooses tool and arguments; `bash -c 'echo tests passed'` produces a real `tool.result`. LoopX's non-forgeability comes from argv fixed at plan time (`executor.py:462-517`: normalised argv, fixed cwd, output discarded, exit code only). The real port: **declare a validator command per criterion at mission creation and trust the exit code** — which is exactly what Arbor already does (`meta.eval_cmd_test`, `tools/builtin/arbor.py:618-622`: "a research run without a held-out eval cannot merge").

**Missed prior art that changes the plan:** `arbor/evaluator_policy.py:30-66 adjust_research_verdict` already implements "the LLM cannot mint the terminal verdict" generically inside the judge pipeline — `satisfied` demoted unless a machine-written `test_trunk_score` improved and the report is done; `failed`/`blocked` demoted without corroboration. Invoked at `harness/loop_mission_evaluator.py:165-173`, right after the judge and before `apply_verdict`. **That is a ready-made, mission-kind-dispatched hook.** The cheapest version of this proposal is a generic `verdict_policy` at that hook plus per-criterion machine checks — not a new ledger. Note per-criterion verdicts break `adjust_research_verdict`'s `{result, explanation, feedback}` contract.

**Migration:** new tables are free under `create_all`; a new *column* on `missions` needs a guarded retrofit ALTER in `db/observability.sql` (`db/engine.py:90-95`). Better: put criteria in their own table and don't touch `missions`.

**Recommended reshape:** (1) structure the rubric into ided criteria at creation and persist per-criterion verdict *history* — that alone kills the re-litigation churn; (2) inject prior verdicts as history with recency, not as settled facts; (3) generalise `evaluator_policy`'s gate to accept a per-criterion declared validator command with trusted argv and exit-code-only semantics.

### P7 — "Should I tick?" gate for ambient · **M**

**Gap confirmed:** `ambient/store.py:152-168 mark_fired` sets `next_run_at = now + cadence_seconds` with no observation state; `AmbientScheduleRow` (`db/models.py:746-800`) has no hash/counter columns; `materialize.py:14-90` unconditionally creates + enqueues (a full LLM turn); `decision.py:16-29` gates only the POST. Zero no-change gating anywhere in surogates.

**What the verifier refuted — three sub-claims:**
1. *"`mark_fired` advances unconditionally even when materialisation fails"* — **it does not.** `materialize.py:89` calls it **last**, after `enqueue_session` (:83). It's the only call site. The advance is already conditional. **The real defect is the inverse:** a failed materialise leaves `next_run_at` in the past, so the row is re-claimed every 30s once the 120s lease expires — a hot retry loop with no failure backoff.
2. *"Port `max_no_change_before_replan`"* — **that field is inert in LoopX.** Grep finds it only in field-list constants and as a passthrough kwarg; nothing compares `consecutive_no_change` against it. The rule that *is* enforced uses a global threshold (`autonomous_replan_obligation.py:290-343`).
3. The cited anti-deadlock line (`deferred_resume.py:270-299`) is `todo_summary_monitor_blocked_resume_items` — unrelated to streaks or forced ticks.

Also: `consecutive_no_change` does **not** drive LoopX's backoff. Cadence is disposition-driven — `MONITOR_WAIT_PROGRESSION_MINUTES = [15, 30, 60]` (`scheduler_hint.py:50`), a bounded 3-step ladder, not `2**n`. And LoopX's quiet verdict doesn't prevent a model call (the heartbeat agent is already running); it saves a `quota_slot_spent` append. The proposal's gate is **stronger** than its cited source.

**Design objection that must be fixed:** the fingerprint contradicts the ambient loop's purpose. `ambient/prompt.py:16-21` lists three triggers, two invisible to (inbound event id, task state, `updated_at`): *"a thread you're involved in went quiet with an open question"* fires **precisely because nothing changed** — a no-change gate suppresses the canonical case, and `2**n` grows detection latency exactly when the thread is quietest. *"Something relevant surfaced"* comes from connected MCP/Composio tools (`materialize.py:32-44` inherits the principal for exactly this) — no local DB fingerprint can observe it.

**Corrections:** (a) include an elapsed-quiet bucket so crossing the quiet threshold registers as a *change*; (b) hard-cap the skip streak with a forced tick, using a config constant not the inert LoopX field name; (c) cap the multiplier low (LoopX's 15/30/60 is 4x, not unbounded). **Drop the `/loop` extension** — classifying "a cron prompt that is a pure poll" means reading free-text prompts, and surogates has already retired regex prompt classifiers; make it an explicit opt-in flag if wanted. **Migration:** two new columns need a retrofit `ALTER TABLE … ADD COLUMN IF NOT EXISTS` in `db/observability.sql` (17 such statements already exist). **Value unverified:** `ambient_enabled` defaults False (`surogate_ops/server/services/mate_settings.py:28`), so "48 calls/channel/day at fleet scale" is a per-enabled-channel worst case — count `ambient_schedules` rows in PROD before scheduling this. **Ship the failure-backoff fix separately; it's the real bug in that code.**

---

## 5. Don't take these

No proposal was fully REFUTED — but several sub-claims were, and those corrections are load-bearing (all folded into §4). The genuine "already solved, better" list:

| LoopX idea | What in surogates kills it |
|---|---|
| Markdown `ACTIVE_GOAL_STATE.md` as source of truth (`bootstrap.py:425`) | LoopX is file-first because it has no server. surogates has an append-only `events` table with monotonic ordering, a lease-guarded cursor and deterministic replay (`harness/loop_context_replay.py:189`). Adopting Markdown adds a second writer next to Postgres — the exact failure LoopX's own `truth_contract` warns about. Take the field-schema *discipline* (P1), not the file. |
| `event_sourced_state.py` as infrastructure | Shadow-mode: one non-example call site (`control_plane/todos/event_writeback.py:270,425`), and `build_event_store_migration_bridge` hardcodes `promotion_allowed: False`. `SessionStore.emit_event` (`session/store.py:967`) is production-hardened with redaction, atomic counters, inbox mirroring and Redis pub/sub. Read the reducer (`:684-796`) as design reference; don't port it. |
| `task_lease_v0` file leases + write-scope glob overlap; git-worktree workspace guard | `session_leases` atomic `ON CONFLICT` steal (`store.py:1561`) + task claim via `UPDATE … RETURNING` (`tasks/dispatcher.py:164`). Write isolation is a per-session sandbox, so there is no shared-worktree collision surface. LoopX's leases are also agent-driven only (call sites: `cli_commands/task_lease.py`) — a protection an LLM can forget to take isn't a protection. |
| Rendezvous-hash peer selection (`agents/runtime_model.py:63-73`) | Solves coordination-free assignment. surogates assigns via a single Redis `BZPOPMIN` queue + atomic DB claim (`orchestrator/dispatcher.py:311`); duplicate execution is structurally impossible. Hashing would be a second, weaker authority. |
| `dreaming` advisory lane (`dreaming.py:241-334`) | A 20-field capability declaration wrapping a keyword match over four buckets. surogates already has two objective-proposal lanes (missions; Arbor's Idea Tree with machine-verified scores at `tools/builtin/arbor.py:593`). |
| `ready_score` (`ready_score.py`) | Nine unjustified magic weights (12/8/8/12/12/12/20/10/6) + a shields.io badge; the quota check only *observes* an already-computed decision. Solves "is my local CLI wired up" — surogates is a k8s service with a release pipeline. Only the `_check(...)` record shape survives, in much smaller form (P6). |
| canary/premerge gate, `doctor` install-freshness, self-update symlink rollback | All target a locally-installed self-updating CLI. surogates ships as a container image; provenance and rollback are the deploy pipeline's job. (The tracked-side-effect guard with `git restore` is worth remembering — but for the sandbox layer, and surogates already has shadow-git checkpoints at `tools/utils/checkpoint_manager.py`.) |
| Tolerant action-drift alignment + `NEXT_ACTION_EXECUTABLE_PATTERN` verb regex (`state_projection.py:159-177, 739-804`) | Heuristics for reconciling prose against structure — a problem LoopX has because Next Action *is* prose. surogates' signals are already structured (mission verdicts, task transitions, tool results). Importing substring/verb matching (incl. hand-tuned CJK) adds false positives; LoopX discards the granular result anyway. |
| `*_v0` schema strings + declarative `boundary: {...}` dicts | 501+ literals with one real consumer; the boundary dicts are hardcoded constants nothing validates. surogates' needs are met by the closed `EventType` enum (`session/events.py:10`) and would be better served by pydantic (P6). |
| Benchmark countability / attempt-phase accounting (`benchmark_ledger_countability.py:245-313`, `benchmark_core/attempts.py:112-199`) | **Genuinely LoopX's best ideas** — but out of scope for `/work/surogates`. No benchmark fleet runs inside the harness. Re-propose against the surogate-ops evaluation pipeline, where "infra failure counted as 0.0" is a live risk. |

---

## 6. License & sourcing

LoopX is **MIT**; surogates is **AGPL-3.0**. MIT → AGPL is the permissive direction: incorporating MIT code into an AGPL work is fine, provided the MIT copyright notice and permission text travel with the copied code.

**Direct port is fine (copy with a header attribution comment naming file + line range):**
- `long_task_cadence.py:36-70` — `_progress_granularity` + `_small_step_streak`, ~40 lines of arithmetic over enums (P4). Note the threshold correction: LoopX's is 2, not 3.
- `boundary_authority.py:120-148` — the recompute-on-read six-reason pattern (P5).
- The `_check(...)` record shape `{status, detail, action}` with auto-collected recommendations (`ready_score.py:342-350`) (P6).

**Reimplement, don't copy:**
- Anything touching LoopX's Markdown/`flock`/file layout — the substrate is wrong for surogates.
- The heartbeat receipt (P8): key it on surogates' `events` table, not on a rollout log; and the attempt ceiling is a surogates invention, not a port.
- The evidence ledger (P9): LoopX's non-forgeability is argv-fixed-at-plan-time; the surogates version should generalise `arbor/evaluator_policy.py`.

**Do not copy the style** regardless of license: `dict[str, Any]` payloads with hand-rolled `_dict_field` guards, schema strings as documentation, and substring-matching taxonomies (`'runner' in failure_class`) are the parts the study flags as the anti-pattern.

---

## 7. Suggested first move

**Ship P8 part 2 today, then P8 part 1 this week.**

The two-line refund cap at `/work/surogates/surogates/harness/loop.py:2034-2047` closes a **verified non-termination hole** — `refund()` is unconditional (`harness/budget.py:35-39`), the loop guard is `while self._budget.remaining > 0`, there is no wall-clock deadline anywhere in the wake loop, and the path additionally resets a sibling counter so two failure modes can ping-pong forever. It is one counter declaration, one cap, one regression test, and it is the only item in this entire study where a provider misbehaviour can hang a worker indefinitely with no backstop.

P8 part 1 is the natural follow-on: same file family (`orchestrator/dispatcher.py` only), no DB migration, no schema change, no prompt change, and it closes the symmetric hole — a poison session that OOM-kills a worker every 60s forever, which the existing crash-loop breaker structurally cannot see because a SIGKILL never reaches the `except`. Using an `events`-table count of `harness.recovered` since the last cursor-advancing event (rather than the originally proposed Redis key) makes it cheaper *and* more correct than the proposal, and leaves an operator-visible trail in the same log they already query.

Both are pure hardening with no behavioural downside, both are independently rollback-able, and neither depends on any of the L-sized items. Everything else in §4 needs a design decision resolved first — P1 needs the read-vs-write tool split, P3 needs the token-vs-cost choice, P4 needs the natural-completion classification decision, P5 needs the above-line-296 placement, P9 needs the latch-invalidation rule. Ship the two that don't.