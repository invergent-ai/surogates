// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// research source collection.  The reducer accumulates one
// AgentChatResearchSource per successful research_memory(add) tool
// result; the citations/sources panel reads ``state.researchSources``
// and ``CitationText`` resolves [S#] markers against it.

import { describe, expect, it } from "vitest";
import {
  applyAgentChatEvent,
  createInitialAgentChatState,
} from "../src/runtime/reducer";

function toolCall(name: string, callId: string, eventId = 1) {
  return {
    type: "tool.call" as const,
    eventId,
    data: { tool_call_id: callId, name, arguments: "{}" },
  };
}

function toolResult(result: string, callId: string, eventId = 2) {
  return {
    type: "tool.result" as const,
    eventId,
    data: { tool_call_id: callId, result },
  };
}

describe("research source collection", () => {
  it("adds a source when research_memory(add) succeeds", () => {
    let state = createInitialAgentChatState({ isLoadingHistory: false });
    state = applyAgentChatEvent(state, toolCall("research_memory", "c1"));
    state = applyAgentChatEvent(
      state,
      toolResult(
        JSON.stringify({
          success: true,
          source_id: "S1",
          url: "https://a.test",
          title: "A",
        }),
        "c1",
      ),
    );
    expect(state.researchSources).toHaveLength(1);
    expect(state.researchSources[0]).toMatchObject({
      sourceId: "S1",
      url: "https://a.test",
      title: "A",
    });
  });

  it("dedupes by sourceId", () => {
    let state = createInitialAgentChatState({ isLoadingHistory: false });
    state = applyAgentChatEvent(state, toolCall("research_memory", "c1"));
    state = applyAgentChatEvent(
      state,
      toolResult(
        JSON.stringify({
          success: true,
          source_id: "S1",
          url: "https://a.test",
          title: "A",
        }),
        "c1",
      ),
    );
    // A second call comes back with the same source_id — defensive
    // dedup so a pubsub double-fire does not duplicate the entry.
    state = applyAgentChatEvent(state, toolCall("research_memory", "c2", 3));
    state = applyAgentChatEvent(
      state,
      toolResult(
        JSON.stringify({
          success: true,
          source_id: "S1",
          url: "https://a.test",
          title: "A again",
        }),
        "c2",
        4,
      ),
    );
    expect(state.researchSources).toHaveLength(1);
  });

  it("ignores non-research tool results", () => {
    let state = createInitialAgentChatState({ isLoadingHistory: false });
    state = applyAgentChatEvent(state, toolCall("web_search", "c2"));
    state = applyAgentChatEvent(state, toolResult("{}", "c2"));
    expect(state.researchSources).toHaveLength(0);
  });

  it("ignores research_memory(add) when success is false", () => {
    let state = createInitialAgentChatState({ isLoadingHistory: false });
    state = applyAgentChatEvent(state, toolCall("research_memory", "c1"));
    state = applyAgentChatEvent(
      state,
      toolResult(
        JSON.stringify({ success: false, error: "no url" }),
        "c1",
      ),
    );
    expect(state.researchSources).toHaveLength(0);
  });

  it("ignores research_memory(retrieve) results — only (add) adds", () => {
    // research_memory(retrieve) returns ``sources`` (a list) without a
    // top-level ``source_id``.  The collector must not treat the
    // ``sources`` array as a single new entry.
    let state = createInitialAgentChatState({ isLoadingHistory: false });
    state = applyAgentChatEvent(state, toolCall("research_memory", "c1"));
    state = applyAgentChatEvent(
      state,
      toolResult(
        JSON.stringify({
          success: true,
          sources: [
            { source_id: "S1", url: "u1", title: "A", summary: "", evidence: [] },
          ],
        }),
        "c1",
      ),
    );
    expect(state.researchSources).toHaveLength(0);
  });

  it("tolerates malformed JSON in the tool result", () => {
    let state = createInitialAgentChatState({ isLoadingHistory: false });
    state = applyAgentChatEvent(state, toolCall("research_memory", "c1"));
    state = applyAgentChatEvent(
      state,
      toolResult("not json", "c1"),
    );
    expect(state.researchSources).toHaveLength(0);
  });

  it("accepts the result under either 'result' or 'content' field", () => {
    // Some routes carry the result as ``content`` (history replay)
    // rather than ``result`` (live wire event); the collector reads
    // either.
    let state = createInitialAgentChatState({ isLoadingHistory: false });
    state = applyAgentChatEvent(state, toolCall("research_memory", "c1"));
    state = applyAgentChatEvent(state, {
      type: "tool.result" as const,
      eventId: 2,
      data: {
        tool_call_id: "c1",
        content: JSON.stringify({
          success: true,
          source_id: "S7",
          url: "https://x.test",
          title: "X",
        }),
      },
    });
    expect(state.researchSources).toHaveLength(1);
    expect(state.researchSources[0].sourceId).toBe("S7");
  });

  it("preserves source order across many adds", () => {
    let state = createInitialAgentChatState({ isLoadingHistory: false });
    for (let i = 1; i <= 3; i++) {
      state = applyAgentChatEvent(
        state,
        toolCall("research_memory", `c${i}`, 2 * i - 1),
      );
      state = applyAgentChatEvent(
        state,
        toolResult(
          JSON.stringify({
            success: true,
            source_id: `S${i}`,
            url: `u${i}`,
            title: `T${i}`,
          }),
          `c${i}`,
          2 * i,
        ),
      );
    }
    expect(state.researchSources.map((s) => s.sourceId)).toEqual([
      "S1",
      "S2",
      "S3",
    ]);
  });
});
