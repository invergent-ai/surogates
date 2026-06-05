// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Tests for the research_memory / research_outline tool renderers.
// Each block reads the JSON-shaped tool call args + result and renders
// a compact summary line.  We render to a JSDOM container and assert
// against textContent so the tests are agnostic to Tailwind class
// changes.

import { describe, expect, it } from "vitest";
import { act, createElement } from "react";
import { createRoot } from "react-dom/client";

import { ResearchOutlineBlock } from "../src/components/chat/tools/research-tool";
import { ResearchMemoryBlock } from "../src/components/chat/tools/oneliner-tools";
import type { ToolCallInfo } from "../src/types";

(
  globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }
).IS_REACT_ACT_ENVIRONMENT = true;

function render(node: React.ReactNode): HTMLDivElement {
  const container = document.createElement("div");
  document.body.appendChild(container);
  const root = createRoot(container);
  act(() => {
    root.render(node);
  });
  return container;
}

function toolCall(overrides: Partial<ToolCallInfo>): ToolCallInfo {
  return {
    id: "tc-1",
    toolName: "research_memory",
    args: "{}",
    result: "",
    status: "complete",
    ...overrides,
  } as ToolCallInfo;
}

describe("ResearchMemoryBlock", () => {
  // The block lives alongside the other one-liners (SessionSearch,
  // WebTool, VisionAnalyze) and follows their shape: a bold
  // "Research" label + a muted action summary.  Tests assert the
  // visible substrings (source id, count, hostname, query) without
  // pinning the exact phrasing of the verb so a future tweak to
  // "recorded" / "retrieved" wording does not require a test rewrite.
  it("renders an add result with source id and hostname", () => {
    const tc = toolCall({
      toolName: "research_memory",
      args: JSON.stringify({
        action: "add",
        url: "https://www.example.com/post",
      }),
      result: JSON.stringify({
        success: true,
        source_id: "S3",
        url: "https://www.example.com/post",
      }),
    });
    const out = render(createElement(ResearchMemoryBlock, { tc }));
    expect(out.textContent).toContain("Research");
    expect(out.textContent).toContain("S3");
    expect(out.textContent).toContain("example.com");
  });

  it("renders a retrieve result with the query and source count", () => {
    const tc = toolCall({
      toolName: "research_memory",
      args: JSON.stringify({
        action: "retrieve",
        query: "qubits",
      }),
      result: JSON.stringify({
        success: true,
        sources: [
          { source_id: "S1" },
          { source_id: "S2" },
        ],
      }),
    });
    const out = render(createElement(ResearchMemoryBlock, { tc }));
    expect(out.textContent).toContain("Research");
    expect(out.textContent).toContain("2 sources");
    expect(out.textContent).toContain("qubits");
  });

  it("renders 1 source (singular) when retrieve returns exactly one", () => {
    const tc = toolCall({
      args: JSON.stringify({ action: "retrieve", query: "x" }),
      result: JSON.stringify({
        success: true,
        sources: [{ source_id: "S1" }],
      }),
    });
    const out = render(createElement(ResearchMemoryBlock, { tc }));
    expect(out.textContent).toContain("1 source");
    expect(out.textContent).not.toContain("1 sources");
  });

  it("renders a list result with the source count", () => {
    const tc = toolCall({
      args: JSON.stringify({ action: "list" }),
      result: JSON.stringify({
        success: true,
        sources: [
          { source_id: "S1" },
          { source_id: "S2" },
          { source_id: "S3" },
        ],
      }),
    });
    const out = render(createElement(ResearchMemoryBlock, { tc }));
    expect(out.textContent).toContain("3 sources");
  });

  it("renders the bare label when the args are malformed", () => {
    // No action visible -> just the "Research" label sits there
    // until the tool result arrives.  The label is the same row
    // shape as the other one-liners so the timeline stays scannable.
    const tc = toolCall({ args: "not json" });
    const out = render(createElement(ResearchMemoryBlock, { tc }));
    expect(out.textContent).toContain("Research");
  });
});

describe("ResearchOutlineBlock", () => {
  it("renders the outline body and section count on set", () => {
    const tc = toolCall({
      toolName: "research_outline",
      args: JSON.stringify({
        action: "set",
        outline: "# Report\n## Background\nbody\n## Methods\n",
      }),
      result: JSON.stringify({
        success: true,
        sections: ["Background", "Methods"],
      }),
    });
    const out = render(createElement(ResearchOutlineBlock, { tc }));
    expect(out.textContent).toContain("Research outline");
    expect(out.textContent).toContain("2 sections");
    expect(out.textContent).toContain("Background");
    expect(out.textContent).toContain("Methods");
  });

  it("renders 'updated' when the outline body is empty", () => {
    const tc = toolCall({
      toolName: "research_outline",
      args: JSON.stringify({ action: "set", outline: "" }),
      result: JSON.stringify({ success: true, sections: [] }),
    });
    const out = render(createElement(ResearchOutlineBlock, { tc }));
    expect(out.textContent).toContain("updated");
  });

  it("reads the outline body from the result when action=get", () => {
    const tc = toolCall({
      toolName: "research_outline",
      args: JSON.stringify({ action: "get" }),
      result: JSON.stringify({
        success: true,
        outline: "# R\n## Background\nbody\n",
        sections: ["Background"],
      }),
    });
    const out = render(createElement(ResearchOutlineBlock, { tc }));
    expect(out.textContent).toContain("Background");
  });
});
