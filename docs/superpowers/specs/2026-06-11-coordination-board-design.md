# Coordination Board — design

- **Date**: 2026-06-11
- **Status**: implemented (v1) — see `docs/board/index.md`
- **Source**: adaptation of DeLM — *Decentralized Multi-Agent Systems with Shared Context* (Mao & Mirhoseini, arXiv:2606.10662) — to the surogates harness. Reference implementation studied at `study/DeLM/` (notably `src/shared_lessons.py`, `src/verifier.py`).
- **Related**: `docs/sub-agents/2026-05-16-subagent-task-layer-v1.md` (task layer this builds beside), `docs/superpowers/specs/2026-05-16-mission-orchestrated-goals-design.md` (missions, untouched in v1)

## 1. Problem

Surogates parallelism is scatter-gather: workers spawned via `spawn_worker`, `delegate_task`, or `spawn_task` report results only upward (`worker.complete` → parent log). Sibling workers are mutually blind:

- Parallel workers re-discover the same environment facts (repo layout, broken harnesses, auth quirks) independently.
- Failed approaches die with the session that tried them; siblings and retry attempts repeat them. Mission corrective iterations (`needs_revision` → new tasks) are especially prone.
- The coordinator learns nothing until a child terminates, then digests a ≤10 KB self-reported summary.
- Task retries get `worker_context` — the prior attempt's *unverified self-report*.

DeLM's measured answer: a shared, append-only board of compact **typed, verified notes** that all parallel agents read and write, replacing routing-through-the-parent for intermediate knowledge. Their ablation shows admission-time verification is the largest single accuracy contributor.

**Honest expectations.** DeLM's headline gains (+9–10 pts, −50 % cost on SWE-bench) used a weak base model (Gemini Flash). On Claude Opus 4.6 the gain was ~1 pt and cost-neutral. We adopt it to fix the observable coordination pain above, not to chase benchmark deltas. The board pays off proportionally to fan-out width and mission iteration count; for 1–2-worker sequential flows it is roughly overhead-neutral.

## 2. Decisions log

| Decision | Choice |
|---|---|
| Primary use case | Sibling coordination (workers on different subtasks of one goal). Best-of-N test-time scaling is a future usage pattern on the same substrate. |
| Board scope | Coordination group = fan-out tree. Group key in v1 = root coordinator's session id. |
| Activation | Automatic on first fan-out. No `board_enabled` flag, no per-org toggle. |
| Verification | Always-on LLM verifier (tenant `llm_summary` model) after free deterministic pre-checks. **Fail-closed** on verifier error. |
| Subtask generation | Stays parent/coordinator-owned. Workers cannot enqueue sibling tasks (deviation from paper §3.1; see Non-goals). |
| Unfolding | Lightweight refs only (note → event/artifact pointer + `expand_note` tool). No gist→summary→raw hierarchy. |
| Missions | **Untouched in v1.** Mission fan-outs benefit automatically (coordinator is a fan-out root). Explicit integration is Phase 2 (§13). |
| Storage | New `board_notes` table (task-layer idiom). Rejected alternatives: memory-file board (concurrency/typing mismatch), event-log-native board (status transitions don't fold well; cross-session event reads break the model). |

## 3. Architecture overview

```
            ┌────────────────────────────────────────────┐
            │ board_notes (PostgreSQL, org + group scoped)│
            └────▲───────────▲────────────▲──────────────┘
        share_note       render        purge job
        (verified)      (windowed)
            │               │
   ┌────────┴───┐   ┌───────┴────────────────────────────┐
   │ writer:    │   │ readers: join snapshot + per-      │
   │ any group  │   │ iteration delta, appended as       │
   │ member     │   │ durable board.update events        │
   └────────────┘   └────────────────────────────────────┘
```

Group formation: first spawn → parent self-assigns `context_group_id = str(parent.id)` (if absent), children inherit through all three spawn paths. Everyone with the key gets the board tools and the read path.

## 4. Data model

New table `board_notes` (Alembic migration, same idiom as `tasks`):

| Column | Type | Notes |
|---|---|---|
| `id` | BIGSERIAL PK | compact ids; exposed to the model as `n<id>` for `expand_note` |
| `seq` | BIGINT NOT NULL | monotonic change cursor — set from a sequence on insert **and re-set on every status transition** (supersede/expire/renew), so one cursor covers new notes and state changes |
| `org_id` | UUID FK → orgs | tenant scoping |
| `group_id` | UUID, indexed | v1: root coordinator session id. Plain UUID, **no FK** — Phase 2 reuses the column for mission ids |
| `writer_session_id` | UUID FK → sessions | |
| `writer_label` | VARCHAR(16) | denormalized display handle: `coord` for the group root, else `w1`, `w2`, … by order of first admitted note |
| `type` | VARCHAR(16) | `FACT` \| `FAIL` \| `CLAIM` \| `RESULT` |
| `content` | VARCHAR(400) | caps: 200 chars (`FACT`/`FAIL`/`CLAIM`), 400 (`RESULT`) |
| `ref` | JSONB NULL | unfold pointer, see §8 |
| `status` | VARCHAR(16) | `active` \| `superseded` \| `expired` |
| `expires_at` | TIMESTAMPTZ NULL | `CLAIM` only |
| `created_at` / `updated_at` | TIMESTAMPTZ | |

Indexes: `(group_id, seq)`, `(group_id, status)`, `(org_id)`.

Rejected notes are **never inserted** — rejection is tool-result feedback only.

### Note types

Four types — DeLM's six minus `TRIED`/`OBSERVED`, which produced chatter DeLM needed a dedicated suppression filter for; with an always-on LLM gate we refuse to pay verifier calls for noise.

| Type | Semantics | Example |
|---|---|---|
| `FACT` | Verified, reusable knowledge. Must name concrete anchors (file, symbol, endpoint, error class). | `slack adapter bypasses outbox for DMs — channels/slack.py:214` |
| `FAIL` | A dead end: approach tried and ruled out, with the observed reason. Highest peer value. | `pytest-in-sandbox for socket mode fails: egress blocked` |
| `CLAIM` | TTL'd "I am working on X" to prevent overlap. Default TTL 300 s, renewable. | `claiming telegram adapter refactor` |
| `RESULT` | Candidate outcome with mandatory structured evidence: `outcome=…\|evidence=…\|risk=…`. Generalizes DeLM's `PATCH_SUMMARY`. | `outcome=slack adapter migrated\|evidence=pytest tests/channels/slack 14/14 passed\|risk=DM path untested` |

## 5. Group formation & membership

A session is a board member iff `sessions.config["context_group_id"]` is set.

- **Parent self-assign**: each spawn handler sets `parent.config["context_group_id"] = str(parent.id)` if absent, then stamps the child with the parent's (possibly just-assigned) value. The coordinator is a first-class member — DeLM's planner reads lessons too.
- **Three spawn paths**:
  - `tools/builtin/coordinator.py` (`spawn_worker`) and `tools/builtin/delegate.py` (`delegate_task`): one line each where `child_config` is already assembled from the parent session.
  - `tasks/spawn.py::_create_session_for_task`: this path builds config from `agent_def + task` (`_build_task_worker_config`), not from the parent session — inherit by reading the parent session's config via `task.parent_session_id` at session-creation time. Doing it at creation (rather than stamping the task row at `spawn_task`) means **retry attempts** rejoin the same board and inherit prior attempts' notes with no task-schema change.
- Grandchildren (orchestrator-role delegation) inherit the *same* group id: one board per fan-out tree, not per level.
- **Tool gating**: `share_note` / `read_board` / `expand_note` are visible only when the config key is present — same mechanism as `worker_block` gating in `_filter_effective_tools`. Solo chat sessions never see board tools.

## 6. Write path — `share_note`

```
share_note(
  notes=[{type, content, ref?}, …],   # batched: sharing costs one tool call
  ttl_seconds?                         # CLAIM only, default board_claim_ttl_seconds
) → {admitted: [{id, type}], rejected: [{index, reason}]}
```

Pipeline per batch:

1. **Deterministic pre-checks** (free, ordered):
   1. type validity; content non-empty; size caps;
   2. prompt-injection scan (reuse `harness/prompt.py::_INJECTION_PATTERNS`) and sensitive-content scan (reuse `memory/store.py::scan_memory_content`) — board content is cross-session prompt input and must clear the same bars as memory;
   3. exact-dedupe against the group's *active* notes (normalized content + type). Exception: a `CLAIM` re-posted by its own writer is a **renewal** — refresh `expires_at`, bump `seq`, skip insert (renewals bypass the cap below: the claim already holds a slot);
   4. active-`CLAIM` cap per writer (default 2), applied to **net-new** claims only — renewal detection must run first, or a writer at the cap could never renew its own claims.
2. **LLM verification** (always-on): one batched call to the tenant `llm_summary` endpoint. Checks per type: `RESULT` evidence must describe a check *actually run* with a concrete outcome (semantic judgment, not just DeLM's phrase blacklist); `FACT`/`FAIL` must be specific and self-contained (anchored to files/symbols/errors — not "made progress"); `CLAIM` must name a concrete target. Returns per-note keep/reject + reason (JSON).
3. **Admit**: insert surviving rows. A new `RESULT` from a writer marks that writer's previous active `RESULT` `superseded` (latest-wins, with `seq` bump). Emit one `board.note` event on the writer's session (transcript audit).
4. **Return** admitted ids + per-rejection reasons — the model learns the gate's standards from feedback (DeLM relies on this for note quality).

**Failure mode — fail-closed**: on verifier error/timeout/parse failure, admit nothing; tool returns `verification unavailable — retry on a later turn`. Rationale: notes are advisory (losing one beat is cheap) and the board's value rests on the invariant *everything visible passed the gate*. (DeLM falls back to deterministic-only; we deliberately don't.)

Concurrency: append-only inserts; no locks. The claim cap and dedupe checks are read-then-write with a tolerable race (render-time dedupe, §7, catches residual duplicates).

## 7. Read path — durable delta events

**Constraint** (hard-won, see `harness/loop.py` cache note around the memory block): never insert changing content mid-list (breaks provider prefix cache the moment the insertion point shifts), and never rely on transient blocks (vanish from the next iteration's rebuilt `api_messages`, leave no transcript). DeLM re-renders the full board into every call; the surogates-native equivalent is **append-only durable events**:

- **Join snapshot**: on the first iteration where a session has the group key but no cursor, append one `board.update` event containing the full windowed render; set `session.config["board_cursor"] = max(seq)`. (Phrased per-iteration, not per-wake, because the *parent* acquires the key mid-wake at its first spawn and must join from the next iteration; children join at their first wake.)
- **Per-iteration delta**: at the top of each harness iteration (before `api_messages` rebuild), one indexed `SELECT … WHERE group_id = ? AND seq > cursor`. If rows: append a compact durable `board.update` event — new notes plus status transitions (`w2's RESULT superseded`, `claim expired: …`) — and advance the cursor. Appended at the end of history: prefix-cache-safe, replay-stable, and the transcript records exactly which board state each worker saw and when.
- Sleeping sessions fold everything missed into one delta at next wake. Staleness bound: one iteration while awake, one wake while asleep.
- Old `board.update` events age out through normal context compression; current truth is always available via `read_board`.

**Windowed render** (used by snapshot, `read_board`, and the REST endpoint):

- Budget: `board_snapshot_window_tokens` (default 600; ≈4 chars/token).
- Active notes only; expired claims filtered; exact-dedupe.
- Priority: `RESULT` > `FACT` > `FAIL` > `CLAIM`; newest-first within type.
- Protected reserve: 35 % of the budget held for `FAIL` so dead ends never scroll out (DeLM's protected-reserve rule).
- Line format: `[n42 w3/FAIL +2m] content` (note id, writer label, age). Claims show remaining TTL.
- Overflow: `… +N more — call read_board`.

Deltas use the same line format, capped at `board_delta_max_chars` (default 1200), oldest dropped with the same overflow pointer.

Cursor semantics: **any durable render advances the cursor** — join snapshot, delta, and `read_board` tool results all live in history, so re-delivering their content as a delta would be duplication.

## 8. Reading tools

- **`read_board(types?: [..])`** → current consolidated render (supersede/expiry applied), budget `board_read_tool_window_tokens` (default 1500). For decision points: a coordinator planning after fan-out, a worker double-checking before committing to an approach.
- **`expand_note(note_id)`** → bounded detail behind a note's `ref`:
  - `{kind: "event", session_id, event_id}` → that event's content, truncated to 4000 chars. **The target session must belong to the same group** — refs must not become a side door into arbitrary org sessions.
  - `{kind: "artifact", artifact_id | path}` → artifact content via the artifact store, same truncation, org-scoped.
  - No `ref` → error (`note has no expandable detail`).

## 9. Lifecycle

- **Claims**: filtered from renders once `expires_at` passes; a sweep in the existing cleanup job flips `status='expired'` (with `seq` bump so deltas report it). Renewal per §6.
- **Supersede**: per §6; superseded `RESULT`s drop out of renders, transition reported via delta.
- **Caps**: `board_max_notes_per_group` (default 300) active notes. At cap: non-`RESULT` admissions rejected with guidance ("board full — let claims expire or supersede a RESULT"); `RESULT` always admitted (supersede frees its writer's slot).
- **Purge** (cleanup-job extension, three clauses — the first alone would leak rows for long-lived roots, e.g. a user's main chat session that never reaches `done`):
  1. all notes of groups whose root session has been terminal (`done`/`failed`) for > `board_purge_after_days` (default 7);
  2. row hygiene regardless of root status: `superseded`/`expired` rows older than `board_purge_after_days`;
  3. orphan cleanup: notes whose `group_id` matches no existing session row.

## 10. Security & tenancy

- Every query org-scoped (`org_id = tenant.org_id`); group membership (`caller.config.context_group_id == note.group_id`) required for all three tools.
- Injection + secret scanning at admission (§6) — board notes render into many sessions' prompts and the REST response; they clear the same bars as memory entries.
- `expand_note` confinement per §8.
- Board content never crosses orgs by construction (group ids are session ids of same-org sessions; org check is still enforced explicitly).

## 11. Configuration (`WorkerSettings`)

| Setting | Default |
|---|---|
| `board_snapshot_window_tokens` | 600 |
| `board_delta_max_chars` | 1200 |
| `board_read_tool_window_tokens` | 1500 |
| `board_claim_ttl_seconds` | 300 |
| `board_max_active_claims_per_writer` | 2 |
| `board_max_notes_per_group` | 300 |
| `board_purge_after_days` | 7 |

No enable flag. The verifier uses the tenant's existing `llm_summary` endpoint — no new model configuration.

## 12. Observability & API

- **Events**: `board.note` on the writer session at admission; `board.update` on reader sessions (these *are* the read path — each transcript is self-explanatory about what the agent saw). Rejections appear in the `share_note` `tool.result`. Two new `EventType` members.
- **REST**: `GET /v1/sessions/{id}/board` → `{group_id, notes: [...active...], render}`; 404 when the session has no group. Web-UI board panel is future work.

## 13. Missions: relationship and phased integration

**v1 changes zero mission code.** A mission coordinator spawning tasks is a fan-out root, so a board group forms automatically: mission workers coordinate (facts, dead ends, claims), retries inherit verified knowledge, and the coordinator receives deltas live instead of waiting for `worker_complete`. The judge, continuation loop, and mission state machine are untouched and un-risked: a bad note can mislead a worker, but cannot flip a mission verdict, because the judge never reads the board in v1.

**Phase 2 — thin mission consumers** (separate small PR, after v1 transcript evidence):
- `missions/evaluator.py::build_evaluator_prompt` gains a `# Mission board (verified notes)` block; judge guidance updated so admission-verified `RESULT` notes are first-class rubric evidence beside `result_metadata`.
- `_CONTINUATION_TEMPLATE` gains a one-line nudge: review the board (`read_board`) before planning corrective tasks.
- Mission-created groups key on `mission_id` instead of the coordinator session id (`handle_mission_create` sets `context_group_id = str(mission_id)`) — prevents board bleed when one coordinator session runs sequential missions. The coordinator's `context_group_id` is cleared alongside `active_mission_id` when the mission reaches a terminal state (`apply_verdict` / cancel); terminal-mission boards become read-only (`share_note` rejects with "mission ended"). Schema-ready (`group_id` is a plain UUID); purge keys off mission terminal status; add `GET /v1/missions/{id}/board`.
- Trigger signals to watch for in v1 transcripts: judge `needs_revision` verdicts restating what the board already established; coordinators spending turns spelunking via `worker_context`; verdicts wrong where board evidence was richer than `result_metadata`.

**Phase 3 — evidence-dependent**: if verified `RESULT` notes consistently beat `result_metadata` as judge evidence, simplify the judge's evidence assembly to lean on the board. This replaces judge *inputs*, never the mission abstraction — the judge is production's substitute for DeLM's benchmark oracle graders and stays.

## 14. Testing

- **Unit**: pre-check ordering and rejections (caps, types, injection/secret scan, claim cap, dedupe, claim renewal — including **renewal while at the claim cap**, which must succeed); verifier admission with mocked LLM (keep/reject parsing, fail-closed on error/timeout/garbage); windowed render (priority order, FAIL reserve, expiry filtering, supersede, overflow line); delta rendering of status transitions; cursor advancement rules.
- **Propagation**: each spawn path self-assigns + inherits `context_group_id`; retry sessions rejoin; tool gating on key presence.
- **Integration** (DB-backed, task-layer test idiom): A shares → B's next iteration delta contains it; late joiner gets snapshot; `RESULT` supersede end-to-end; group cap; `expand_note` group/org confinement; purge job.
- **Loop invariant**: `board.update` events append at end of history only — never mid-list (cache invariant).

## 15. Non-goals (v1)

- Decentralized subtask generation (workers enqueueing sibling tasks) — task creation stays coordinator-owned.
- Full gist→summary→raw unfold hierarchy (`UNFOLD`/`DEEP_UNFOLD`).
- Best-of-N attempt runner, winner selection, strategy diversification prompts — future usage pattern on this substrate.
- Mission code changes of any kind (Phase 2).
- Web UI board panel; Redis push/dirty-flag for delta checks (pull SELECT per iteration is cheap; flag is a noted optimization).
- Cross-group or org-global boards.
- Board-triggered mission evaluation (triggers stay `task_terminal` + `completion_claim`).

## 16. Deliberate deviations from DeLM

| DeLM | Here | Why |
|---|---|---|
| 6 note types incl. `TRIED`/`OBSERVED` | 4 types | the dropped types are chatter DeLM had to filter; we won't pay the always-on verifier for noise |
| Full board re-render injected into every LLM call | Append-only durable `board.update` events | provider prefix cache + event-sourced replay + transcript fidelity |
| Verifier falls back to deterministic on LLM error | Fail-closed | preserves the "everything visible is verified" invariant the user chose |
| Shared context at prompt head for KV-cache reuse | Deltas at history tail | surogates' cache unit is the conversation prefix, not a rebuilt prompt |
| Workers generate subtasks when queue empties | Coordinator-owned tasks | mission/continuation loop already plays this role with production guardrails |
| In-process `asyncio.Lock` board | PostgreSQL table | board members are distributed across worker pods |
