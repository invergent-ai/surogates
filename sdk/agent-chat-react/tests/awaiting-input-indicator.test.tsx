/**
 * The "Working on it…" running indicator must not show while the
 * session is parked on the user.
 *
 * When the agent emits an ``ask_user_question`` and waits for an
 * answer, the reducer keeps ``isRunning`` true (the tool call is still
 * pending), but the agent is NOT working — it's the user's turn. The
 * thread-level shimmer would read as "busy", contradicting the question
 * widget shown right above it. It must be suppressed until the answer
 * lands (the ask tool flips out of "running").
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
    getArtifact: vi.fn().mockResolvedValue(null),
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
  vi.useRealTimers();
});

function mount(node: ReactElement): HTMLDivElement {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => {
    root?.render(
      <AgentChatAdapterProvider value={{ adapter: adapterStub(), sessionId: "s-1" }}>
        <TooltipProvider>{node}</TooltipProvider>
      </AgentChatAdapterProvider>,
    );
  });
  return container;
}

const noop = () => Promise.resolve();

function userMessage(): ChatMessage {
  return {
    id: "user-1",
    role: "user",
    content: "design a thing",
    createdAt: new Date(),
    status: "complete",
  } as ChatMessage;
}

function pendingAskTurn(): ChatMessage {
  return {
    id: "asst-ask",
    role: "assistant",
    content: "Here's the design. Does it work?",
    createdAt: new Date(),
    status: "streaming",
    turnId: "t-1",
    iterationIndex: 0,
    toolCalls: [
      {
        id: "call_ask",
        toolName: "ask_user_question",
        args: JSON.stringify({
          questions: [
            {
              prompt: "Does this design work for you?",
              choices: [{ label: "Yes, build it" }, { label: "Changes needed" }],
              allow_other: false,
            },
          ],
        }),
        status: "running",
      },
    ],
  } as ChatMessage;
}

function runningTerminalTurn(): ChatMessage {
  return {
    id: "asst-tool",
    role: "assistant",
    content: "Let me run that.",
    createdAt: new Date(),
    status: "streaming",
    turnId: "t-1",
    iterationIndex: 0,
    toolCalls: [
      { id: "call_term", toolName: "terminal", args: "{}", status: "running" },
    ],
  } as ChatMessage;
}

describe("Working-on-it indicator vs. awaiting user input", () => {
  for (const viewMode of ["simple", "expert"] as const) {
    it(`${viewMode}: suppresses 'Working on it' while a pending ask_user_question blocks the turn`, () => {
      const dom = mount(
        <ChatThread
          sessionId="s-1"
          messages={[userMessage(), pendingAskTurn()]}
          isRunning={true}
          terminal={false}
          onSend={noop}
          onStop={noop}
          viewMode={viewMode}
        />,
      );
      expect(dom.textContent).not.toContain("Working on it");
      // The question widget is still presented.
      expect(dom.textContent).toContain("Does this design work for you?");
    });
  }

  it("suppresses 'Working on it' even when a trailing system marker follows the pending ask", () => {
    // A create_artifact + ask_user_question turn lands its
    // artifact.created system message as the literal tail; the helper
    // must look past it to the still-pending ask, not give up at the tail.
    const artifactMarker: ChatMessage = {
      id: "sys-artifact",
      role: "system",
      content: "plan.md",
      createdAt: new Date(),
      status: "complete",
      systemKind: "artifact",
      systemMeta: { artifact_id: "a1", name: "plan.md", kind: "markdown", version: 1 },
    } as ChatMessage;
    const dom = mount(
      <ChatThread
        sessionId="s-1"
        messages={[userMessage(), pendingAskTurn(), artifactMarker]}
        isRunning={true}
        terminal={false}
        onSend={noop}
        onStop={noop}
        viewMode="simple"
      />,
    );
    expect(dom.textContent).not.toContain("Working on it");
    expect(dom.textContent).toContain("Does this design work for you?");
  });

  it("disables the composer (no Stop button) while parked on a pending ask", () => {
    const dom = mount(
      <ChatThread
        sessionId="s-1"
        messages={[userMessage(), pendingAskTurn()]}
        isRunning={true}
        terminal={false}
        onSend={noop}
        onStop={noop}
        viewMode="simple"
      />,
    );
    // Composer is replaced by a hint pointing at the question widget.
    expect(dom.textContent).toContain("Answer the question above to continue.");
    // The Stop/abort control is gone (it would otherwise abort the
    // session if the user typed an answer and submitted).
    expect(dom.querySelector('[aria-label="Stop"]')).toBeNull();
    // The composer textarea is not rendered.
    expect(dom.querySelector("textarea")).toBeNull();
  });

  it("still shows 'Working on it' when the running tool is NOT an ask", () => {
    const dom = mount(
      <ChatThread
        sessionId="s-1"
        messages={[userMessage(), runningTerminalTurn()]}
        isRunning={true}
        terminal={false}
        onSend={noop}
        onStop={noop}
        viewMode="simple"
      />,
    );
    expect(dom.textContent).toContain("Working on it");
  });
});
