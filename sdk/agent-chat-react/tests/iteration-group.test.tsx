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
    // The condensed row label (verb + path) is now visible.
    expect(dom.textContent).toContain("Edited x.html");
    // The "Done" footer marks the iteration as complete.
    expect(dom.textContent).toContain("Done");
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

  it("shows a derived live label collapsing same-tool runs while streaming", () => {
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
    expect(dom.textContent).toMatch(/Running.+Patch.+×.+2/);
  });

  it("shows a derived single-tool live label including the path detail", () => {
    // Even when tools have started and reasoning text exists, the
    // streaming row stays collapsed — users who want progress detail
    // switch to Expert mode.
    const message = buildMessage({
      turnId: "t-1",
      iterationIndex: 0,
      status: "streaming",
      reasoning: "internal thought that should stay hidden",
      toolCalls: [
        { id: "c1", toolName: "patch", args: "{\"path\":\"x.html\"}", status: "running" },
      ],
    });
    const dom = mount(
      <IterationGroup
        message={message}
        sessionId="s-1"
        artifactFallbacks={{}}
      />,
    );
    expect(dom.textContent).toMatch(/Running.+Patch.+x\.html/);
    // Reasoning content stays hidden (no expanded view during stream).
    expect(dom.textContent).not.toContain("internal thought");
  });

  it("swaps shimmer for derived label once all tools complete, even with status=streaming", () => {
    // Regression for user-reported bug: tool-using assistant messages
    // never transition out of status="streaming" — the reducer leaves
    // them that way for the rest of the turn. The IterationGroup must
    // detect "no running tools" as the iteration-done signal.
    const message = buildMessage({
      turnId: "t-1",
      iterationIndex: 0,
      status: "streaming",  // still tagged streaming!
      toolCalls: [
        { id: "c1", toolName: "list_files", args: "{\"path\":\".\"}", status: "complete" },
      ],
    });
    const dom = mount(
      <IterationGroup
        message={message}
        sessionId="s-1"
        artifactFallbacks={{}}
      />,
    );
    // Collapsed derived label, NOT the shimmer.
    expect(dom.textContent).not.toMatch(/Running/i);
    expect(dom.textContent).not.toMatch(/Thinking/i);
    expect(dom.textContent).toContain("List Files");
    expect(dom.querySelector("button[aria-expanded='false']"))
      .not.toBeNull();
  });

  it("derives a tool-name label when no summary is present (complete)", () => {
    const message = buildMessage({
      turnId: "t-1",
      iterationIndex: 0,
      toolCalls: [
        { id: "c1", toolName: "list_files", args: "{\"path\":\".\"}", status: "complete" },
      ],
    });
    const dom = mount(
      <IterationGroup
        message={message}
        sessionId="s-1"
        artifactFallbacks={{}}
      />,
    );
    // Collapsed by default — derived label includes the human tool
    // name and the path detail.
    expect(dom.textContent).toContain("List Files");
    // Collapsible trigger exists so users can drill into the Expert
    // timeline on demand.
    const trigger = dom.querySelector("button[aria-expanded='false']");
    expect(trigger).not.toBeNull();
  });

  it("collapses multiple same-tool runs into a 'Tool × N' label", () => {
    const message = buildMessage({
      turnId: "t-1",
      iterationIndex: 0,
      toolCalls: [
        { id: "c1", toolName: "patch", args: "{}", status: "complete" },
        { id: "c2", toolName: "patch", args: "{}", status: "complete" },
        { id: "c3", toolName: "patch", args: "{}", status: "complete" },
      ],
    });
    const dom = mount(
      <IterationGroup
        message={message}
        sessionId="s-1"
        artifactFallbacks={{}}
      />,
    );
    expect(dom.textContent).toMatch(/Patch.+×.+3/);
  });

  it("falls back to 'Used N tools' for mixed tool batches without a summary", () => {
    const message = buildMessage({
      turnId: "t-1",
      iterationIndex: 0,
      toolCalls: [
        { id: "c1", toolName: "patch", args: "{}", status: "complete" },
        { id: "c2", toolName: "read_file", args: "{}", status: "complete" },
      ],
    });
    const dom = mount(
      <IterationGroup
        message={message}
        sessionId="s-1"
        artifactFallbacks={{}}
      />,
    );
    expect(dom.textContent).toContain("Used 2 tools");
  });

  it("renders nothing when complete, no summary, no tools, no reasoning", () => {
    // Empty iteration — nothing to derive. The surrounding
    // SimpleAssistantGroup still renders any final text + the
    // TurnSummaryCard, so dropping this row is the right move.
    const message = buildMessage({
      turnId: "t-1",
      iterationIndex: 0,
    });
    const dom = mount(
      <IterationGroup
        message={message}
        sessionId="s-1"
        artifactFallbacks={{}}
      />,
    );
    expect(dom.textContent ?? "").toBe("");
  });
});
