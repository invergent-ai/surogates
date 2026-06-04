// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Smoke tests for the in-thread sources panel.  Contracts:
//
//   * Renders nothing when there are no sources (the chat thread also
//     branches on length>0, but the panel itself short-circuits to
//     keep callers honest).
//   * Header shows ``Sources · N`` even when collapsed.
//   * Body is hidden until the header is clicked, at which point each
//     source renders as a link with id ``source-<id>`` so the citation
//     chips can deep-link.

import { describe, expect, it } from "vitest";
import { act, createElement } from "react";
import { createRoot } from "react-dom/client";

import { ResearchSourcesPanel } from "../src/components/research/research-sources-panel";
import type { AgentChatResearchSource } from "../src/types";

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

function clickHeader(out: HTMLElement): void {
  const button = out.querySelector("button[aria-expanded]") as HTMLButtonElement
    | null;
  if (!button) throw new Error("Sources panel header button not found");
  act(() => {
    button.click();
  });
}

describe("ResearchSourcesPanel", () => {
  it("renders nothing when there are no sources", () => {
    const out = render(
      createElement(ResearchSourcesPanel, { sources: [] }),
    );
    // Empty state intentionally yields no DOM so the wrapping strip
    // above the composer doesn't reserve space for an empty card.
    expect(out.textContent).toBe("");
  });

  it("shows the sources count badge while collapsed", () => {
    const sources: AgentChatResearchSource[] = [
      { sourceId: "S1", url: "u1", title: "" },
      { sourceId: "S2", url: "u2", title: "" },
      { sourceId: "S3", url: "u3", title: "" },
    ];
    const out = render(
      createElement(ResearchSourcesPanel, { sources }),
    );
    expect(out.textContent).toContain("Sources · 3");
    // The list is hidden until the header is clicked.
    expect(out.querySelector("#source-S1")).toBeNull();
    expect(
      out.querySelector("button[aria-expanded='false']"),
    ).not.toBeNull();
  });

  it("reveals the source rows when the header is clicked", () => {
    const sources: AgentChatResearchSource[] = [
      { sourceId: "S1", url: "https://a.test", title: "Alpha" },
      { sourceId: "S2", url: "https://b.test", title: "Beta" },
    ];
    const out = render(
      createElement(ResearchSourcesPanel, { sources }),
    );
    clickHeader(out);
    expect(out.querySelector("#source-S1")).not.toBeNull();
    expect(out.querySelector("#source-S2")).not.toBeNull();
    expect(out.textContent).toContain("Alpha");
    expect(out.textContent).toContain("Beta");
    expect(out.textContent).toContain("a.test");
    expect(
      out.querySelector("button[aria-expanded='true']"),
    ).not.toBeNull();
  });

  it("falls back to URL when a title is empty", () => {
    const sources: AgentChatResearchSource[] = [
      { sourceId: "S1", url: "https://no-title.test/path", title: "" },
    ];
    const out = render(
      createElement(ResearchSourcesPanel, { sources }),
    );
    clickHeader(out);
    expect(out.textContent).toContain("https://no-title.test/path");
  });
});
