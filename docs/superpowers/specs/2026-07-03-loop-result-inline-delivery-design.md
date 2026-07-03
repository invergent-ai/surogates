# Loop-result inline delivery (web/api) — design

**Date:** 2026-07-03
**Status:** Approved design, pending implementation plan

## Problem

A `/loop` (recurring scheduled prompt) fires on an interval. Each firing runs
on a **separate child session** (`channel="scheduled"`) that executes the loop's
prompt and produces a final assistant message. That per-run answer never reaches
the originating conversation:

- Each run's output is mirrored to a `task_complete` **inbox card**, which the
  background inbox-expiry job removes unread. In production, web loop inbox
  cards are **8/8 expired**, service-account **6/6 expired** — users never see
  the results.
- The run's events live on the child session; the parent conversation's live
  stream (web SSE reads the *parent* session's events) never includes them.

For **channel** parents (Slack/Telegram/Teams) this is already solved: the
channel-delivery fix resolves a scheduled run's deliverable events to the
parent's channel and pushes each run into the channel conversation via the
outbox. The gap remains for **web** and **api** parents, which have no outbox
path — they consume results by streaming/polling `GET /sessions/{id}/events`,
and that endpoint is per-session.

## Goal

When a loop run whose origin is a **web** or **api** session completes, surface
that run's final answer **inline in the originating conversation**, matching the
Slack behavior (every run is surfaced). Do not wake the parent agent, do not
pollute its LLM context, and remove the now-redundant expiring inbox card for
these runs.

Non-goals: changing channel (Slack/Telegram/Teams) delivery; per-run
diffing/filtering (every run is surfaced); a separate loop-runs UI panel.

## Approach

Emit a new **`loop.result`** event into the **parent** session's event log at
run completion. Web SSE streams it so it renders inline; api clients see it by
polling the parent session's events. Because it is a new event type, the
harness's history reconstruction ignores it — the parent agent's context is
untouched and the parent is never re-enqueued.

Considered and rejected:

- **Reuse `LLM_RESPONSE` on the parent (tagged).** No frontend change, but
  `_rebuild_messages` consumes `LLM_RESPONSE`, so every loop output would flood
  the parent agent's context. Avoiding that requires a skip-filter inside the
  byte-stable, prefix-cache-stable replay path — a riskier change to a sensitive
  code path. Rejected.
- **Reuse `notify_parent_on_completion` minus the wake.** Emits
  `WORKER_COMPLETE`, which is sub-agent coordinator plumbing, not a user-facing
  message; the thread renderer would not show it as a reply. Rejected.

## Backend design

### New event type

`EventType.LOOP_RESULT = "loop.result"`, emitted on the **parent** session.

Payload:

```json
{
  "run_session_id": "<child run session uuid>",
  "scheduled_session_id": "<schedule uuid>",
  "content": "<full final assistant message of this run>",
  "outcome": "success | <reason>",
  "created_at": "<run completion time, ISO8601>"
}
```

`content` is the run's full final assistant response (via `extract_final_response`,
generously capped), not the short inbox excerpt.

### Integration point

In the loop-run completion path (`surogates/harness/loop_artifact_completion.py`,
adjacent to the `INBOX_TASK_COMPLETE` emission):

1. Determine whether this run should surface inline (gating helper below).
2. If yes, emit `LOOP_RESULT` on the parent session with the run's final
   response.
3. Suppress the `INBOX_TASK_COMPLETE` inbox card for these runs (see below).

The emission is **best-effort**: any failure is logged and must never abort run
completion or the schedule's `mark_run_created`. An empty/missing final response
emits nothing.

### Gating

Emit `loop.result` only when **all** hold:

- the run is a scheduled run (`session.channel == "scheduled"`), and
- it has a `parent_id`, and
- the parent session's channel is **`web` or `api`**.

Slack/Telegram/Teams parents are skipped — they already receive each run through
the outbox (channel-delivery fix). Detached runs (no parent) are skipped.

A small helper resolves the parent's channel (one session lookup by
`parent_id`) and returns the surfacing decision.

### Inbox card suppression

Today every completion emits `INBOX_TASK_COMPLETE`, which for these runs becomes
the expiring card. For **scheduled runs with a web/api parent**, suppress that
emission — the result is now surfaced inline, so the card is redundant. Other
sessions (regular tasks, sub-agent work, channel runs) are unaffected.

### Context and wake safety

`loop.result` is a new type, so `_rebuild_messages`
(`surogates/harness/loop_context_replay.py`), which whitelists the event types
that feed the agent context, ignores it automatically — no bloat, no replay
change. The completion step does **not** re-enqueue the parent, so the parent
agent is never woken.

## Frontend design

Add `loop.result` to the `EventType` enum in each web app and render it in the
conversation thread as an **assistant-style message** with a small affordance
(e.g. `🔁 from your loop · <time>`) and a click-through to the run session.

- ops frontend: `frontend/src/types/session.ts` +
  `frontend/src/components/sessions/session-thread.tsx`
- surogates web: `web/src/types/session.ts` + its chat thread renderer

Both apps consume the same session events; an unknown event type must never
break the thread. The implementation plan will confirm which app hosts the
web loop conversations and prioritize accordingly.

## Data flow (web/api loop, per run)

```
ticker fires → run child session executes prompt → final assistant message
  → completion hook:
       emit loop.result on PARENT session  (content = run's final response)
       suppress task_complete inbox item for web/api scheduled runs
  → parent SSE (GET /sessions/{parent}/events) streams loop.result
  → web thread renders it inline as an assistant message
  → api client sees it by polling the parent session's events
_rebuild_messages ignores loop.result → parent agent context unchanged, parent not woken
```

## Testing

Backend (unit):

- Gating: web parent → emit; api parent → emit; slack/telegram/teams parent →
  skip; no parent → skip.
- Emission writes a `loop.result` on the **parent** with the full final content
  and correct metadata.
- Inbox `task_complete` is suppressed for web/api scheduled runs and still
  emitted for other sessions.
- The parent is **not** re-enqueued by the surfacing step.
- Best-effort: a failure in the emission does not abort completion.

Replay:

- `_rebuild_messages` ignores `loop.result` (parent context byte-stable).

Frontend:

- Thread renders `loop.result` as an assistant message with the loop affordance.
- Unknown event type does not crash the thread.

## Consistency note (accepted)

Channel loops post each run via the outbox (including intermediate narration);
web/api loops post one clean final result per run at completion. This asymmetry
is accepted — the web/api surface shows the run's final answer, which is the
useful unit.
