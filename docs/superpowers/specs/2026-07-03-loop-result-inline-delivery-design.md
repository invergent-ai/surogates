# Loop-result inline delivery (web/api) — design

**Date:** 2026-07-03
**Status:** Reviewed design, pending implementation plan

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

Current code confirms the two relevant mechanics:

- `SessionStore.emit_event()` publishes every committed event to
  `surogates:session:{session_id}`, so an event emitted on the parent session is
  immediately visible to the parent's SSE stream without adding it to the
  channel-delivery outbox.
- `_enqueue_channel_delivery()` already resolves `channel="scheduled"` runs to
  their parent channel and skips `web`; therefore channel parents keep their
  existing outbox behavior, and this design only adds a parent-event path for
  non-outbox parents.

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

The new event is intentionally **message-shaped for UI only**: renderers treat
it like an assistant reply, but the replay layer does not treat it as
`llm.response`.

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
  "duration_seconds": 123,
  "run_completed_at": "<run completion time, ISO8601>"
}
```

`content` is the run's full final assistant response, not the short inbox
excerpt. Use `extract_final_response(events, fallback="")` against the child
run's events, then emit only when the resulting content is non-empty after
trimming. The implementation may cap the stored string defensively, but the cap
must be substantially larger than the inbox excerpt because this event is the
primary delivery surface.

`scheduled_session_id` comes from `session.config["scheduled_session_id"]`.
`outcome` matches the current completion mapping: `"success"` for
`stop`/`done`/`complete`/`completed`, otherwise the raw completion `reason`.

### Integration point

In the loop-run completion path
(`surogates/harness/loop_artifact_completion.py::_complete_session`, adjacent
to the `INBOX_TASK_COMPLETE` emission):

1. Determine whether this run should surface inline (gating helper below).
2. If yes, read the child run's events and extract the final response.
3. If the final response is non-empty, emit `LOOP_RESULT` on the parent session.
4. Suppress the `INBOX_TASK_COMPLETE` inbox card for these runs (see below).

The emission is **best-effort**: any failure is logged and must never abort run
completion, status update, cursor advancement, or dynamic-loop finalization. An
empty/missing final response emits nothing.

Emit `loop.result` after `session.complete` for the child has been written and
before the optional inbox-card emission/status update. This ordering keeps the
child run's own terminal event durable first while still making the parent
inline result available as soon as completion is processed. The parent event id
is not used as the child's harness cursor; cursor advancement remains scoped to
the child session's events.

### Gating

Emit `loop.result` only when **all** hold:

- the run is a scheduled run (`session.channel == "scheduled"` and/or
  `session.config["scheduled_session_id"]` is present), and
- it has a `parent_id`, and
- the parent session's channel is **`web` or `api`**.

Slack/Telegram/Teams parents are skipped — they already receive each run through
the outbox (channel-delivery fix). Detached schedules/runs (no parent) are
skipped because there is no originating conversation to append to.

A small helper resolves the parent's channel (one session lookup by
`parent_id`) and returns the surfacing decision plus the parent session. Lookup
failure is a skip, not a hard error.

Recommended helper boundary:
`_resolve_loop_result_parent(self, session: Session) -> Session | None`.

Return a parent session only for web/api scheduled children; callers use the
returned parent both for `emit_event(parent.id, EventType.LOOP_RESULT, ...)` and
for deciding whether to suppress the inbox card.

### Inbox card suppression

Today every completion emits `INBOX_TASK_COMPLETE`, which for these runs becomes
the expiring card. For **scheduled runs with a web/api parent**, suppress that
emission even if `loop.result` extraction/emission fails — the inbox card is not
a reliable delivery path and creates noisy, expiring duplicates. Other sessions
(regular tasks, sub-agent work, channel runs, detached scheduled runs) are
unaffected.

Because `_complete_session` currently uses the inbox event id as the fallback
cursor target, suppression must also adjust cursor advancement:

- If `through_event_id` was supplied, keep using it.
- Else if the inbox card was emitted, use that inbox event id.
- Else use the child `SESSION_COMPLETE` event id.

### Context and wake safety

`loop.result` is a new type, so `_rebuild_messages`
(`surogates/harness/loop_context_replay.py`), which whitelists the event types
that feed the agent context, ignores it automatically — no bloat, no replay
change. The completion step does **not** re-enqueue the parent, so the parent
agent is never woken.

Do not add `loop.result` to `SessionStore._DELIVERABLE_EVENTS`. It is emitted on
web/api parents specifically so SSE/polling can see it; channel parents must
continue to use their existing `llm.response` outbox delivery from the child
run.

## Frontend design

Add `loop.result` to the live chat event types and render it in the conversation
thread as an **assistant-style message** with a small text affordance (for
example, `From loop · <time>`) and a click-through to the run session when the
host app has a route for session drill-in.

Primary live-chat surface:

- `sdk/agent-chat-react/src/types.ts`: add `"loop.result"` to
  `AgentChatEventType`.
- `sdk/agent-chat-react/src/runtime/events.ts`: add it to
  `AGENT_CHAT_LISTENED_EVENTS` so SSE and reconciliation polling both pass the
  event into the reducer.
- `sdk/agent-chat-react/src/runtime/reducer.ts`: add a `loop.result` case that
  appends a completed assistant message from `data.content`. The message should
  carry metadata such as `run_session_id`, `scheduled_session_id`, and
  `run_completed_at` without disturbing normal `llm.response` bookkeeping
  (`llmResponseEventId`, token usage, `isRunning`, turn summaries).
- `sdk/agent-chat-react/src/components/chat/chat-thread.tsx`: render the
  affordance on loop-result messages. Prefer a small metadata line inside the
  assistant bubble over a separate card.

Surogates web host:

- `web/src/types/session.ts`: add `"loop.result"` so raw session event typing
  does not reject it.
- `web/src/features/chat/surogates-web-chat-adapter.ts`: no special handling is
  needed; it opens the raw session SSE stream and polls raw events, while the
  shared SDK runtime owns event-type filtering.

Ops/admin session drill-in:

- `surogates/db/observability.sql`: add `'loop.result'` to `v_session_messages`
  so ops shows parent-loop outputs in the Thread tab.
- `surogate-ops/frontend/src/types/session.ts`: add
  `EventType.LOOP_RESULT = "loop.result"`.
- `surogate-ops/frontend/src/components/sessions/thread-tab.tsx` and
  `session-thread.tsx`: render `loop.result` as an assistant-like bubble that
  reads `data.content` directly.

Unknown event types already arrive through polling in some clients; reducers
and renderers must continue to ignore unhandled events rather than crash.

Website widget SDK:

- No first-pass change is required for `sdk/website-widget`. Its translator
  maps unknown Surogates events to AG-UI `CUSTOM`, and widget loops are not the
  target surface for parent conversation inline loop delivery. Add first-class
  handling later only if website-channel loops need user-visible inline
  scheduled results.

## Data flow (web/api loop, per run)

```
ticker fires → run child session executes prompt → final assistant message
  → completion hook:
       child session.complete is emitted
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
  skip; no parent → skip; missing/deleted parent → skip.
- Emission writes a `loop.result` on the **parent** with the full final content
  and correct metadata.
- Empty final response suppresses `loop.result` but does not fail completion.
- Inbox `task_complete` is suppressed for web/api scheduled runs and still
  emitted for other sessions, including detached scheduled runs.
- Cursor advancement uses `SESSION_COMPLETE` when the inbox event is suppressed
  and no explicit `through_event_id` was supplied.
- The parent is **not** re-enqueued by the surfacing step.
- Best-effort: a failure in the emission does not abort completion.

Replay:

- `_rebuild_messages` ignores `loop.result` (parent context byte-stable).
- `loop.result` is not included in `_DELIVERABLE_EVENTS`.

Frontend:

- `AGENT_CHAT_LISTENED_EVENTS` includes `loop.result`; SSE and polling both
  deliver it to the reducer.
- Reducer appends a completed assistant-style message with loop metadata and
  does not set `isRunning`.
- Thread renders `loop.result` as an assistant message with the loop affordance.
- Ops `v_session_messages` includes `loop.result` and both ops thread renderers
  show it as assistant-like output.
- Unknown event type does not crash the thread/reducer.

## Consistency note (accepted)

Channel loops post each run via the outbox (including intermediate narration);
web/api loops post one clean final result per run at completion. This asymmetry
is accepted — the web/api surface shows the run's final answer, which is the
useful unit.
