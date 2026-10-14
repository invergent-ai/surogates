/**
 * A step the platform wrote first into a person's web chat: its context
 * block and the record of the opener are notes for the agent, so the
 * person sees only what the agent wrote.
 */
import { describe, expect, it } from "vitest";

import {
  applyAgentChatEvent,
  createInitialAgentChatState,
} from "../src/runtime/reducer";

describe("platform notes in the chat", () => {
  it("shows the agent's step message, not the notes written for it", () => {
    let state = createInitialAgentChatState({ isLoadingHistory: false });
    for (const [eventId, synthetic] of [
      [1, "checkin"],
      [2, "checkin_opener"],
      [3, "outreach_opener"],
    ] as const) {
      state = applyAgentChatEvent(state, {
        type: "user.message",
        eventId,
        data: { content: "[Step] internal context", synthetic },
      });
    }
    expect(state.messages).toHaveLength(0);
    expect(state.isRunning).toBe(false);

    state = applyAgentChatEvent(state, {
      type: "llm.response",
      eventId: 4,
      data: { message: { role: "assistant", content: "Bună ziua! Cum vă simțiți azi?" } },
    });
    state = applyAgentChatEvent(state, {
      type: "user.message",
      eventId: 5,
      data: { content: "Bine, mulțumesc" },
    });
    expect(state.messages.map((m) => [m.role, m.content])).toEqual([
      ["assistant", "Bună ziua! Cum vă simțiți azi?"],
      ["user", "Bine, mulțumesc"],
    ]);
  });
});
