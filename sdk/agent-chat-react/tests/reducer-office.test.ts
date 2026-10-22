import { describe, expect, it } from "vitest";
import { applyAgentChatEvent, createInitialAgentChatState } from "../src/runtime/reducer";
import { AGENT_CHAT_LISTENED_EVENTS } from "../src/runtime/events";

describe("office save events", () => {
  it("records a conflict for its path and refreshes the workspace for the copy", () => {
    const state = createInitialAgentChatState();
    const next = applyAgentChatEvent(state, {
      type: "office.conflict",
      eventId: 7,
      data: { path: "docs/Plan.docx", conflict_path: "docs/Plan (edited 2026-10-13 09.05 UTC).docx" },
    });
    expect(next.officeEvents["docs/Plan.docx"]).toEqual({
      type: "office.conflict",
      eventId: 7,
      conflictPath: "docs/Plan (edited 2026-10-13 09.05 UTC).docx",
    });
    expect(next.workspaceRefreshKey).toBe(state.workspaceRefreshKey + 1);
  });

  it("records a failed save for its path without a refresh", () => {
    const state = applyAgentChatEvent(createInitialAgentChatState(), {
      type: "office.conflict",
      eventId: 7,
      data: { path: "a.xlsx", conflict_path: "a (edited 2026-10-13 09.05 UTC).xlsx" },
    });
    const next = applyAgentChatEvent(state, {
      type: "office.save_failed",
      eventId: 9,
      data: { path: "b.docx", attempts: 10 },
    });
    expect(next.officeEvents["b.docx"]).toEqual({ type: "office.save_failed", eventId: 9 });
    expect(next.officeEvents["a.xlsx"]?.type).toBe("office.conflict");
    expect(next.workspaceRefreshKey).toBe(state.workspaceRefreshKey);
  });

  it("records a save waiting on the computer, and the save that landed after it", () => {
    expect(AGENT_CHAT_LISTENED_EVENTS).toEqual(expect.arrayContaining(["office.save_waiting", "office.saved"]));
    const state = createInitialAgentChatState();
    const waiting = applyAgentChatEvent(state, {
      type: "office.save_waiting", eventId: 3, data: { path: "a.docx", computer: "Mac", reason: "offline" },
    });
    expect(waiting.officeEvents["a.docx"]).toEqual({ type: "office.save_waiting", eventId: 3, computer: "Mac", reason: "offline" });
    const saved = applyAgentChatEvent(waiting, { type: "office.saved", eventId: 4, data: { path: "a.docx" } });
    expect(saved.officeEvents["a.docx"]).toEqual({ type: "office.saved", eventId: 4 });
    expect(saved.workspaceRefreshKey).toBe(state.workspaceRefreshKey);
  });
});

describe("live office edits", () => {
  const call = (eventId: number, path: string, name = "office_edit") => ({
    type: "tool.call" as const,
    eventId,
    data: { tool_call_id: `tc${eventId}`, name, arguments: { path, ops: [] } },
  });
  const result = (eventId: number, callEventId: number, name = "office_edit", content = "{}") => ({
    type: "tool.result" as const,
    eventId,
    data: { tool_call_id: `tc${callEventId}`, name, content, elapsed_ms: 900 },
  });

  it("open nothing from the replayed backlog, and the document of a live edit once it has run", () => {
    let state = applyAgentChatEvent(createInitialAgentChatState(), call(5, "docs/Old.docx"));
    state = applyAgentChatEvent(state, result(6, 5));
    expect(state.live).toBe(false);
    expect(state.officeAutoOpen).toBeNull();
    state = applyAgentChatEvent(state, { type: "stream.live", eventId: 0, data: {} });
    expect(state.live).toBe(true);
    state = applyAgentChatEvent(state, call(9, "__WORKSPACE__/docs/Plan.docx"));
    // The call comes before the edit runs: a document the edit creates is not there yet.
    expect(state.officeAutoOpen).toBeNull();
    state = applyAgentChatEvent(state, result(10, 9));
    expect(state.officeAutoOpen).toEqual({ path: "docs/Plan.docx", eventId: 10 });
    state = applyAgentChatEvent(state, call(11, "docs/notes.md", "write_file"));
    state = applyAgentChatEvent(state, result(12, 11, "write_file"));
    expect(state.officeAutoOpen).toEqual({ path: "docs/Plan.docx", eventId: 10 });
  });

  it("open nothing for an edit that failed", () => {
    let state = applyAgentChatEvent(createInitialAgentChatState(), { type: "stream.live", eventId: 0, data: {} });
    state = applyAgentChatEvent(state, call(5, "docs/New.docx"));
    state = applyAgentChatEvent(state, result(6, 5, "office_edit", JSON.stringify({ error: "The editor could not do that" })));
    expect(state.officeAutoOpen).toBeNull();
  });

  it("refresh the file tree when an edit finishes", () => {
    const state = applyAgentChatEvent(createInitialAgentChatState(), call(5, "docs/Plan.docx"));
    const next = applyAgentChatEvent(state, result(6, 5));
    expect(next.workspaceRefreshKey).toBe(state.workspaceRefreshKey + 1);
  });
});
