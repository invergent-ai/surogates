// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Smoke tests for the sources/citations panel.  Two contracts:
//
//   * Empty-state message when no sources are collected yet.
//   * Each source renders a link with id ``source-<id>`` so the
//     citation chip's ``scrollToSource`` can find it.

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

describe("ResearchSourcesPanel", () => {
  it("renders the empty-state message when no sources exist yet", () => {
    const out = render(
      createElement(ResearchSourcesPanel, { sources: [] }),
    );
    expect(out.textContent).toContain("No research sources yet");
  });

  it("renders one row per source with anchor id source-<id>", () => {
    const sources: AgentChatResearchSource[] = [
      { sourceId: "S1", url: "https://a.test", title: "Alpha" },
      { sourceId: "S2", url: "https://b.test", title: "Beta" },
    ];
    const out = render(
      createElement(ResearchSourcesPanel, { sources }),
    );
    expect(out.querySelector("#source-S1")).not.toBeNull();
    expect(out.querySelector("#source-S2")).not.toBeNull();
    expect(out.textContent).toContain("Alpha");
    expect(out.textContent).toContain("Beta");
    expect(out.textContent).toContain("a.test");
  });

  it("includes a sources count badge", () => {
    const sources: AgentChatResearchSource[] = [
      { sourceId: "S1", url: "u1", title: "" },
      { sourceId: "S2", url: "u2", title: "" },
      { sourceId: "S3", url: "u3", title: "" },
    ];
    const out = render(
      createElement(ResearchSourcesPanel, { sources }),
    );
    expect(out.textContent).toContain("Sources · 3");
  });

  it("falls back to URL when title is empty", () => {
    const sources: AgentChatResearchSource[] = [
      { sourceId: "S1", url: "https://no-title.test/path", title: "" },
    ];
    const out = render(
      createElement(ResearchSourcesPanel, { sources }),
    );
    // The visible label uses the URL when title is missing.
    expect(out.textContent).toContain("https://no-title.test/path");
  });
});
