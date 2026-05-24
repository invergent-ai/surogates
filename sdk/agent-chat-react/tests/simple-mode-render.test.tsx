/**
 * Simple-mode ChatThread rendering: AssistantGroup composes
 * IterationGroup + final-answer text + TurnSummaryCard when
 * viewMode="simple"; falls back to the existing Expert timeline
 * when viewMode="expert".
 *
 * Also verifies system entries (skill_invoked, artifact) stay
 * visible in Simple mode (review correction).
 */
import { act, type ReactElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AgentChatAdapterProvider, NO_BROWSER_ADAPTER } from "../src/adapter-context";
import { ChatThread } from "../src/components/chat/chat-thread";
import { TooltipProvider } from "../src/components/ui/tooltip";
import type { AgentChatAdapter, ChatMessage } from "../src/types";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT =
  true;

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


function assistantMessage(over: Partial<ChatMessage> = {}): ChatMessage {
  return {
    id: "asst-1",
    role: "assistant",
    content: "Here's what I did.",
    createdAt: new Date(),
    status: "complete",
    turnId: "t-1",
    iterationIndex: 0,
    iterationSummary: {
      iterationIndex: 0,
      summary: "Reworked the hero copy",
      toolCallIds: ["c1"],
      startedAt: "",
      endedAt: "",
    },
    toolCalls: [
      { id: "c1", toolName: "patch", args: "{}", status: "complete", result: "{}" },
    ],
    turnSummary: {
      turnId: "t-1",
      recap: "Reworked the hero around brain/hands.",
      artifacts: [{ kind: "file", label: "landing.html", ref: "landing.html" }],
    },
    ...over,
  };
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


describe("Simple mode ChatThread rendering", () => {
  it("shows the iteration summary line, hides per-tool entries by default, shows the recap", () => {
    const messages = [
      // Iteration 0 with tool calls; iteration summary attached.
      assistantMessage({
        id: "iter-0",
        content: "",
        reasoning: "thinking briefly",
      }),
      // Iteration 1: final text answer.
      {
        id: "final",
        role: "assistant" as const,
        content: "Here's what I did.",
        createdAt: new Date(),
        status: "complete" as const,
        turnId: "t-1",
        iterationIndex: 1,
        turnSummary: {
          turnId: "t-1",
          recap: "Reworked the hero around brain/hands.",
          artifacts: [
            { kind: "file" as const, label: "landing.html", ref: "landing.html" },
          ],
        },
      },
    ];

    const dom = mount(
      <ChatThread
        sessionId="s-1"
        messages={messages}
        isRunning={false}
        onSend={noop}
        onStop={noop}
        viewMode="simple"
      />,
    );

    expect(dom.textContent).toContain("Reworked the hero copy"); // iteration summary
    expect(dom.textContent).toContain("Here's what I did."); // final answer
    expect(dom.textContent).toContain("Reworked the hero around brain/hands."); // recap
    expect(dom.textContent).toContain("landing.html"); // artifact
    // Per-tool labels stay collapsed in Simple mode.
    expect(dom.textContent).not.toMatch(/^Patch$/m);
  });

  it("Expert mode renders the per-tool timeline and hides the TurnSummaryCard", () => {
    const messages = [assistantMessage()];
    const dom = mount(
      <ChatThread
        sessionId="s-1"
        messages={messages}
        isRunning={false}
        onSend={noop}
        onStop={noop}
        viewMode="expert"
      />,
    );
    expect(dom.textContent).toContain("Patch");
    // TurnSummaryCard is hidden in Expert.
    expect(dom.textContent).not.toContain(
      "Reworked the hero around brain/hands.",
    );
  });

  it("Simple mode preserves skill.invoked system markers", () => {
    const messages: ChatMessage[] = [
      {
        id: "sys-1",
        role: "system",
        content: "frontend-design",
        createdAt: new Date(),
        status: "complete",
        systemKind: "skill_invoked",
        systemMeta: { skill: "frontend-design", staged_at: null },
      },
      assistantMessage({ id: "iter-0" }),
    ];
    const dom = mount(
      <ChatThread
        sessionId="s-1"
        messages={messages}
        isRunning={false}
        onSend={noop}
        onStop={noop}
        viewMode="simple"
      />,
    );
    expect(dom.textContent).toContain("frontend-design");
  });

  it("Simple mode falls back to the expanded timeline when an iteration has no summary", () => {
    const messages = [
      assistantMessage({
        id: "iter-0",
        iterationSummary: undefined,
      }),
    ];
    const dom = mount(
      <ChatThread
        sessionId="s-1"
        messages={messages}
        isRunning={false}
        onSend={noop}
        onStop={noop}
        viewMode="simple"
      />,
    );
    // Reverts to per-tool view because there's no summary.
    expect(dom.textContent).toContain("Patch");
    expect(dom.textContent).not.toContain("Reworked the hero copy");
  });
});
