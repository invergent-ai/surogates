# Chat Turn Feedback (Thumbs Up / Down) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let users rate the final assistant answer of each turn with thumbs-up / thumbs-down in the `agent-chat-react` SDK, persisting the rating across reload via the existing `USER_FEEDBACK` SSE event.

**Architecture:** Mirror the existing expert-feedback flow at the assistant-message level. `AgentChatMessage` gains an `llmResponseEventId` (tracked in the reducer from `llm.response` events) and a `userFeedback` field (reduced from `user.feedback` SSE events with `source: "user"`). A new `TurnFeedback` component renders thumbs buttons under each final text entry; thumbs-down opens an inline reason textarea. An optional `submitUserFeedback` adapter method POSTs to the existing feedback endpoint.

**Tech Stack:** TypeScript, React 19, Vitest, the existing `agent-chat-react` SDK + `surogates/web` Vite app. Backend (Python FastAPI) is untouched.

**Spec:** [`docs/superpowers/specs/2026-05-21-chat-turn-feedback-design.md`](../specs/2026-05-21-chat-turn-feedback-design.md)

---

## Progress

- [x] Task 1 — Message fields & adapter method
- [x] Task 2 — Reducer: track `llmResponseEventId` on assistant messages
- [x] Task 3 — Reducer: handle `user.feedback` events
- [x] Task 4 — `TurnFeedback` component (TDD)
- [x] Task 5 — `chat-thread` integration
- [x] Task 6 — Web adapter wire-up
- [~] Task 7 — End-to-end smoke verification (Steps 1-3 done; Step 4 needs a live backend)

---

## File Structure

**SDK — `surogates/sdk/agent-chat-react/`:**
- Modify `src/types.ts` — add `llmResponseEventId` + `userFeedback` to `AgentChatMessage`; add `"user.feedback"` to `AgentChatEventType`; add optional `submitUserFeedback` to `AgentChatAdapter`.
- Modify `src/runtime/events.ts` — add `"user.feedback"` to `AGENT_CHAT_LISTENED_EVENTS`.
- Modify `src/runtime/reducer.ts` — set `llmResponseEventId` in `applyLlmResponse`; add `applyUserFeedback`; dispatch the new case.
- Create `src/components/chat/turn-feedback.tsx` — new component, ~130 lines, mirrors `expert-tool.tsx` but smaller (no expert-specific args/result parsing).
- Modify `src/components/chat/chat-thread.tsx` — extend the `text` `TimelineEntry` variant with `msg` + optional `isFinalTurnText`; mark the final text entry after assistant-group flattening; render `<TurnFeedback />` in `TextEntry`.
- Modify `tests/reducer.test.ts` — add tests for `llmResponseEventId` tracking and `user.feedback` reduction.
- Create `tests/turn-feedback.test.tsx` — new component-level tests.

**Web app — `surogates/web/`:**
- Modify `src/api/feedback.ts` — rename `submitExpertFeedback` → `submitTurnFeedback` (the helper is endpoint-generic).
- Modify `src/features/chat/surogates-web-chat-adapter.ts` — update the rename's caller; add `submitUserFeedback` method.

Each file has one responsibility; the SDK split (types ↔ runtime ↔ components ↔ tests) follows the existing convention.

---

## Task 1: Wire the data model — message fields & adapter method

**Files:**
- Modify: `surogates/sdk/agent-chat-react/src/types.ts` — `AgentChatMessage` interface (add two optional fields) and `AgentChatAdapter` interface (add optional `submitUserFeedback?`)

Foundation task: no behavior changes yet. The `AgentChatEventType` union and `AGENT_CHAT_LISTENED_EVENTS` array stay untouched here — they move to Task 3, paired with the reducer dispatch case, because adding `"user.feedback"` to the union would break the reducer's exhaustive switch until the case lands. Verified by `tsc`.

- [x] **Step 1: Add two optional fields to `AgentChatMessage`**

In `src/types.ts`, add `llmResponseEventId?: number` and `userFeedback?: { rating: "up" | "down"; reason?: string }` to the `AgentChatMessage` interface.

- [x] **Step 2: Add the optional `submitUserFeedback` adapter method**

In the `AgentChatAdapter` interface, immediately after `submitExpertFeedback?`, add:

```ts
  submitUserFeedback?(input: {
    sessionId: string;
    llmResponseEventId: number;
    rating: AgentChatExpertFeedbackRating;
    reason?: string;
  }): Promise<{ eventId?: number; eventType?: string }>;
```

(The existing `AgentChatExpertFeedbackRating` alias is just `"up" | "down"` — reuse it.)

- [x] **Step 3: Run the typecheck**

```bash
cd /work/surogates/sdk/agent-chat-react && npx tsc --noEmit
```
Expected: exit 0, no output.

- [x] **Step 4: Commit**

```bash
cd /work/surogates && git add sdk/agent-chat-react/src/types.ts && git commit -m "feat(chat-sdk): add user-feedback fields and adapter method"
```

---

## Task 2: Reducer — track `llmResponseEventId` on assistant messages

**Files:**
- Modify: `surogates/sdk/agent-chat-react/src/runtime/reducer.ts:418-510` (`applyLlmResponse`)
- Test: `surogates/sdk/agent-chat-react/tests/reducer.test.ts` (append two `it` blocks inside the existing `describe("applyAgentChatEvent", …)`)

The four branches of `applyLlmResponse` create or update an assistant message. Each needs to set `llmResponseEventId = event.eventId` on the message it writes.

- [x] **Step 1: Write the failing test for fresh-message branch**

Append inside `describe("applyAgentChatEvent", …)` in `tests/reducer.test.ts`:

```ts
  it("stores llmResponseEventId on a freshly created assistant message", () => {
    const next = applyAgentChatEvent(createInitialAgentChatState(), {
      type: "llm.response",
      eventId: 17,
      data: { message: { content: "hi there" } },
    });

    expect(next.messages).toHaveLength(1);
    expect(next.messages[0]?.role).toBe("assistant");
    expect(next.messages[0]?.llmResponseEventId).toBe(17);
  });
```

- [x] **Step 2: Run the test to verify it fails**

```bash
cd /work/surogates/sdk/agent-chat-react && npx vitest run tests/reducer.test.ts -t "stores llmResponseEventId on a freshly created"
```
Expected: FAIL — `llmResponseEventId` is `undefined`.

- [x] **Step 3: Write the failing test for the multi-response update branch**

Append the second test:

```ts
  it("updates llmResponseEventId when a second llm.response lands on the same message", () => {
    const afterDelta = applyAgentChatEvent(createInitialAgentChatState(), {
      type: "llm.delta",
      eventId: 10,
      data: { content: "partial" },
    });
    const afterFirst = applyAgentChatEvent(afterDelta, {
      type: "llm.response",
      eventId: 11,
      data: { message: { content: "partial", tool_calls: [{ id: "tc-1" }] } },
    });
    const afterSecond = applyAgentChatEvent(afterFirst, {
      type: "llm.response",
      eventId: 25,
      data: { message: { content: "final answer" } },
    });

    expect(afterSecond.messages).toHaveLength(1);
    expect(afterSecond.messages[0]?.llmResponseEventId).toBe(25);
  });
```

- [x] **Step 4: Run both tests; both should fail**

```bash
cd /work/surogates/sdk/agent-chat-react && npx vitest run tests/reducer.test.ts -t "llmResponseEventId"
```
Expected: both new tests FAIL.

- [x] **Step 5: Implement — set `llmResponseEventId` in every branch of `applyLlmResponse`**

In `src/runtime/reducer.ts`, modify `applyLlmResponse` (lines 418–510). Each of the four mutation sites currently builds a new message object — add `llmResponseEventId: event.eventId` to every one. The full updated function body's mutation section becomes:

```ts
  if (state.hadDeltas && idx >= 0 && !hasUserAfter) {
    const current = messages[idx]!;
    if (hasToolCalls) {
      messages[idx] = {
        ...current,
        reasoning: (current.reasoning ?? "") + current.content,
        content: "",
        status: "streaming",
        llmResponseEventId: event.eventId,
      };
    } else {
      messages[idx] = {
        ...current,
        status: "complete",
        llmResponseEventId: event.eventId,
      };
    }
  } else if (matchesExistingToolTurn && idx >= 0) {
    const current = messages[idx]!;
    messages[idx] = {
      ...current,
      reasoning: responseContent
        ? appendText(current.reasoning, responseContent)
        : current.reasoning,
      status: "streaming",
      llmResponseEventId: event.eventId,
    };
  } else if (prevHasTools || !prevAssistant || hasUserAfter) {
    messages.push({
      id: `evt-${event.eventId}`,
      role: "assistant",
      content: hasToolCalls ? "" : responseContent,
      reasoning: hasToolCalls && responseContent ? responseContent : undefined,
      createdAt: new Date(),
      status: hasToolCalls ? "streaming" : "complete",
      llmResponseEventId: event.eventId,
    });
  } else {
    const current = messages[idx]!;
    if (hasToolCalls && responseContent) {
      messages[idx] = {
        ...current,
        reasoning: (current.reasoning ?? "") + responseContent,
        status: "streaming",
        llmResponseEventId: event.eventId,
      };
    } else {
      messages[idx] = {
        ...current,
        content: responseContent || current.content,
        status: hasToolCalls ? "streaming" : "complete",
        llmResponseEventId: event.eventId,
      };
    }
  }
```

- [x] **Step 6: Run both tests; both should pass; full suite should still pass**

```bash
cd /work/surogates/sdk/agent-chat-react && npx vitest run tests/reducer.test.ts
```
Expected: all reducer tests PASS.

- [x] **Step 7: Commit**

```bash
cd /work/surogates && git add sdk/agent-chat-react/src/runtime/reducer.ts sdk/agent-chat-react/tests/reducer.test.ts && git commit -m "feat(chat-sdk): track llmResponseEventId per assistant turn"
```

---

## Task 3: Reducer — handle `user.feedback` events

**Files:**
- Modify: `surogates/sdk/agent-chat-react/src/types.ts` — add `"user.feedback"` to `AgentChatEventType` (moved here from Task 1 so it pairs with the dispatch case in one commit)
- Modify: `surogates/sdk/agent-chat-react/src/runtime/events.ts` — add `"user.feedback"` to `AGENT_CHAT_LISTENED_EVENTS`
- Modify: `surogates/sdk/agent-chat-react/src/runtime/reducer.ts` (add `applyUserFeedback` helper near `applyExpertFeedback`; add `case "user.feedback":` in the main switch)
- Test: `surogates/sdk/agent-chat-react/tests/reducer.test.ts` (three more `it` blocks)

`applyUserFeedback` is symmetric to `applyExpertFeedback` but matches on `msg.llmResponseEventId` instead of a tool call's `expertResultEventId`, and ignores non-interactive sources (judge feedback uses the same event type with `source: "judge"`).

- [x] **Step 1: Write the failing test — user-source feedback writes onto the right message**

Append:

```ts
  it("applies user.feedback (source=user) to the assistant message with matching llmResponseEventId", () => {
    const seeded = applyAgentChatEvent(createInitialAgentChatState(), {
      type: "llm.response",
      eventId: 50,
      data: { message: { content: "answer" } },
    });

    const next = applyAgentChatEvent(seeded, {
      type: "user.feedback",
      eventId: 51,
      data: {
        target_event_id: 50,
        rating: "down",
        source: "user",
        reason: "missed a column",
      },
    });

    expect(next.messages).toHaveLength(1);
    expect(next.messages[0]?.userFeedback).toEqual({
      rating: "down",
      reason: "missed a column",
    });
  });
```

- [x] **Step 2: Write the failing test — judge-source feedback is ignored**

```ts
  it("ignores user.feedback events emitted by judges (source != 'user')", () => {
    const seeded = applyAgentChatEvent(createInitialAgentChatState(), {
      type: "llm.response",
      eventId: 60,
      data: { message: { content: "answer" } },
    });

    const next = applyAgentChatEvent(seeded, {
      type: "user.feedback",
      eventId: 61,
      data: {
        target_event_id: 60,
        rating: "up",
        source: "judge",
      },
    });

    expect(next.messages[0]?.userFeedback).toBeUndefined();
  });
```

- [x] **Step 3: Write the failing test — replay is idempotent (same array reference)**

```ts
  it("returns the same messages reference when user.feedback replays unchanged", () => {
    const seeded = applyAgentChatEvent(createInitialAgentChatState(), {
      type: "llm.response",
      eventId: 70,
      data: { message: { content: "answer" } },
    });
    const once = applyAgentChatEvent(seeded, {
      type: "user.feedback",
      eventId: 71,
      data: { target_event_id: 70, rating: "up", source: "user" },
    });
    const twice = applyAgentChatEvent(once, {
      type: "user.feedback",
      eventId: 71,
      data: { target_event_id: 70, rating: "up", source: "user" },
    });

    expect(twice.messages).toBe(once.messages);
  });
```

- [x] **Step 4: Add `"user.feedback"` to the `AgentChatEventType` union**

In `src/types.ts`, add `"user.feedback"` immediately after `"expert.override"`:

```ts
  | "expert.result"
  | "expert.endorse"
  | "expert.override"
  | "user.feedback"
```

After this change the reducer's exhaustive switch will fail to typecheck until Step 6 lands the dispatch case. That's why the three steps (union, listened events, dispatch) are atomic.

- [x] **Step 5: Add `"user.feedback"` to `AGENT_CHAT_LISTENED_EVENTS`**

In `src/runtime/events.ts`, insert `"user.feedback",` immediately after `"expert.override",`. The relevant slice becomes:

```ts
  "expert.result",
  "expert.endorse",
  "expert.override",
  "user.feedback",
```

Without this the SSE stream drops the event before it reaches the reducer; the type union alone is not enough.

- [x] **Step 6: Run the failing tests to confirm typecheck blocks them**

```bash
cd /work/surogates/sdk/agent-chat-react && npx vitest run tests/reducer.test.ts -t "user.feedback"
```
Expected: tests FAIL (vitest reports a typecheck or runtime mismatch because the reducer still lacks the `case "user.feedback"`).

- [x] **Step 7: Add `applyUserFeedback` to reducer**

Insert the new function in `src/runtime/reducer.ts` immediately after `applyExpertFeedback` (around line 770):

```ts
function applyUserFeedback(
  messages: AgentChatMessage[],
  data: Record<string, unknown>,
): AgentChatMessage[] {
  const source = typeof data.source === "string" ? data.source : "user";
  if (source !== "user") return messages;

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
    ) {
      return messages;
    }
    const next = [...messages];
    next[i] = { ...msg, userFeedback: { rating, reason } };
    return next;
  }
  return messages;
}
```

- [x] **Step 8: Dispatch `user.feedback`**

In the main switch (immediately after the `expert.endorse` / `expert.override` case), insert:

```ts
    case "user.feedback":
      return withMessages(
        nextState,
        applyUserFeedback(nextState.messages, event.data),
      );
```

- [x] **Step 9: Run the reducer suite and typecheck**

```bash
cd /work/surogates/sdk/agent-chat-react && npx tsc --noEmit && npx vitest run tests/reducer.test.ts
```
Expected: typecheck clean, all reducer tests PASS.

- [x] **Step 10: Commit**

```bash
cd /work/surogates && git add sdk/agent-chat-react/src/types.ts sdk/agent-chat-react/src/runtime/events.ts sdk/agent-chat-react/src/runtime/reducer.ts sdk/agent-chat-react/tests/reducer.test.ts && git commit -m "feat(chat-sdk): reduce user.feedback SSE events into message state"
```

---

## Task 4: `TurnFeedback` component (TDD)

**Files:**
- Create: `surogates/sdk/agent-chat-react/src/components/chat/turn-feedback.tsx`
- Test: `surogates/sdk/agent-chat-react/tests/turn-feedback.test.tsx`

Component mirrors `expert-tool.tsx`'s feedback affordance but is smaller — no tool-args parsing, no expanded/collapsed body, just the buttons + optional reason form. The `ReasonForm` is duplicated locally rather than extracted; the spec notes that a third caller would justify lifting it out.

- [x] **Step 1: Write the failing test scaffold**

Create `tests/turn-feedback.test.tsx`:

```tsx
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AgentChatAdapterProvider } from "../src/adapter-context";
import { TurnFeedback } from "../src/components/chat/turn-feedback";
import type { AgentChatAdapter, AgentChatMessage } from "../src/types";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT =
  true;

let root: Root | null = null;
let container: HTMLDivElement | null = null;

afterEach(() => {
  if (root) act(() => root?.unmount());
  root = null;
  container?.remove();
  container = null;
});

function render(
  msg: AgentChatMessage,
  overrides: Partial<AgentChatAdapter> = {},
  sessionId: string | null = "sess-1",
): HTMLDivElement {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  const adapter = { submitUserFeedback: vi.fn(), ...overrides } as unknown as AgentChatAdapter;
  act(() => {
    root?.render(
      <AgentChatAdapterProvider value={{ adapter, sessionId }}>
        <TurnFeedback msg={msg} />
      </AgentChatAdapterProvider>,
    );
  });
  return container;
}

function assistant(overrides: Partial<AgentChatMessage> = {}): AgentChatMessage {
  return {
    id: "evt-50",
    role: "assistant",
    content: "answer",
    createdAt: new Date("2026-01-01T00:00:00Z"),
    status: "complete",
    llmResponseEventId: 50,
    ...overrides,
  };
}

describe("TurnFeedback", () => {
  it("renders nothing when llmResponseEventId is missing", () => {
    const node = render(assistant({ llmResponseEventId: undefined }));
    expect(node.querySelector('button[aria-label="Good response"]')).toBeNull();
  });

  it("renders nothing when status is streaming", () => {
    const node = render(assistant({ status: "streaming" }));
    expect(node.querySelector('button[aria-label="Good response"]')).toBeNull();
  });

  it("renders nothing when the adapter has no submitUserFeedback method", () => {
    const node = render(assistant(), { submitUserFeedback: undefined });
    expect(node.querySelector('button[aria-label="Good response"]')).toBeNull();
  });

  it("posts rating='up' immediately on thumbs-up click", async () => {
    const submit = vi.fn().mockResolvedValue({});
    const node = render(assistant(), { submitUserFeedback: submit });
    const up = node.querySelector('button[aria-label="Good response"]') as HTMLButtonElement;
    expect(up).not.toBeNull();
    await act(async () => {
      up.click();
    });
    expect(submit).toHaveBeenCalledWith({
      sessionId: "sess-1",
      llmResponseEventId: 50,
      rating: "up",
    });
  });

  it("opens the reason form on thumbs-down and submits the trimmed reason", async () => {
    const submit = vi.fn().mockResolvedValue({});
    const node = render(assistant(), { submitUserFeedback: submit });
    const down = node.querySelector('button[aria-label="Poor response"]') as HTMLButtonElement;
    await act(async () => {
      down.click();
    });
    const textarea = node.querySelector("textarea") as HTMLTextAreaElement;
    expect(textarea).not.toBeNull();
    await act(async () => {
      textarea.value = "  wrong column  ";
      textarea.dispatchEvent(new Event("input", { bubbles: true }));
    });
    const send = Array.from(node.querySelectorAll("button")).find(
      (b) => b.textContent?.includes("Send feedback"),
    ) as HTMLButtonElement;
    await act(async () => {
      send.click();
    });
    expect(submit).toHaveBeenCalledWith({
      sessionId: "sess-1",
      llmResponseEventId: 50,
      rating: "down",
      reason: "wrong column",
    });
  });

  it("renders the persisted reason and disabled buttons when already rated", () => {
    const node = render(
      assistant({ userFeedback: { rating: "down", reason: "off by one" } }),
    );
    const up = node.querySelector('button[aria-label="Good response"]') as HTMLButtonElement;
    const down = node.querySelector('button[aria-label="Poor response"]') as HTMLButtonElement;
    expect(up.disabled).toBe(true);
    expect(down.disabled).toBe(true);
    expect(node.textContent).toContain("off by one");
  });
});
```

- [x] **Step 2: Run the tests — every test should fail**

```bash
cd /work/surogates/sdk/agent-chat-react && npx vitest run tests/turn-feedback.test.tsx
```
Expected: 6 FAIL (module not found).

- [x] **Step 3: Implement `TurnFeedback`**

Create `src/components/chat/turn-feedback.tsx`:

```tsx
// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Renders thumbs-up / thumbs-down feedback under an assistant message's
// final text answer.  Thumbs-up posts immediately; thumbs-down opens an
// inline comment form so the user can record why the response was
// unsatisfactory.  Submission hits
// POST /v1/sessions/{id}/events/{event_id}/feedback (where event_id is
// the id of the llm.response turn being rated) which emits
// USER_FEEDBACK into the event log.

import { useState } from "react";
import { ThumbsDownIcon, ThumbsUpIcon } from "lucide-react";
import { cn } from "../../lib/utils";
import { Textarea } from "../ui/textarea";
import { Button } from "../ui/button";
import type {
  AgentChatExpertFeedbackRating,
  ChatMessage as ChatMessageType,
} from "../../types";
import { useAgentChatAdapterContext } from "../../adapter-context";

// Keep in sync with _MAX_REASON_LENGTH in api/routes/feedback.py.
const MAX_REASON_LENGTH = 500;

export function TurnFeedback({ msg }: { msg: ChatMessageType }) {
  const { adapter, sessionId } = useAgentChatAdapterContext();
  const [pending, setPending] = useState<AgentChatExpertFeedbackRating | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reasonDraft, setReasonDraft] = useState<string | null>(null);

  const llmResponseEventId = msg.llmResponseEventId;
  const canRate =
    llmResponseEventId !== undefined &&
    msg.status === "complete" &&
    sessionId !== null &&
    adapter.submitUserFeedback !== undefined;

  if (!canRate) return null;

  const rating = msg.userFeedback?.rating ?? pending;
  const alreadyRated = msg.userFeedback !== undefined;

  const submit = async (
    next: AgentChatExpertFeedbackRating,
    reason?: string,
  ) => {
    if (
      sessionId === null ||
      llmResponseEventId === undefined ||
      adapter.submitUserFeedback === undefined
    ) return;
    setPending(next);
    setError(null);
    try {
      await adapter.submitUserFeedback({
        sessionId,
        llmResponseEventId,
        rating: next,
        ...(reason ? { reason } : {}),
      });
      setReasonDraft(null);
    } catch (e) {
      setPending(null);
      setError(e instanceof Error ? e.message : "Failed to submit feedback");
    }
  };

  const handleRate = (next: AgentChatExpertFeedbackRating) => {
    if (alreadyRated || pending !== null) return;
    if (next === "down") {
      setReasonDraft("");
      return;
    }
    void submit("up");
  };

  return (
    <div className="mt-1 space-y-1.5">
      <div className="flex items-center gap-1 text-xs text-muted-foreground">
        <Button
          type="button"
          variant="ghost"
          size="icon-xs"
          aria-label="Good response"
          title={alreadyRated && rating === "up" ? "Good response (recorded)" : "Good response"}
          disabled={pending !== null || alreadyRated}
          onClick={() => handleRate("up")}
          className={cn(
            rating === "up" ? "text-foreground" : "text-muted-foreground/60",
          )}
        >
          <ThumbsUpIcon className="size-3.5" />
        </Button>
        <Button
          type="button"
          variant="ghost"
          size="icon-xs"
          aria-label="Poor response"
          title={alreadyRated && rating === "down" ? "Poor response (recorded)" : "Poor response"}
          disabled={
            pending !== null ||
            reasonDraft !== null ||
            alreadyRated
          }
          onClick={() => handleRate("down")}
          className={cn(
            rating === "down" ? "text-foreground" : "text-muted-foreground/60",
          )}
        >
          <ThumbsDownIcon className="size-3.5" />
        </Button>
        {alreadyRated && msg.userFeedback?.reason && (
          <span
            className="text-muted-foreground/70 truncate max-w-xs"
            title={msg.userFeedback.reason}
          >
            · "{msg.userFeedback.reason}"
          </span>
        )}
        {error && <span className="text-red-500 ml-1">{error}</span>}
      </div>

      {reasonDraft !== null && !alreadyRated && (
        <ReasonForm
          initialValue={reasonDraft}
          busy={pending !== null}
          onSubmit={(reason) => void submit("down", reason)}
          onCancel={() => {
            setReasonDraft(null);
            setError(null);
          }}
        />
      )}
    </div>
  );
}

function ReasonForm({
  initialValue,
  busy,
  onSubmit,
  onCancel,
}: {
  initialValue: string;
  busy: boolean;
  onSubmit: (reason: string) => void;
  onCancel: () => void;
}) {
  const [value, setValue] = useState(initialValue);
  const trimmed = value.trim();
  const charsLeft = MAX_REASON_LENGTH - value.length;

  const submit = () => {
    if (trimmed && !busy) onSubmit(trimmed);
  };

  return (
    <div className="rounded-md border border-border bg-muted/30 p-2 space-y-1.5">
      <label className="text-xs text-muted-foreground" htmlFor="turn-feedback-reason">
        What was wrong with the response?
      </label>
      <Textarea
        id="turn-feedback-reason"
        autoFocus
        rows={3}
        value={value}
        maxLength={MAX_REASON_LENGTH}
        placeholder="e.g. wrong answer, missed a constraint, hallucinated a fact…"
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
            e.preventDefault();
            submit();
          } else if (e.key === "Escape") {
            e.preventDefault();
            onCancel();
          }
        }}
        className="text-xs min-h-12"
        disabled={busy}
      />
      <div className="flex items-center justify-between gap-2">
        <span
          className={cn(
            "text-[10px] tabular-nums",
            charsLeft < 50 ? "text-amber-500" : "text-muted-foreground/60",
          )}
        >
          {charsLeft} characters left
        </span>
        <div className="flex items-center gap-1.5">
          <Button
            type="button"
            variant="ghost"
            size="xs"
            onClick={onCancel}
            disabled={busy}
          >
            Cancel
          </Button>
          <Button
            type="button"
            variant="default"
            size="xs"
            onClick={submit}
            disabled={busy || !trimmed}
          >
            {busy ? "Sending…" : "Send feedback"}
          </Button>
        </div>
      </div>
    </div>
  );
}
```

- [x] **Step 4: Run the tests — all six should pass**

```bash
cd /work/surogates/sdk/agent-chat-react && npx vitest run tests/turn-feedback.test.tsx
```
Expected: 6 PASS.

- [x] **Step 5: Commit**

```bash
cd /work/surogates && git add sdk/agent-chat-react/src/components/chat/turn-feedback.tsx sdk/agent-chat-react/tests/turn-feedback.test.tsx && git commit -m "feat(chat-sdk): TurnFeedback component for assistant thumbs"
```

---

## Task 5: `chat-thread` integration

**Files:**
- Modify: `surogates/sdk/agent-chat-react/src/components/chat/chat-thread.tsx`
  - `TimelineEntry` `text` variant declaration (around line 100)
  - `messageToEntries` text-push site (around line 204–211)
  - assistant-group flattening before timeline rendering (around line 690)
  - `TextEntry` body (around line 645–664)
  - Add `TurnFeedback` import

Carry the message through to the text-entry render so it can render `<TurnFeedback />`. Keep the current preamble behavior: when a message has tool calls, its text content is folded into reasoning and receives no feedback controls. After the assistant group is flattened and grouped, mark only the last `text` entry in that assistant group as the final user-facing answer.

- [x] **Step 1: Add the `TurnFeedback` import**

Near the top of `chat-thread.tsx`, alongside the other component imports, add:

```ts
import { TurnFeedback } from "./turn-feedback";
```

- [x] **Step 2: Extend the `text` variant of `TimelineEntry`**

Replace the `text` entry in the union (around line 100) with:

```ts
  | {
      kind: "text";
      key: string;
      content: string;
      isStreaming: boolean;
      msg: ChatMessageType;
      isFinalTurnText?: boolean;
    }
```

- [x] **Step 3: Populate the new fields when pushing a text entry**

In `messageToEntries`, find the existing block (around line 204):

```ts
  if (effectiveHasContent) {
    entries.push({
      kind: "text",
      key: `${msg.id}-text`,
      content: msg.content,
      isStreaming,
    });
  }
```

Replace with:

```ts
  if (effectiveHasContent) {
    entries.push({
      kind: "text",
      key: `${msg.id}-text`,
      content: msg.content,
      isStreaming,
      msg,
    });
  }
```

- [x] **Step 4: Mark only the final text entry in each assistant group**

In `AssistantGroup`, after:

```ts
  entries = groupBrowserActivityEntries(entries);
  entries = groupWebSearchEntries(entries);
```

add:

```ts
  for (let i = entries.length - 1; i >= 0; i--) {
    const entry = entries[i];
    if (entry.kind === "text") {
      entries[i] = { ...entry, isFinalTurnText: true };
      break;
    }
  }
```

This prevents multiple thumbs controls when consecutive assistant
messages are grouped into one visual turn.

- [x] **Step 5: Render `TurnFeedback` inside `TextEntry`**

Replace `TextEntry` (around line 645–664) with:

```tsx
function TextEntry({
  entry,
  step,
}: {
  entry: Extract<TimelineEntry, { kind: "text" }>;
  step: number;
}) {
  const content = useSmoothStream(entry.content, entry.isStreaming);
  return (
    <TimelineItem step={step}>
      <TimelineHeader>
        <TimelineSeparator style={{ backgroundColor: "var(--color-border)" }} />
        <TimelineIndicator className="size-2 border-none bg-foreground/40" />
      </TimelineHeader>
      <TimelineContent>
        <MessageResponse>{content}</MessageResponse>
        {entry.isFinalTurnText && entry.msg.status === "complete" && (
          <TurnFeedback msg={entry.msg} />
        )}
      </TimelineContent>
    </TimelineItem>
  );
}
```

- [x] **Step 6: Run typecheck and the full SDK suite**

```bash
cd /work/surogates/sdk/agent-chat-react && npx tsc --noEmit && npx vitest run
```
Expected: typecheck clean, all tests PASS.

- [x] **Step 7: Commit**

```bash
cd /work/surogates && git add sdk/agent-chat-react/src/components/chat/chat-thread.tsx && git commit -m "feat(chat-sdk): render TurnFeedback under final assistant text"
```

---

## Task 6: Web adapter wire-up

**Files:**
- Modify: `surogates/web/src/api/feedback.ts` — rename `submitExpertFeedback` → `submitTurnFeedback`
- Modify: `surogates/web/src/features/chat/surogates-web-chat-adapter.ts` — update the import + caller; add a new `submitUserFeedback` method

The HTTP helper is endpoint-generic (it just POSTs to `/feedback` against a target event id), so we rename it to remove the "expert" misnomer. The SDK adapter method `submitExpertFeedback` stays unchanged; only the helper inside the web adapter is renamed.

- [x] **Step 1: Rename the helper in `web/src/api/feedback.ts`**

Replace `submitExpertFeedback` with `submitTurnFeedback`. The full file becomes:

```ts
// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Client for the turn-feedback endpoint. Lets the UI record a
// thumbs-up or thumbs-down on any rate-able assistant turn event
// (an expert.result, emitting EXPERT_ENDORSE/EXPERT_OVERRIDE, or an
// llm.response, emitting USER_FEEDBACK).  The backend routes by the
// target event's type, so this client is event-agnostic.

import { authFetch } from "./auth";

export type FeedbackRating = "up" | "down";

export interface FeedbackResponse {
  event_id: number;
  event_type: string;
}

export async function submitTurnFeedback(
  sessionId: string,
  targetEventId: number,
  rating: FeedbackRating,
  reason?: string,
): Promise<FeedbackResponse> {
  const response = await authFetch(
    `/api/v1/sessions/${sessionId}/events/${targetEventId}/feedback`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ rating, reason }),
    },
  );
  if (!response.ok) {
    const err = (await response.json().catch(() => null)) as {
      detail?: string;
    } | null;
    throw new Error(err?.detail ?? "Failed to submit feedback");
  }
  return (await response.json()) as FeedbackResponse;
}
```

- [x] **Step 2: Update the import in `surogates-web-chat-adapter.ts`**

Change line 4 from:

```ts
import { submitExpertFeedback as submitExpertFeedbackApi } from "@/api/feedback";
```

to:

```ts
import { submitTurnFeedback } from "@/api/feedback";
```

- [x] **Step 3: Update the existing `submitExpertFeedback` adapter method**

Around lines 226–234, change the body from:

```ts
  async submitExpertFeedback(input) {
    const response = await submitExpertFeedbackApi(
      input.sessionId,
      input.expertResultEventId,
      input.rating,
      input.reason,
    );
    return { eventId: response.event_id, eventType: response.event_type };
  },
```

to:

```ts
  async submitExpertFeedback(input) {
    const response = await submitTurnFeedback(
      input.sessionId,
      input.expertResultEventId,
      input.rating,
      input.reason,
    );
    return { eventId: response.event_id, eventType: response.event_type };
  },
```

- [x] **Step 4: Add the new `submitUserFeedback` method**

Immediately after `submitExpertFeedback` (still in the adapter object), add:

```ts
  async submitUserFeedback(input) {
    const response = await submitTurnFeedback(
      input.sessionId,
      input.llmResponseEventId,
      input.rating,
      input.reason,
    );
    return { eventId: response.event_id, eventType: response.event_type };
  },
```

- [x] **Step 5: Run the web app typecheck**

```bash
cd /work/surogates/web && npm run typecheck
```
Expected: no errors. The TypeScript compiler verifies the adapter shape matches `AgentChatAdapter`.

- [x] **Step 6: Run the web build**

```bash
cd /work/surogates/web && npm run build
```
Expected: exit 0. Confirms no other call sites of `submitExpertFeedback` (the renamed helper) were missed.

- [x] **Step 7: Commit**

```bash
cd /work/surogates && git add web/src/api/feedback.ts web/src/features/chat/surogates-web-chat-adapter.ts && git commit -m "feat(web): wire submitUserFeedback to the renamed turn-feedback helper"
```

---

## Task 7: End-to-end smoke verification

**Files:** none modified.

Confirm the whole stack compiles and tests pass in both packages, and exercise the live UI once.

- [x] **Step 1: Full SDK test suite** — 116/116 PASS

- [x] **Step 2: Full SDK typecheck** — clean

- [x] **Step 3: Web typecheck + build** — both clean

- [ ] **Step 4: Run the web app locally and exercise the flow** — requires a live backend with a logged-in session; the unit suite covers rendering and click handling, but the SSE replay path needs a running deployment to confirm interactively. **Action item for the human partner.**

```bash
cd /work/surogates/web && npm run dev
```

Open the app in a browser, log in, send a chat message, wait for a complete assistant reply, then:
- Click thumbs-up under the answer → buttons disable, thumbs-up stays highlighted.
- Reload the page → the thumbs-up rating still shows (proves `user.feedback` SSE replay reduces correctly).
- Start a new turn, click thumbs-down → reason textarea appears, type "test reason", Cmd+Enter → buttons disable, the reason appears next to the icons.
- Reload → thumbs-down + reason still visible.

If any of these fail, debug before committing. No commit produced for this step.

- [ ] **Step 5: Final summary commit (if any docs need touching up)** — skipped; no follow-ups required from Step 4 yet.

---

## Self-review

**Spec coverage:**
- Data model (types + listened events) → Task 1
- Reducer `llmResponseEventId` tracking → Task 2
- Reducer `applyUserFeedback` (with source filter + idempotency) → Task 3
- `TurnFeedback` component (canRate gate, optimistic submit, reason form, already-rated state) → Task 4
- `chat-thread` integration → Task 5
- Web adapter wire-up (HTTP helper rename + `submitUserFeedback` method) → Task 6
- End-to-end verification (suite + manual smoke) → Task 7
- Studio out-of-scope note → preserved in spec; no task

**Placeholder scan:** no TBDs, no "implement appropriate error handling", every code step shows the exact code; every command shows the exact invocation and expected outcome.

**Type / name consistency:**
- `llmResponseEventId` used identically in types, reducer code, component code, adapter method.
- `userFeedback: { rating, reason }` shape consistent across types, reducer writes, component reads.
- `submitUserFeedback` signature matches across SDK adapter interface, component call site, and web adapter implementation.
- `submitTurnFeedback` rename touches both the export in `api/feedback.ts` and its sole caller in the web adapter.
