// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Simple-mode labels for the deep-research tools.  Pins the verb-first
// labels so the planner's outputs read as natural prose instead of a
// wall of bare tool names ("research_memory" × 5) when a command's row
// opens onto several of them.

import { describe, expect, it } from "vitest";

import { toolRowLabel } from "../src/components/chat/simple-labels";
describe("Simple-mode rows for deep-research tools", () => {
  it("renders 'Stored source \"<title>\"' for research_memory(add) with a title", () => {
    const text = toolRowLabel({
      id: "c1",
      toolName: "research_memory",
      args: JSON.stringify({
        action: "add",
        url: "https://arxiv.org/abs/2024.12345",
        title: "Self-Route: Cost-aware Long-Context vs RAG",
        summary: "",
        evidence: [],
      }),
      status: "complete",
      result: "{\"success\": true, \"source_id\": \"S1\"}",
    });
    expect(text).toContain(
      'Stored source "Self-Route: Cost-aware Long-Context vs RAG"',
    );
    // Falls back to the title, not the raw tool name.
    expect(text).not.toContain("research_memory");
  });

  it("falls back to hostname when research_memory(add) lacks a title", () => {
    const text = toolRowLabel({
      id: "c2",
      toolName: "research_memory",
      args: JSON.stringify({
        action: "add",
        url: "https://www.example.com/path/to/paper",
      }),
      status: "complete",
      result: "{\"success\": true}",
    });
    expect(text).toContain('Stored source "example.com"');
  });

  it("renders 'Retrieved sources for \"<query>\"' for research_memory(retrieve)", () => {
    const text = toolRowLabel({
      id: "c3",
      toolName: "research_memory",
      args: JSON.stringify({
        action: "retrieve",
        query: "long-context cost trade-offs",
        k: 5,
      }),
      status: "complete",
      result: "{\"success\": true, \"sources\": []}",
    });
    expect(text).toContain(
      'Retrieved sources for "long-context cost trade-offs"',
    );
  });

  it("renders 'Listed sources' for research_memory(list)", () => {
    const text = toolRowLabel({
      id: "c4",
      toolName: "research_memory",
      args: JSON.stringify({ action: "list" }),
      status: "complete",
      result: "{\"success\": true, \"sources\": []}",
    });
    expect(text).toContain("Listed sources");
  });

  it("renders 'Updated outline (<N> sections)' for research_outline(set)", () => {
    const outline = [
      "# Title",
      "",
      "## Section A",
      "body",
      "",
      "## Section B",
      "body",
      "",
      "### Sub of B",
      "body",
    ].join("\n");
    const text = toolRowLabel({
      id: "c5",
      toolName: "research_outline",
      args: JSON.stringify({ action: "set", outline }),
      status: "complete",
      result: "{\"success\": true, \"sections\": []}",
    });
    // Two ##, one ### -> 3 sections.
    expect(text).toContain("Updated outline (3 sections)");
  });

  it("renders 'Fetched <hostname>' for web_extract", () => {
    const text = toolRowLabel({
      id: "c6",
      toolName: "web_extract",
      args: JSON.stringify({
        url: "https://www.anthropic.com/news/contextual-retrieval",
      }),
      status: "complete",
      result: "{}",
    });
    // Previously this read "Fetched a page" with no anchor; now the
    // hostname surfaces.
    expect(text).toContain("Fetched anthropic.com");
  });
});
