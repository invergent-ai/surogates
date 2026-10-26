/**
 * A session's history arrives as a replay, event by event. The chat shows its loading state until the replay
 * is over, so the thread opens at the bottom of the finished conversation instead of growing under the reader.
 */
import { describe, expect, it } from "vitest";

import { applyAgentChatEvent, createInitialAgentChatState } from "../src/runtime/reducer";

const replaying = () => createInitialAgentChatState({ isLoadingHistory: true });

describe("the history replay", () => {
  it("keeps the loading state while the replay delivers events", () => {
    let state = applyAgentChatEvent(replaying(), { type: "user.message", eventId: 1, data: { content: "hello" } });
    state = applyAgentChatEvent(state, { type: "llm.response", eventId: 2, data: { message: { content: "hi" } } });

    expect(state.messages).toHaveLength(2);
    expect(state.isLoadingHistory).toBe(true);
  });

  it("keeps it through a replayed end of a past turn", () => {
    let state = applyAgentChatEvent(replaying(), { type: "user.message", eventId: 1, data: { content: "hello" } });
    state = applyAgentChatEvent(state, { type: "session.complete", eventId: 2, data: {} });

    expect(state.isLoadingHistory).toBe(true);
  });

  it.each(["stream.live", "session.done", "stream.timeout"] as const)("ends when the stream says %s", (type) => {
    let state = applyAgentChatEvent(replaying(), { type: "user.message", eventId: 1, data: { content: "hello" } });
    state = applyAgentChatEvent(state, { type, eventId: 0, data: {} });

    expect(state.isLoadingHistory).toBe(false);
    expect(state.messages).toHaveLength(1);
  });
});
