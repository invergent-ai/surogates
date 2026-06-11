# Coordination Board

## What is the Board?

The coordination board is the **shared, verified context** for a fan-out of parallel agents. When a session spawns children (via `spawn_worker`, `delegate_task`, or `spawn_task`), the whole fan-out tree forms a **coordination group** sharing one board of compact, typed notes. Workers post what they learn; every group member — siblings, retries, and the coordinator — sees it without the information being routed (and diluted) through the parent.

Without the board, results flow only upward: a child reports to its parent at completion, and siblings are mutually blind. With the board, a dead end hit by worker 1 is a `FAIL` note that worker 2 reads *before* repeating it.

## Note Types

| Type | Cap | Semantics | Example |
|---|---|---|---|
| `FACT` | 200 chars | Concrete reusable knowledge, anchored to a file/symbol/endpoint/error | `slack adapter bypasses outbox for DMs — channels/slack.py:214` |
| `FAIL` | 200 chars | A dead end actually hit, with the observed reason. Highest peer value | `pytest-in-sandbox for socket mode fails: egress blocked` |
| `CLAIM` | 200 chars | TTL'd "I am working on X" to prevent overlap (default 300 s, renewable by re-posting) | `claiming telegram adapter refactor` |
| `RESULT` | 400 chars | Candidate outcome with mandatory structured evidence: `outcome=…\|evidence=…\|risk=…` | `outcome=slack migrated\|evidence=pytest tests/channels/slack 14/14 passed\|risk=DM path untested` |

A new `RESULT` from the same writer supersedes its previous one (latest wins). Notes can carry a `ref` pointer to expandable detail (a source event or artifact).

## How a Group Forms

Automatic — no flag, no configuration:

1. On a session's **first spawn** (any of the three paths), it self-assigns `context_group_id = str(its own session id)` and the child inherits it.
2. All later spawns — including grandchildren via orchestrator-role delegation and **task retry attempts** — inherit the same group id: one board per fan-out tree.
3. Board tools (`share_note`, `read_board`, `expand_note`) appear automatically for group members and never for solo sessions (gated in `_filter_effective_tools`, force-added past restrictive AgentDef allowlists — the `worker_*` self-tool idiom).

## Writing Notes — `share_note`

```
share_note(notes=[{type, content, ref?}, …], ttl_seconds?)
  → {admitted: […], renewed_claims: […], rejected: [{index, reason}, …]}
```

Admission pipeline (rejected notes are tool feedback, never rows):

1. **Deterministic pre-checks** (free): type validity, size caps, prompt-injection scan and secret scan (same bars as memory entries), duplicate detection against the active board, claim renewal detection (re-posting your own claim refreshes its TTL — renewal runs *before* the cap so a writer at the cap can always renew), claim cap (2 active per writer), group cap (300 active notes; `RESULT` always admitted since supersede frees its slot).
2. **LLM verification** (always on, via the tenant summary model): `RESULT` evidence must describe a check *actually run* with a concrete outcome; `FACT`/`FAIL` must be specific and anchored; vague notes are rejected with a reason the model learns from. **Fail-closed**: if the verifier is unavailable, nothing is admitted ("verification unavailable — retry on a later turn") — everything visible on a board has passed the gate, with no fallback that would weaken that invariant.

Admitted notes emit a `board.note` event on the writer's session for transcript audit.

## Reading

- **Join snapshot**: the first harness iteration that sees a non-empty board appends one `board.update` event with the full windowed render (priority `RESULT` > `FACT` > `FAIL` > `CLAIM`, 35 % of the budget reserved for `FAIL` so dead ends never scroll out, expired claims filtered, exact-deduped, ~600-token budget).
- **Per-iteration deltas**: each iteration checks for board changes past the session's cursor and appends a compact `board.update` delta — new notes plus transitions ("superseded: …", "expired: …", "renewed: …"). Events append at the **end** of history, never mid-list, so the provider prefix cache and event replay stay stable; each member's transcript records exactly which board state it saw and when.
- **`read_board(types?)`**: the consolidated *current* state (supersede/expiry applied) at a larger budget — for decision points, since inline updates in history may be stale. Any durable render advances the session's persisted cursor.
- **`expand_note(note_id)`**: follows a note's `ref` to bounded detail (4000 chars) — `{kind: "event", session_id, event_id}` or `{kind: "artifact", session_id, artifact_id}`. The ref target must belong to the same group (refs are not a side door into arbitrary org sessions); org scoping is enforced on every query.

Render line format: `[n42 w3f2/FAIL +2m] content` — note id (for `expand_note`), writer label (`coord` for the group root, `w<hex4>` for workers), age, and remaining TTL for claims.

## Lifecycle & Maintenance

A worker-process sweeper (`jobs/board_maintenance.py`, 5-minute cadence, started alongside the inbox-expire sweeper) runs:

- **Claim expiry**: lapsed claims flip to `expired` with a `seq` bump so deltas report it.
- **Purge clause 1**: all notes of groups whose root session has been terminal (`completed`/`failed`/`archived`) for > 7 days.
- **Purge clause 2**: `superseded`/`expired` rows older than 7 days regardless of root status (long-lived roots don't leak rows).
- **Purge clause 3**: notes whose `group_id` matches no session row (orphans).

## Configuration

All knobs under `SUROGATES_BOARD_*` (there is deliberately **no enable flag** — a fan-out has a board, definitionally):

| Env var | Default |
|---|---|
| `SUROGATES_BOARD_SNAPSHOT_WINDOW_TOKENS` | 600 |
| `SUROGATES_BOARD_DELTA_MAX_CHARS` | 1200 |
| `SUROGATES_BOARD_READ_TOOL_WINDOW_TOKENS` | 1500 |
| `SUROGATES_BOARD_CLAIM_TTL_SECONDS` | 300 |
| `SUROGATES_BOARD_MAX_ACTIVE_CLAIMS_PER_WRITER` | 2 |
| `SUROGATES_BOARD_MAX_NOTES_PER_GROUP` | 300 |
| `SUROGATES_BOARD_PURGE_AFTER_DAYS` | 7 |

The admission verifier uses the tenant's existing summary model — no new model configuration.

## API

`GET /v1/sessions/{session_id}/board` → `{group_id, notes: […], render}`; 404 when the session is not a group member.

## Relationship to Missions

A mission coordinator spawning tasks is a fan-out root, so a board forms automatically: mission workers coordinate, retries inherit verified knowledge from failed attempts, and the coordinator receives live deltas instead of waiting for `worker_complete` summaries. The judge, continuation loop, and mission state machine are untouched — a bad note can mislead a worker but cannot flip a mission verdict.
