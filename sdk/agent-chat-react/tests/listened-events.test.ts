import { describe, expect, it } from "vitest";

import { AGENT_CHAT_LISTENED_EVENTS } from "../src/runtime/events";

describe("AGENT_CHAT_LISTENED_EVENTS", () => {
  it("includes iteration.summary so the SSE stream is subscribed", () => {
    expect(AGENT_CHAT_LISTENED_EVENTS).toContain("iteration.summary");
  });

  it("includes turn.summary so the SSE stream is subscribed", () => {
    expect(AGENT_CHAT_LISTENED_EVENTS).toContain("turn.summary");
  });
});
