# Chat Turn Feedback (Thumbs Up / Down)

Add thumbs-up / thumbs-down feedback to assistant turns in the
`agent-chat-react` SDK so users can rate the final answer of each
turn. The backend endpoint and the `USER_FEEDBACK` SSE event already
exist; this spec covers the SDK plumbing, UI, and adapter wire-up.

## Goals

- Let a user rate the final assistant answer of each turn with a
  thumbs-up or thumbs-down.
- Collect an optional free-form reason on thumbs-down (matching the
  existing expert-feedback flow).
- Persist the rating across reload by reducing the `user.feedback` SSE
  event into message state.
- Reuse the existing
  `POST /v1/sessions/{session_id}/events/{event_id}/feedback`
  endpoint, which already emits `USER_FEEDBACK` when the target is an
  `llm.response` event.

## Non-goals

- Backend changes — the endpoint and event are in place.
- Wiring `submitUserFeedback` into the surogate-ops studio's
  work-agent adapter. That is a follow-up PR; the studio picks up the
  feature automatically when its adapter implements the optional
  method.
- Aggregation surfaces. `hadResponseThumbsUp` /
  `hadResponseThumbsDown` on `Session` are already populated
  server-side from `USER_FEEDBACK` events.

## Architecture overview

The SDK already supports an analogous feature for expert tool calls
([`components/chat/tools/expert-tool.tsx`][expert-tool]). The new
feature mirrors that flow at the assistant-message level:

1. `AgentChatMessage` gains an `llmResponseEventId` (the latest
   `llm.response` event id for the turn) and a `userFeedback` field.
2. The reducer sets `llmResponseEventId` whenever an `llm.response`
   event lands on a message, and reduces the `user.feedback` SSE
   event into `userFeedback`.
3. A new `TurnFeedback` component renders thumbs buttons beneath the
   final text entry of each assistant turn. Thumbs-up posts
   immediately; thumbs-down opens an inline reason textarea (500-char
   cap, matching the backend).
4. The optional `submitUserFeedback` method on `AgentChatAdapter`
   posts to the same feedback endpoint used by expert feedback.

[expert-tool]: ../../../sdk/agent-chat-react/src/components/chat/tools/expert-tool.tsx

## Data model

### `types.ts`

Add two fields to `AgentChatMessage`:

```ts
export interface AgentChatMessage {
  // ...existing fields...
  llmResponseEventId?: number;
  userFeedback?: { rating: "up" | "down"; reason?: string };
}
```

Add `"user.feedback"` to the `AgentChatEventType` union.

Add an optional method to `AgentChatAdapter`:

```ts
submitUserFeedback?(input: {
  sessionId: string;
  llmResponseEventId: number;
  rating: "up" | "down";
  reason?: string;
}): Promise<{ eventId?: number; eventType?: string }>;
```

### Why a separate `llmResponseEventId`

The message's `id` (`evt-${eventId}`) is set when the message is first
created and may come from an `llm.delta` event rather than an
`llm.response`. In multi-step turns multiple `llm.response` events
update the same message in place. The feedback endpoint requires the
exact target event id of the assistant turn, so we track it
explicitly. The latest `llm.response` for the message wins.

## Reducer changes

[`runtime/reducer.ts`][reducer]:

[reducer]: ../../../sdk/agent-chat-react/src/runtime/reducer.ts

### `applyLlmResponse`

In every code path that creates or updates an assistant message
(`hadDeltas` reuse, tool-turn merge, new push, in-place update), set
`llmResponseEventId = event.eventId` on the message being written.

### `applyUserFeedback` (new)

Symmetric to `applyExpertFeedback`:

```ts
function applyUserFeedback(
  messages: AgentChatMessage[],
  data: Record<string, unknown>,
): AgentChatMessage[] {
  const targetEventId =
    typeof data.target_event_id === "number" ? data.target_event_id : undefined;
  if (targetEventId == null) return messages;
  const rating = data.rating === "down" ? "down" : "up";
  const reason = typeof data.reason === "string" ? data.reason : undefined;

  for (let i = messages.length - 1; i >= 0; i--) {
    const msg = messages[i];
    if (msg?.role !== "assistant") continue;
    if (msg.llmResponseEventId !== targetEventId) continue;
    if (
      msg.userFeedback?.rating === rating &&
      msg.userFeedback?.reason === reason
    ) return messages;
    const next = [...messages];
    next[i] = { ...msg, userFeedback: { rating, reason } };
    return next;
  }
  return messages;
}
```

### Dispatch

Register the new case in the main switch:

```ts
case "user.feedback":
  return withMessages(nextState, applyUserFeedback(nextState.messages, event.data));
```

## Adapter wire-up

### Standalone web app

[`surogates/web/src/api/feedback.ts`][feedback-api]: rename the HTTP
helper from `submitExpertFeedback` to `submitTurnFeedback` (it is
already endpoint-generic — the backend routes by event type). Update
the single caller.

[feedback-api]: ../../../web/src/api/feedback.ts

[`surogates/web/src/features/chat/surogates-web-chat-adapter.ts`][web-adapter]:
implement `submitUserFeedback`, calling `submitTurnFeedback` with the
LLM response event id.

[web-adapter]: ../../../web/src/features/chat/surogates-web-chat-adapter.ts

### Studio (out of scope)

`/work/surogate-ops/frontend/src/features/work/work-agent-chat-adapter.ts`
does not currently implement the optional `submitExpertFeedback`
method either. Wiring `submitUserFeedback` there is a follow-up PR.
Because the method is optional, the studio chat continues to compile
and render correctly without it — `canRate` is false and the thumbs
UI is hidden.

## UI

### New component: `TurnFeedback`

File: `sdk/agent-chat-react/src/components/chat/turn-feedback.tsx`

Structured like
[`expert-tool.tsx`](../../../sdk/agent-chat-react/src/components/chat/tools/expert-tool.tsx)
but smaller (~120 lines). Props: the assistant `ChatMessage` being
rated.

**Capability check** (rating allowed when all are true):
- `msg.llmResponseEventId !== undefined`
- `msg.status === "complete"`
- `sessionId !== null` (from adapter context)
- `adapter.submitUserFeedback !== undefined`

**Behavior**:
- `rating = msg.userFeedback?.rating ?? pending`
  (`pending` is local optimistic state; cleared on success or
  rejected error.)
- `alreadyRated = msg.userFeedback !== undefined`
- Thumbs-up click: `submit("up")` immediately.
- Thumbs-down click: opens an inline `<Textarea>` (autoFocus, 500
  char cap, Cmd+Enter submits, Esc cancels). On send: `submit("down",
  trimmed)`.
- After a rating exists, both buttons are disabled, the chosen icon
  stays highlighted, and the persisted reason (if any) renders as a
  muted truncated `· "…"` next to the icons.
- Error state: `"Failed to submit feedback"` inline.

The `ReasonForm` is duplicated from `expert-tool.tsx` rather than
shared in this pass. If a third surface adopts the pattern, extract
to a shared component then.

### Integration into `chat-thread.tsx`

The `TimelineEntry` `text` variant currently carries only
`content`/`isStreaming`. Extend it so the text entry knows which
message it belongs to and whether it is the final user-facing answer
of the turn:

```ts
| {
    kind: "text";
    key: string;
    content: string;
    isStreaming: boolean;
    msg: ChatMessageType;
    isFinalTurnText: boolean;
  }
```

In `messageToEntries`, `isFinalTurnText` is `true` only for an
`effectiveHasContent` block that has no following tool calls in its
own message — the model's user-facing answer.

`TextEntry` renders:

```tsx
<MessageResponse>{content}</MessageResponse>
{entry.isFinalTurnText && entry.msg.status === "complete" && (
  <TurnFeedback msg={entry.msg} />
)}
```

Placement: directly under the text bubble, inside the same
`TimelineContent` cell, so the thumbs hang off the same timeline node
as the answer.

## Testing

### Reducer unit tests
Extend [`runtime/reducer.test.ts`](../../../sdk/agent-chat-react/src/runtime/reducer.test.ts):
- `llm.response` sets `llmResponseEventId` on a freshly created
  assistant message.
- A later `llm.response` on the same message updates
  `llmResponseEventId` to the new event id.
- `user.feedback` with a matching `target_event_id` writes
  `userFeedback` onto the right message; non-matching is a no-op.
- Replaying the same `user.feedback` twice returns the same array
  reference (idempotency).

### Component test
New file: `sdk/agent-chat-react/src/components/chat/__tests__/turn-feedback.test.tsx`:
- Renders `null` when `canRate` is false (no `llmResponseEventId`,
  status streaming, or adapter method absent).
- Thumbs-up calls `adapter.submitUserFeedback` with `rating: "up"`
  and no reason.
- Thumbs-down opens the textarea; submitting calls the adapter with
  the trimmed reason.
- Already-rated state (msg has `userFeedback`) renders disabled
  buttons with the persisted rating highlighted and the reason
  visible.

### Adapter test
Extend `surogates/web/src/features/chat/__tests__/surogates-web-chat-adapter.test.ts`
(or sibling): `submitUserFeedback` POSTs to
`/v1/sessions/{id}/events/{eventId}/feedback` with the correct body
and rejects on non-2xx.

## Risks / open questions

- **Race between optimistic write and SSE replay.** The user clicks
  thumbs-up, we POST, the server emits a `user.feedback` event that
  arrives via SSE while the POST response is still in flight. The
  reducer handler is idempotent (compares rating + reason), so the
  double-write is harmless; `pending` is cleared on POST resolution.
- **History replay timing.** When the SDK reloads a session, events
  are replayed in id order. The `user.feedback` event has a larger
  id than its target `llm.response`, so by the time
  `applyUserFeedback` runs, the message's `llmResponseEventId` is
  already set. No special ordering needed.
- **Multi-tab / multi-user sessions.** A second viewer of the same
  session sees the rating land via SSE; behavior is identical to a
  reload mid-session.
