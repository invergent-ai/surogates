/**
 * Reducer handlers for iteration.summary and turn.summary events.
 * Verifies attachment-by-(turnId, iterationIndex) lookup, the
 * tail-of-turn rule for turn.summary, and the preserve-across-rehydrate
 * invariant that prevents later llm.response events from wiping out
 * previously attached summaries.
 */
import { describe, expect, it } from "vitest";

import {
  applyAgentChatEvent,
  createInitialAgentChatState,
} from "../src/runtime/reducer";

describe("iteration.summary reducer handling", () => {
  it("attaches summary to the matching assistant message", () => {
    let state = createInitialAgentChatState({ isLoadingHistory: false });
    state = applyAgentChatEvent(state, {
      type: "llm.response",
      eventId: 1,
      data: {
        message: { role: "assistant", content: "hi", tool_calls: [] },
        turn_id: "t-1",
        iteration_index: 0,
      },
    });
    state = applyAgentChatEvent(state, {
      type: "iteration.summary",
      eventId: 2,
      data: {
        turn_id: "t-1",
        iteration_index: 0,
        summary: "Said hi",
        tool_call_ids: ["c1", "c2"],
        started_at: "2026-05-24T00:00:00Z",
        ended_at: "2026-05-24T00:00:01Z",
      },
    });
    expect(state.messages.at(-1)?.iterationSummary).toEqual({
      iterationIndex: 0,
      summary: "Said hi",
      toolCallIds: ["c1", "c2"],
      startedAt: "2026-05-24T00:00:00Z",
      endedAt: "2026-05-24T00:00:01Z",
    });
  });

  it("is a no-op when no matching assistant message exists", () => {
    let state = createInitialAgentChatState({ isLoadingHistory: false });
    state = applyAgentChatEvent(state, {
      type: "iteration.summary",
      eventId: 1,
      data: {
        turn_id: "t-1",
        iteration_index: 0,
        summary: "Stale",
        tool_call_ids: [],
        started_at: "2026-05-24T00:00:00Z",
        ended_at: "2026-05-24T00:00:01Z",
      },
    });
    expect(state.messages).toHaveLength(0);
  });

  it("handles out-of-order arrival: summary lands after a later iteration's llm.response", () => {
    // Real-world pattern: iteration 0's summarizer is slow, iteration
    // 1's llm.response arrives first, then iteration 0's summary
    // lands. The reducer must still attach iteration 0's summary to
    // the right message.
    let state = createInitialAgentChatState({ isLoadingHistory: false });
    // Iteration 0 — tool-calling response (a new message at idx 0).
    state = applyAgentChatEvent(state, {
      type: "llm.response",
      eventId: 1,
      data: {
        message: {
          role: "assistant",
          content: "",
          tool_calls: [
            { id: "c1", type: "function",
              function: { name: "todo", arguments: "{}" } },
          ],
        },
        turn_id: "t-1",
        iteration_index: 0,
      },
    });
    state = applyAgentChatEvent(state, {
      type: "tool.call",
      eventId: 2,
      data: { tool_call_id: "c1", name: "todo", arguments: "{}" },
    });
    state = applyAgentChatEvent(state, {
      type: "tool.result",
      eventId: 3,
      data: { tool_call_id: "c1", content: "{}" },
    });
    // Iteration 1's llm.response lands BEFORE iteration 0's summary.
    state = applyAgentChatEvent(state, {
      type: "llm.response",
      eventId: 4,
      data: {
        message: { role: "assistant", content: "done", tool_calls: [] },
        turn_id: "t-1",
        iteration_index: 1,
      },
    });
    expect(state.messages).toHaveLength(2);
    // Now iteration 0's summary lands late.
    state = applyAgentChatEvent(state, {
      type: "iteration.summary",
      eventId: 5,
      data: {
        turn_id: "t-1",
        iteration_index: 0,
        summary: "Outlined the plan",
        tool_call_ids: ["c1"],
        started_at: "2026-05-24T00:00:00Z",
        ended_at: "2026-05-24T00:00:01Z",
      },
    });
    // Attaches to message[0] (iteration 0), not message[1] (iteration 1).
    expect(state.messages[0]?.iterationSummary?.summary).toBe(
      "Outlined the plan",
    );
    expect(state.messages[1]?.iterationSummary).toBeUndefined();
    // And iteration 1's summary, when it eventually arrives, finds
    // the right message too.
    state = applyAgentChatEvent(state, {
      type: "iteration.summary",
      eventId: 6,
      data: {
        turn_id: "t-1",
        iteration_index: 1,
        summary: "Wrapped up",
        tool_call_ids: [],
        started_at: "",
        ended_at: "",
      },
    });
    expect(state.messages[1]?.iterationSummary?.summary).toBe("Wrapped up");
    expect(state.messages[0]?.iterationSummary?.summary).toBe(
      "Outlined the plan",
    );
  });

  it("matches a message even when iteration_index is 0 (regression: numberValue would default to 0)", () => {
    let state = createInitialAgentChatState({ isLoadingHistory: false });
    state = applyAgentChatEvent(state, {
      type: "llm.response",
      eventId: 1,
      data: {
        message: { role: "assistant", content: "hi", tool_calls: [] },
        turn_id: "t-1",
        iteration_index: 0,
      },
    });
    state = applyAgentChatEvent(state, {
      type: "iteration.summary",
      eventId: 2,
      data: {
        turn_id: "t-1",
        iteration_index: 0,
        summary: "Zero index works",
        tool_call_ids: [],
        started_at: "",
        ended_at: "",
      },
    });
    expect(state.messages.at(-1)?.iterationSummary?.summary).toBe(
      "Zero index works",
    );
  });

  it("ignores payloads missing required fields", () => {
    let state = createInitialAgentChatState({ isLoadingHistory: false });
    state = applyAgentChatEvent(state, {
      type: "llm.response",
      eventId: 1,
      data: {
        message: { role: "assistant", content: "hi", tool_calls: [] },
        turn_id: "t-1",
        iteration_index: 0,
      },
    });
    state = applyAgentChatEvent(state, {
      type: "iteration.summary",
      eventId: 2,
      data: {
        // turn_id missing
        iteration_index: 0,
        summary: "no turn id",
      },
    });
    expect(state.messages.at(-1)?.iterationSummary).toBeUndefined();
  });
});

describe("turn.summary reducer handling", () => {
  it("attaches turn.summary to the LAST assistant message in the turn", () => {
    let state = createInitialAgentChatState({ isLoadingHistory: false });
    // Iteration 0: tool-calling response. We also dispatch tool.call so
    // the assistant message has a toolCalls field (the real harness
    // emits tool.call alongside llm.response); without it, the second
    // llm.response merges into this message instead of pushing a new
    // one, which would mask the LAST-message lookup we want to test.
    state = applyAgentChatEvent(state, {
      type: "llm.response",
      eventId: 1,
      data: {
        message: {
          role: "assistant",
          content: "",
          tool_calls: [
            { id: "c1", type: "function", function: { name: "todo", arguments: "{}" } },
          ],
        },
        turn_id: "t-1",
        iteration_index: 0,
      },
    });
    state = applyAgentChatEvent(state, {
      type: "tool.call",
      eventId: 2,
      data: {
        tool_call_id: "c1",
        name: "todo",
        arguments: "{}",
      },
    });
    state = applyAgentChatEvent(state, {
      type: "tool.result",
      eventId: 3,
      data: { tool_call_id: "c1", content: "{}" },
    });
    // Iteration 1: text-only completion — new message because the
    // previous one has tool_calls attached.
    state = applyAgentChatEvent(state, {
      type: "llm.response",
      eventId: 4,
      data: {
        message: { role: "assistant", content: "done", tool_calls: [] },
        turn_id: "t-1",
        iteration_index: 1,
      },
    });
    state = applyAgentChatEvent(state, {
      type: "turn.summary",
      eventId: 5,
      data: {
        turn_id: "t-1",
        recap: "Did the thing.",
        artifacts: [
          { kind: "file", label: "x.md", ref: "x.md" },
          { kind: "url", label: "example", ref: "https://example.com" },
        ],
      },
    });

    expect(state.messages).toHaveLength(2);
    const tail = state.messages.at(-1);
    expect(tail?.turnSummary?.recap).toBe("Did the thing.");
    expect(tail?.turnSummary?.artifacts).toHaveLength(2);
    expect(state.messages[0]?.turnSummary).toBeUndefined();
  });

  it("drops artifacts with unknown kinds", () => {
    let state = createInitialAgentChatState({ isLoadingHistory: false });
    state = applyAgentChatEvent(state, {
      type: "llm.response",
      eventId: 1,
      data: {
        message: { role: "assistant", content: "hi", tool_calls: [] },
        turn_id: "t-1",
        iteration_index: 0,
      },
    });
    state = applyAgentChatEvent(state, {
      type: "turn.summary",
      eventId: 2,
      data: {
        turn_id: "t-1",
        recap: "ok",
        artifacts: [
          { kind: "file", label: "good.txt", ref: "good.txt" },
          { kind: "weirdo", label: "bad", ref: "bad" },
          { kind: "url", label: "", ref: "https://example.com" },  // empty label dropped
        ],
      },
    });
    const tail = state.messages.at(-1);
    expect(tail?.turnSummary?.artifacts).toEqual([
      { kind: "file", label: "good.txt", ref: "good.txt" },
    ]);
  });

  it("is a no-op when no message in the turn exists", () => {
    let state = createInitialAgentChatState({ isLoadingHistory: false });
    state = applyAgentChatEvent(state, {
      type: "turn.summary",
      eventId: 1,
      data: { turn_id: "t-1", recap: "stale", artifacts: [] },
    });
    expect(state.messages).toHaveLength(0);
  });

  it("ignores payloads missing turn_id", () => {
    let state = createInitialAgentChatState({ isLoadingHistory: false });
    state = applyAgentChatEvent(state, {
      type: "llm.response",
      eventId: 1,
      data: {
        message: { role: "assistant", content: "hi", tool_calls: [] },
        turn_id: "t-1",
        iteration_index: 0,
      },
    });
    state = applyAgentChatEvent(state, {
      type: "turn.summary",
      eventId: 2,
      data: { recap: "no turn id", artifacts: [] },
    });
    expect(state.messages.at(-1)?.turnSummary).toBeUndefined();
  });
});

describe("summary preservation across llm.response rehydration", () => {
  it("a follow-up llm.response (matchesExistingToolTurn) does not wipe an attached iterationSummary", () => {
    let state = createInitialAgentChatState({ isLoadingHistory: false });
    state = applyAgentChatEvent(state, {
      type: "llm.response",
      eventId: 1,
      data: {
        message: {
          role: "assistant",
          content: "",
          tool_calls: [
            { id: "c1", type: "function", function: { name: "todo", arguments: "{}" } },
          ],
        },
        turn_id: "t-1",
        iteration_index: 0,
      },
    });
    state = applyAgentChatEvent(state, {
      type: "iteration.summary",
      eventId: 2,
      data: {
        turn_id: "t-1",
        iteration_index: 0,
        summary: "Outlined the plan",
        tool_call_ids: ["c1"],
        started_at: "2026-05-24T00:00:00Z",
        ended_at: "2026-05-24T00:00:01Z",
      },
    });
    // Same iteration's response re-emitted (rehydration path).
    state = applyAgentChatEvent(state, {
      type: "llm.response",
      eventId: 3,
      data: {
        message: {
          role: "assistant",
          content: "",
          tool_calls: [
            { id: "c1", type: "function", function: { name: "todo", arguments: "{}" } },
          ],
        },
        turn_id: "t-1",
        iteration_index: 0,
      },
    });
    expect(state.messages.at(-1)?.iterationSummary?.summary).toBe(
      "Outlined the plan",
    );
  });
});
