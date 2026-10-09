import { describe, expect, it } from "vitest";

import { AGENT_CHAT_LISTENED_EVENTS } from "../src/runtime/events";

describe("AGENT_CHAT_LISTENED_EVENTS", () => {
  it("includes iteration.summary so the SSE stream is subscribed", () => {
    expect(AGENT_CHAT_LISTENED_EVENTS).toContain("iteration.summary");
  });

  it("includes turn.summary so the SSE stream is subscribed", () => {
    expect(AGENT_CHAT_LISTENED_EVENTS).toContain("turn.summary");
  });

  it("includes loop.result so scheduled results reach the reducer", () => {
    expect(AGENT_CHAT_LISTENED_EVENTS).toContain("loop.result");
  });

  it("includes the device wait events so a local folder's wait is shown", () => {
    expect(AGENT_CHAT_LISTENED_EVENTS).toContain("device.waiting");
    expect(AGENT_CHAT_LISTENED_EVENTS).toContain("device.resumed");
  });

  it("includes the computer's no-browser event so a local-folder chat's pane says it", () => {
    expect(AGENT_CHAT_LISTENED_EVENTS).toContain("browser.unavailable");
  });

  it("includes the coordinator's follow-up so a thread shows it", () => {
    expect(AGENT_CHAT_LISTENED_EVENTS).toContain("coordinator.message");
  });

  it("includes the worker and proposal events so a master's cards are drawn", () => {
    for (const type of ["worker.spawned", "worker.complete", "worker.failed", "thread.proposed"] as const) {
      expect(AGENT_CHAT_LISTENED_EVENTS).toContain(type);
    }
  });
});
