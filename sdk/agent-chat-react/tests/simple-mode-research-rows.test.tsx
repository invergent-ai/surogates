// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Simple-mode condensed rows for the deep-research tools.  Pins the
// verb-first labels so the planner timeline reads as natural prose
// instead of a wall of bare tool names ("research_memory" × 5).
//
// The label generators (`_toolRowLabel`, `extractToolDetail`) are not
// exported; we drive them via the full ChatThread render path the
// same way the rest of the simple-mode tests do.

import { act, type ReactElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AgentChatAdapterProvider, NO_BROWSER_ADAPTER } from "../src/adapter-context";
import { ChatThread } from "../src/components/chat/chat-thread";
import { TooltipProvider } from "../src/components/ui/tooltip";
import type {
  AgentChatAdapter,
  ChatMessage,
  ToolCallInfo,
} from "../src/types";

(
  globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }
).IS_REACT_ACT_ENVIRONMENT = true;

function adapterStub(): AgentChatAdapter {
  return {
    ...NO_BROWSER_ADAPTER,
    listSessions: vi.fn().mockResolvedValue({ sessions: [], total: 0 }),
    createSession: vi.fn(),
    getSession: vi.fn(),
    sendMessage: vi.fn(),
    openEventStream: vi.fn(() => ({
      addEventListener: vi.fn(),
      close: vi.fn(),
      onerror: null,
    })),
  } as unknown as AgentChatAdapter;
}

let root: Root | null = null;
let container: HTMLDivElement | null = null;

afterEach(() => {
  if (root) act(() => root?.unmount());
  root = null;
  container?.remove();
  container = null;
});

function mount(node: ReactElement): HTMLDivElement {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => {
    root?.render(
      <AgentChatAdapterProvider
        value={{ adapter: adapterStub(), sessionId: "s-1" }}
      >
        <TooltipProvider>{node}</TooltipProvider>
      </AgentChatAdapterProvider>,
    );
  });
  return container;
}

const noop = () => Promise.resolve();

function withTools(tools: ToolCallInfo[]): ChatMessage {
  return {
    id: "iter-0",
    role: "assistant",
    content: "",
    createdAt: new Date(0),
    status: "complete",
    turnId: "t-1",
    iterationIndex: 0,
    iterationSummary: {
      iterationIndex: 0,
      summary: "doing research",
      toolCallIds: tools.map((t) => t.id),
      startedAt: "",
      endedAt: "",
    },
    toolCalls: tools,
  };
}

function renderSimple(message: ChatMessage): HTMLDivElement {
  const dom = mount(
    <ChatThread
      sessionId="s-1"
      messages={[
        message,
        {
          id: "final",
          role: "assistant",
          content: "Done.",
          createdAt: new Date(0),
          status: "complete",
          turnId: "t-1",
          iterationIndex: 1,
          turnSummary: {
            turnId: "t-1",
            recap: "wrapped up",
            artifacts: [],
          },
        },
      ]}
      isRunning={false}
      terminal
      onSend={noop}
      onStop={noop}
      viewMode="simple"
    />,
  );

  // Simple mode keeps the iteration body collapsed by default; the
  // tool-row labels we want to assert on only render once the
  // iteration group is expanded.  Click every collapsed toggle so
  // the body becomes visible.
  const collapsed = dom.querySelectorAll(
    "button[aria-expanded='false']",
  );
  collapsed.forEach((btn) => {
    act(() => {
      (btn as HTMLButtonElement).click();
    });
  });
  return dom;
}

describe("Simple-mode rows for deep-research tools", () => {
  it("renders 'Stored source \"<title>\"' for research_memory(add) with a title", () => {
    const dom = renderSimple(
      withTools([
        {
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
        },
      ]),
    );
    expect(dom.textContent).toContain(
      'Stored source "Self-Route: Cost-aware Long-Context vs RAG"',
    );
    // Falls back to the title, not the raw tool name.
    expect(dom.textContent).not.toContain("research_memory");
  });

  it("falls back to hostname when research_memory(add) lacks a title", () => {
    const dom = renderSimple(
      withTools([
        {
          id: "c2",
          toolName: "research_memory",
          args: JSON.stringify({
            action: "add",
            url: "https://www.example.com/path/to/paper",
          }),
          status: "complete",
          result: "{\"success\": true}",
        },
      ]),
    );
    expect(dom.textContent).toContain('Stored source "example.com"');
  });

  it("renders 'Retrieved sources for \"<query>\"' for research_memory(retrieve)", () => {
    const dom = renderSimple(
      withTools([
        {
          id: "c3",
          toolName: "research_memory",
          args: JSON.stringify({
            action: "retrieve",
            query: "long-context cost trade-offs",
            k: 5,
          }),
          status: "complete",
          result: "{\"success\": true, \"sources\": []}",
        },
      ]),
    );
    expect(dom.textContent).toContain(
      'Retrieved sources for "long-context cost trade-offs"',
    );
  });

  it("renders 'Listed sources' for research_memory(list)", () => {
    const dom = renderSimple(
      withTools([
        {
          id: "c4",
          toolName: "research_memory",
          args: JSON.stringify({ action: "list" }),
          status: "complete",
          result: "{\"success\": true, \"sources\": []}",
        },
      ]),
    );
    expect(dom.textContent).toContain("Listed sources");
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
    const dom = renderSimple(
      withTools([
        {
          id: "c5",
          toolName: "research_outline",
          args: JSON.stringify({ action: "set", outline }),
          status: "complete",
          result: "{\"success\": true, \"sections\": []}",
        },
      ]),
    );
    // Two ##, one ### -> 3 sections.
    expect(dom.textContent).toContain("Updated outline (3 sections)");
  });

  it("renders 'Fetched <hostname>' for web_extract", () => {
    const dom = renderSimple(
      withTools([
        {
          id: "c6",
          toolName: "web_extract",
          args: JSON.stringify({
            url: "https://www.anthropic.com/news/contextual-retrieval",
          }),
          status: "complete",
          result: "{}",
        },
      ]),
    );
    // Previously this read "Fetched a page" with no anchor; now the
    // hostname surfaces.
    expect(dom.textContent).toContain("Fetched anthropic.com");
  });
});
