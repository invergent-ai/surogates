# Steer the agent mid-turn without stopping it

**Date:** 2026-06-20
**Status:** Design approved, ready for planning
**Component:** `surogates` harness (`surogates/harness/loop.py`)

## Problem

When a user sends a message while the agent is still generating a multi-step
response, the harness throws the in-progress work away. The staleness guard
`_should_abort_before_llm_response` (`surogates/harness/loop.py:599-636`) detects
that a newer `user.message` was appended after the current iteration's
`llm.request`, **drops the buffered response**, and re-wakes the session. The
user experiences this as the agent stopping and restarting.

We want the Claude Code behavior: the new message is **queued and injected at the
next iteration boundary**. The current assistant message and its tool results
finish cleanly, then the queued text is appended as a new user turn and the loop
continues in the same wake. Nothing is discarded; the model just gets the new
input and adapts.

## Decisions (settled during brainstorming)

- **Injection point:** the next iteration boundary. The current LLM response and
  its in-flight tool calls finish, then the queued message is appended before the
  next `llm.request`. Nothing is discarded.
- **Coalescing:** all messages that arrive in one window are appended together,
  in order, as a single user turn → one `llm.request`.
- **Stop is preserved:** the explicit pause/Stop path keeps today's immediate
  hard-cancel of in-flight work. Only ordinary follow-up messages get the new
  queue-and-inject behavior.

## Why Approach B (replay re-sequence), not Approach A (in-band delivery)

The API writes the `user.message` to the durable event log the instant it
arrives, at whatever event id that happens to be. If that id lands **between** an
`assistant(tool_calls)` event and its `tool.result`, a later replay reconstructs
`assistant → user → tool`, which violates the tool-call/tool-result adjacency the
LLM APIs require — a hard error. So we cannot simply "stop dropping"; we must
control where the steer message sits in the reconstructed history.

- **Approach A — in-band delivery:** the API hands the message to the running
  harness over the existing Redis interrupt channel; the harness emits the
  `user.message` event itself at the boundary (born in the right place, no replay
  fix needed). Rejected: requires a durable "pending steer" record + dedup +
  fallback for the crash / wrong-worker cases. New infrastructure.
- **Approach B — replay re-sequence (chosen):** keep `send_message` exactly as it
  is (durable write + enqueue/signal). Fix ordering entirely inside the harness:
  the boundary injector appends at the right place live, and the rebuild applies a
  deterministic re-sequencing rule so replay produces the same order. No new
  storage, no new delivery path, no dedup, fully durable, all logic in one module.

## Components

Everything lives in the harness. `send_message`, the Redis interrupt channel, the
orchestrator, and the DB schema are **untouched**.

| Component | Location | Change |
|---|---|---|
| Iteration boundary injector | `loop.py` `_run_loop` | At each iteration top, pull new `user.message` events past a cursor, coalesce, append as one user turn, advance the cursor |
| Staleness guard | `loop.py` `_should_abort_before_llm_response` | Abort **only** on a real interrupt (Stop/pause/lease-loss); no longer drop on "newer user.message" |
| Completion-branch drain | `loop.py` `_run_loop`, the `if not tool_calls_raw:` branch | Emit the final response, then check the cursor — if a follow-up is pending, inject + `continue` instead of completing |
| Replay re-sequencer | `_rebuild_messages` | Defer a `user.message` that landed mid-iteration to that iteration's close, so rebuild order == live order |

## The incorporation cursor

The whole anti-double-incorporation mechanism is the durable log plus a single
integer cursor — no new state or table.

- **Init:** at wake start, `cursor = max(id of user.message events already present
  in the replayed history)`, computed from `all_events` (already passed into
  `_run_loop`).
- **Advance:** each time the injector folds messages in,
  `cursor = max(incorporated ids)`.
- **Query:** at each boundary, `get_events(session, after=cursor,
  types=[USER_MESSAGE])`, filtered to **non-synthetic** messages only. Reuse the
  same real-vs-synthetic filter `_has_stranded_user_message` uses — mission
  continuations and harness nudges are not steer messages and keep their existing
  handling.

Note the distinction from the existing mid-loop synthetic injections
(length-continuation `loop.py:1911-1927`, deep-research delegate nudge): those
append **ephemeral** user messages that are never logged and are re-derived on
every wake. Steer messages are **durable** (the API already wrote them); the
injector only appends them to the in-memory `messages` list and advances the
cursor — it does **not** emit a new event.

## Data flow (the steer case)

```
[live wake, iteration 2 running]
  llm.response (call read_file) emitted
  read_file runs → tool.result emitted
  └─ ⟵ user sends "actually, also check Z"  → API writes user.message to log (durable)

[iteration 3 boundary]
  1. interrupt check        → no Stop pending, continue
  2. injector: events after cursor? → yes: ["...check Z"]  (non-synthetic)
  3. append to messages as one user turn; advance cursor
  4. llm.request → model sees "check Z" and adapts → keeps going
```

No abort, no re-wake — one continuous wake. The interrupt check runs **before**
the injector each iteration, so an explicit Stop always takes precedence.

In-memory order is correct by construction:

```
assistant("check Y" + read_file call)
tool.result(read_file)          ← tool call properly closed
user("also check Z")            ← injected at the boundary, AFTER the tool result
llm.request                     ← next iteration
```

## Replay re-sequencing rule (Edit 3)

The log still has the steer message physically between the tool call and tool
result. On rebuild, apply:

> A `user.message` that arrived while an iteration was still "open" — between that
> iteration's `llm.request` and its tool results (or its `llm.response` if the
> iteration had no tool calls) — is deferred to that iteration's close.

`llm.request` / `llm.response` events carry `turn_id` and `iteration_index`, so
the rebuild can detect a mid-iteration message and slide it down to the boundary,
producing the exact order the live run produced. Live path and replay path always
agree, and tool-call/tool-result adjacency is never broken.

Implementation shape: while folding events in id order, track open/closed
iteration state. On `llm.request`, the iteration opens; buffer any `user.message`
seen while open; flush the buffer at iteration close (last `tool.result` for that
iteration, or the `llm.response` when there are no tool calls). A `user.message`
seen while no iteration is open keeps its natural placement.

## Edge cases

- **Stop / Pause still wins.** The interrupt check precedes the injector each
  iteration, and the guard still aborts on `_check_interrupt()`. Hard-cancel is
  fully preserved.
- **Message lands exactly as the turn finishes (no tool calls).** The final
  `llm.response` is still emitted (the user gets the answer to the prior
  question), then the drain check runs: pending follow-up → inject + `continue`;
  nothing pending → complete normally. "Complete + re-wake" becomes "keep going"
  only when there is actually something to continue for.
- **Message lands after the final boundary / during completion.** Falls through to
  the existing `_rewake_pending` + stranded-message safety net → a fresh wake
  picks it up. Unchanged.
- **Budget exhausted with a pending follow-up.** The existing final-summary path
  runs; the durable message sits past the cursor and is handled on the next wake.
  Unchanged.

## Cost & non-goals

- **Per-iteration query:** one indexed `(session_id, type)` lookup per boundary —
  negligible. Could later be gated behind an orchestrator "pending message" signal
  if it ever matters; not optimized now (YAGNI).
- **Frontend "queued" affordance** (greying a message until it is incorporated):
  the `user.message` event already broadcasts, so this is optional later polish —
  out of scope here.
- **No change** to `send_message`, the Redis interrupt channel, the orchestrator,
  or any DB schema.

## Test plan

1. **Re-sequencer unit tests** (pure function over an event list): a
   `user.message` injected (a) mid-tool-call, (b) mid-stream, (c) at a clean
   boundary → assert rebuilt messages are correctly ordered and every
   `assistant(tool_calls)` is immediately followed by its `tool.result`.
2. **Boundary injection:** a `user.message` mid-wake → the in-flight response is
   **not** dropped, the message is folded in at the next iteration, and exactly
   one continuous wake runs (no abort event).
3. **Coalescing:** two messages in one window → appended together, in order, one
   `llm.request`.
4. **Cursor:** the same message is never incorporated twice across iterations.
5. **Stop precedence:** explicit pause mid-wake still aborts immediately
   (regression guard).
6. **Completion drain:** a follow-up arriving as the final answer lands → the
   final response is emitted **and** the follow-up continues the same wake.

## Open item to verify during planning

The design assumes `_rebuild_messages` reconstructs the message list by iterating
events in id order, which makes Edit 3 a localized change there. If the rebuild
works differently, Edit 3 attaches to a different spot — the design holds
regardless, only the integration point moves.
