// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Artifact revisions supersede the panel they revise.  ArtifactBlock
// always fetches the artifact's LATEST payload, so an appended second
// marker for the same artifact_id would render two panels with
// identical content.  The reducer must update the original marker in
// place instead.

import { describe, expect, it } from "vitest";
import {
  applyAgentChatEvent,
  createInitialAgentChatState,
} from "../src/runtime/reducer";
import type { AgentChatState } from "../src/types";

function created(artifactId: string, name: string, eventId = 1) {
  return {
    type: "artifact.created" as const,
    eventId,
    data: {
      artifact_id: artifactId,
      name,
      kind: "chart",
      version: 1,
      size: 100,
    },
  };
}

function updated(
  artifactId: string,
  name: string,
  version: number,
  eventId = 2,
) {
  return {
    type: "artifact.updated" as const,
    eventId,
    data: {
      artifact_id: artifactId,
      name,
      kind: "chart",
      version,
      size: 120,
    },
  };
}

const artifacts = (state: AgentChatState) =>
  state.messages.filter((m) => m.systemKind === "artifact");

describe("artifact revisions", () => {
  it("creates one panel", () => {
    let state = createInitialAgentChatState({ isLoadingHistory: false });
    state = applyAgentChatEvent(state, created("a1", "Revenue"));
    expect(artifacts(state)).toHaveLength(1);
  });

  it("does not add a second panel when revised", () => {
    let state = createInitialAgentChatState({ isLoadingHistory: false });
    state = applyAgentChatEvent(state, created("a1", "Revenue"));
    state = applyAgentChatEvent(state, updated("a1", "Revenue", 2));
    expect(artifacts(state)).toHaveLength(1);
  });

  it("bumps the version so the block re-fetches", () => {
    let state = createInitialAgentChatState({ isLoadingHistory: false });
    state = applyAgentChatEvent(state, created("a1", "Revenue"));
    state = applyAgentChatEvent(state, updated("a1", "Revenue", 2));
    expect(artifacts(state)[0].systemMeta?.version).toBe(2);
  });

  it("carries a renamed artifact through", () => {
    let state = createInitialAgentChatState({ isLoadingHistory: false });
    state = applyAgentChatEvent(state, created("a1", "Revenue"));
    state = applyAgentChatEvent(state, updated("a1", "Revenue by region", 2));
    const [panel] = artifacts(state);
    expect(panel.systemMeta?.name).toBe("Revenue by region");
    expect(panel.content).toBe("Revenue by region");
  });

  it("keeps the panel at its original position in the thread", () => {
    let state = createInitialAgentChatState({ isLoadingHistory: false });
    state = applyAgentChatEvent(state, created("a1", "Revenue"));
    const before = state.messages.length;
    state = applyAgentChatEvent(state, updated("a1", "Revenue", 2));
    expect(state.messages.length).toBe(before);
    expect(state.messages[0].systemKind).toBe("artifact");
  });

  it("keeps distinct artifacts as distinct panels", () => {
    let state = createInitialAgentChatState({ isLoadingHistory: false });
    state = applyAgentChatEvent(state, created("a1", "Revenue", 1));
    state = applyAgentChatEvent(state, created("a2", "Costs", 2));
    state = applyAgentChatEvent(state, updated("a1", "Revenue", 2, 3));
    const panels = artifacts(state);
    expect(panels).toHaveLength(2);
    expect(panels.map((p) => p.systemMeta?.artifact_id)).toEqual(["a1", "a2"]);
    expect(panels[0].systemMeta?.version).toBe(2);
    expect(panels[1].systemMeta?.version).toBe(1);
  });

  it("renders an update with no prior marker (history replay from mid-thread)", () => {
    let state = createInitialAgentChatState({ isLoadingHistory: false });
    state = applyAgentChatEvent(state, updated("a1", "Revenue", 4));
    const panels = artifacts(state);
    expect(panels).toHaveLength(1);
    expect(panels[0].systemMeta?.version).toBe(4);
  });
});
