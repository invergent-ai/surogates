/**
 * IterationGroup — Simple-mode renderer for a single assistant
 * iteration. Behaviors covered:
 *
 * - Collapsed by default when an iterationSummary is attached; only the
 *   summary line + status dot show.
 * - Click expands the row to reveal the underlying timeline (reasoning
 *   + tool entries) using the existing Expert-mode rendering helpers.
 * - Live placeholders ("Thinking..." / "Working (N tools)") replace the
 *   summary line while the iteration is still streaming.
 * - Permanently expanded fallback when the iteration is complete but no
 *   summary ever arrived (replay of pre-feature history, summarizer
 *   timeout).
 */
import { act, type ReactElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";

import { IterationGroup } from "../src/components/chat/chat-thread";
import { TooltipProvider } from "../src/components/ui/tooltip";
import type { ChatMessage } from "../src/types";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT =
  true;

function buildMessage(over: Partial<ChatMessage> = {}): ChatMessage {
  return {
    id: "m1",
    role: "assistant",
    content: "",
    createdAt: new Date(),
    status: "complete",
    ...over,
  };
}

let root: Root | null = null;
let container: HTMLDivElement | null = null;

afterEach(() => {
  if (root) {
    act(() => root?.unmount());
  }
  root = null;
  container?.remove();
  container = null;
});


function mount(node: ReactElement): HTMLDivElement {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => {
    // TooltipProvider is normally established by <AgentChat>; provide one
    // here so the per-tool blocks (Patch, Read, etc.) can mount.
    root?.render(<TooltipProvider>{node}</TooltipProvider>);
  });
  return container;
}


describe("IterationGroup", () => {
  it("renders the summary as a collapsed row when present", () => {
    const message = buildMessage({
      turnId: "t-1",
      iterationIndex: 0,
      iterationSummary: {
        iterationIndex: 0,
        summary: "Rework hero paragraph",
        toolCallIds: ["c1"],
        startedAt: "",
        endedAt: "",
      },
      reasoning: "long internal reasoning text",
      toolCalls: [
        { id: "c1", toolName: "patch", args: "{}", status: "complete" },
      ],
    });
    const dom = mount(
      <IterationGroup
        message={message}
        sessionId="s-1"
        artifactFallbacks={{}}
      />,
    );
    expect(dom.textContent).toContain("Rework hero paragraph");
    // Reasoning content stays hidden until expanded.
    expect(dom.textContent).not.toContain("long internal reasoning text");
  });

  it("expands to reveal reasoning + tool entries when the row is clicked", () => {
    const message = buildMessage({
      turnId: "t-1",
      iterationIndex: 0,
      iterationSummary: {
        iterationIndex: 0,
        summary: "Outline the plan",
        toolCallIds: [],
        startedAt: "",
        endedAt: "",
      },
      toolCalls: [
        { id: "c1", toolName: "patch", args: "{\"path\":\"x.html\"}", status: "complete" },
      ],
    });
    const dom = mount(
      <IterationGroup
        message={message}
        sessionId="s-1"
        artifactFallbacks={{}}
      />,
    );
    const trigger = dom.querySelector("button");
    expect(trigger).not.toBeNull();
    act(() => {
      trigger!.click();
    });
    // The Patch label from the existing per-tool renderer is now visible.
    expect(dom.textContent).toContain("Patch");
  });

  it("shows a Thinking placeholder while streaming and no tools have started", () => {
    const message = buildMessage({
      turnId: "t-1",
      iterationIndex: 0,
      status: "streaming",
    });
    const dom = mount(
      <IterationGroup
        message={message}
        sessionId="s-1"
        artifactFallbacks={{}}
      />,
    );
    expect(dom.textContent).toMatch(/Thinking/i);
  });

  it("shows a Working placeholder with running-tool count", () => {
    const message = buildMessage({
      turnId: "t-1",
      iterationIndex: 0,
      status: "streaming",
      toolCalls: [
        { id: "c1", toolName: "patch", args: "{}", status: "running" },
        { id: "c2", toolName: "patch", args: "{}", status: "running" },
      ],
    });
    const dom = mount(
      <IterationGroup
        message={message}
        sessionId="s-1"
        artifactFallbacks={{}}
      />,
    );
    expect(dom.textContent).toMatch(/Working/i);
    expect(dom.textContent).toMatch(/2 tools/);
  });

  it("renders the underlying timeline permanently when no summary and not streaming", () => {
    const message = buildMessage({
      turnId: "t-1",
      iterationIndex: 0,
      reasoning: "post-hoc reasoning",
      toolCalls: [
        { id: "c1", toolName: "patch", args: "{}", status: "complete" },
      ],
    });
    const dom = mount(
      <IterationGroup
        message={message}
        sessionId="s-1"
        artifactFallbacks={{}}
      />,
    );
    // Tool block visible without expansion of the IterationGroup
    // itself. Note the inner Reasoning component owns its own
    // collapsible, which is fine — the assertion below is just that
    // the IterationGroup doesn't add yet another header row.
    expect(dom.textContent).toContain("Patch");
    expect(dom.textContent).not.toContain("(no summary)");
  });
});
