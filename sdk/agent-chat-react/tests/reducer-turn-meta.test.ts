/**
 * The reducer stamps turn_id + iteration_index from llm.delta /
 * llm.thinking / llm.response onto the resulting assistant message so
 * the Simple chat view can correlate later iteration.summary and
 * turn.summary events back to the right message.
 */
import { describe, expect, it } from "vitest";

import {
  applyAgentChatEvent,
  createInitialAgentChatState,
} from "../src/runtime/reducer";
import { streamedIterationEvents } from "./streamed-iteration-fixture";

describe("llm event meta stamping", () => {
  for (const replay of [false, true]) {
    it(`keeps early tool results in their iteration during ${replay ? "history replay" : "live streaming"}`, () => {
      let state = createInitialAgentChatState();
      for (const event of streamedIterationEvents()) {
        if (replay && event.type === "llm.delta") continue;
        state = applyAgentChatEvent(state, event);
      }
      const assistants = state.messages.filter((message) => message.role === "assistant");
      expect(assistants).toHaveLength(replay ? 2 : 3);
      for (let i = 0; i < 2; i++) {
        expect(assistants[i]).toMatchObject({
          iterationIndex: i, status: "complete", reasoningTokens: [34, 143][i],
        });
        expect(assistants[i].toolCalls?.map((tc) => tc.id)).toEqual([
          `tool-${i}-a`, `tool-${i}-b`,
        ]);
      }
      if (!replay) {
        expect(assistants[2]).toMatchObject({
          iterationIndex: 2, status: "streaming", reasoningDeltaCount: 232,
        });
      }
    });
  }

  it("stamps turn_id and iteration_index from llm.delta on a new streaming message", () => {
    let state = createInitialAgentChatState({ isLoadingHistory: false });
    state = applyAgentChatEvent(state, {
      type: "llm.delta",
      eventId: 1,
      data: {
        content: "hi",
        turn_id: "turn-1",
        iteration_index: 0,
      },
    });
    expect(state.messages.at(-1)?.turnId).toBe("turn-1");
    expect(state.messages.at(-1)?.iterationIndex).toBe(0);
  });

  it("preserves turn_id when subsequent llm.delta events append to the same message", () => {
    let state = createInitialAgentChatState({ isLoadingHistory: false });
    state = applyAgentChatEvent(state, {
      type: "llm.delta",
      eventId: 1,
      data: { content: "hi ", turn_id: "turn-1", iteration_index: 0 },
    });
    state = applyAgentChatEvent(state, {
      type: "llm.delta",
      eventId: 2,
      data: { content: "there" },  // no meta this time
    });
    expect(state.messages).toHaveLength(1);
    expect(state.messages[0]?.turnId).toBe("turn-1");
    expect(state.messages[0]?.iterationIndex).toBe(0);
    expect(state.messages[0]?.content).toBe("hi there");
  });

  it("stamps turn_id and iteration_index from llm.thinking on the current message", () => {
    let state = createInitialAgentChatState({ isLoadingHistory: false });
    state = applyAgentChatEvent(state, {
      type: "llm.thinking",
      eventId: 1,
      data: {
        reasoning: "let me think",
        turn_id: "turn-2",
        iteration_index: 1,
      },
    });
    expect(state.messages.at(-1)?.turnId).toBe("turn-2");
    expect(state.messages.at(-1)?.iterationIndex).toBe(1);
  });

  it("stamps turn_id and iteration_index from llm.response onto a new assistant message", () => {
    let state = createInitialAgentChatState({ isLoadingHistory: false });
    state = applyAgentChatEvent(state, {
      type: "llm.response",
      eventId: 1,
      data: {
        message: { role: "assistant", content: "hello", tool_calls: [] },
        turn_id: "turn-3",
        iteration_index: 2,
      },
    });
    expect(state.messages.at(-1)?.turnId).toBe("turn-3");
    expect(state.messages.at(-1)?.iterationIndex).toBe(2);
  });

  it("stamps turn_id from llm.response onto a streaming-rehydrated message", () => {
    let state = createInitialAgentChatState({ isLoadingHistory: false });
    state = applyAgentChatEvent(state, {
      type: "llm.delta",
      eventId: 1,
      data: { content: "partial", turn_id: "turn-4", iteration_index: 0 },
    });
    state = applyAgentChatEvent(state, {
      type: "llm.response",
      eventId: 2,
      data: {
        message: { role: "assistant", content: "partial", tool_calls: [] },
        turn_id: "turn-4",
        iteration_index: 0,
      },
    });
    expect(state.messages).toHaveLength(1);
    expect(state.messages[0]?.turnId).toBe("turn-4");
    expect(state.messages[0]?.iterationIndex).toBe(0);
  });

  it("leaves turnId undefined when the event payload omits it (backwards compat)", () => {
    let state = createInitialAgentChatState({ isLoadingHistory: false });
    state = applyAgentChatEvent(state, {
      type: "llm.response",
      eventId: 1,
      data: {
        message: { role: "assistant", content: "hello", tool_calls: [] },
      },
    });
    expect(state.messages.at(-1)?.turnId).toBeUndefined();
    expect(state.messages.at(-1)?.iterationIndex).toBeUndefined();
  });
});
