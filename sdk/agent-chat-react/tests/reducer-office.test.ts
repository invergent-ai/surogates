import { describe, expect, it } from "vitest";
import { applyAgentChatEvent, createInitialAgentChatState } from "../src/runtime/reducer";
import { AGENT_CHAT_LISTENED_EVENTS } from "../src/runtime/events";

describe("office save events", () => {
  it("are listened to", () => {
    expect(AGENT_CHAT_LISTENED_EVENTS).toContain("office.conflict");
    expect(AGENT_CHAT_LISTENED_EVENTS).toContain("office.save_failed");
  });

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
});

describe("live office edits", () => {
  const call = (eventId: number, path: string, name = "office_edit") => ({
    type: "tool.call" as const,
    eventId,
    data: { tool_call_id: `tc${eventId}`, name, arguments: { path, ops: [] } },
  });

  it("are listened to", () => {
    expect(AGENT_CHAT_LISTENED_EVENTS).toContain("stream.live");
  });

  it("open nothing from the replayed backlog, and the document of a live edit", () => {
    let state = applyAgentChatEvent(createInitialAgentChatState(), call(5, "docs/Old.docx"));
    expect(state.live).toBe(false);
    expect(state.officeAutoOpen).toBeNull();
    state = applyAgentChatEvent(state, { type: "stream.live", eventId: 0, data: {} });
    expect(state.live).toBe(true);
    state = applyAgentChatEvent(state, call(9, "__WORKSPACE__/docs/Plan.docx"));
    expect(state.officeAutoOpen).toEqual({ path: "docs/Plan.docx", eventId: 9 });
    state = applyAgentChatEvent(state, call(10, "docs/notes.md", "write_file"));
    expect(state.officeAutoOpen).toEqual({ path: "docs/Plan.docx", eventId: 9 });
  });

  it("refresh the file tree when an edit finishes", () => {
    const state = applyAgentChatEvent(createInitialAgentChatState(), call(5, "docs/Plan.docx"));
    const next = applyAgentChatEvent(state, {
      type: "tool.result",
      eventId: 6,
      data: { tool_call_id: "tc5", name: "office_edit", content: "{}", elapsed_ms: 900 },
    });
    expect(next.workspaceRefreshKey).toBe(state.workspaceRefreshKey + 1);
  });
});
