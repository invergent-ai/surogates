/**
 * A completed ask_user_question keeps the exchange in Simple mode.
 *
 * While running, IterationGroup renders the question; once answered the
 * iteration must not collapse to a generic "Ask User Question" tool row
 * that drops the question from the visible thread.
 *
 * A ONE-question ask is conversational: the question stays as the
 * agent's own message and the answer arrives as a user bubble the
 * reducer synthesises, so the thread reads as a dialogue. It carries
 * none of the form chrome — no "Clarification answered" banner, no
 * "Q1." numbering, no bordered card.
 *
 * A MULTI-question ask is a batch decision and keeps the Q/A recap
 * block, which is the only place its answers are recorded.
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
      <AgentChatAdapterProvider value={{ adapter: adapterStub(), sessionId: "s-1" }}>
        <TooltipProvider>{node}</TooltipProvider>
      </AgentChatAdapterProvider>,
    );
  });
  return container;
}

const noop = () => Promise.resolve();

function answeredAskTurn(): ChatMessage {
  return {
    id: "asst-ask",
    role: "assistant",
    content: "Here's the design.",
    createdAt: new Date(),
    status: "complete",
    turnId: "t-1",
    iterationIndex: 0,
    toolCalls: [
      {
        id: "call_ask",
        toolName: "ask_user_question",
        args: JSON.stringify({
          questions: [{ prompt: "Which approach do you prefer?" }],
        }),
        status: "complete",
        result: "{}",
        askUserQuestionAnswers: [
          {
            question: "Which approach do you prefer?",
            answer: "Surogate Cron",
            is_other: false,
          },
        ],
      },
    ],
  } as ChatMessage;
}

function answeredBatchAskTurn(): ChatMessage {
  return {
    id: "asst-ask-batch",
    role: "assistant",
    content: "Here's the design.",
    createdAt: new Date(),
    status: "complete",
    turnId: "t-1",
    iterationIndex: 0,
    toolCalls: [
      {
        id: "call_ask_batch",
        toolName: "ask_user_question",
        args: JSON.stringify({
          questions: [
            { prompt: "Which approach do you prefer?" },
            { prompt: "Which region?" },
          ],
        }),
        status: "complete",
        result: "{}",
        askUserQuestionAnswers: [
          {
            question: "Which approach do you prefer?",
            answer: "Surogate Cron",
            is_other: false,
          },
          { question: "Which region?", answer: "eu-west", is_other: false },
        ],
      },
    ],
  } as ChatMessage;
}

describe("answered ask_user_question in Simple mode", () => {
  it("keeps a single question in the thread with no form chrome", () => {
    const dom = mount(
      <ChatThread
        sessionId="s-1"
        messages={[answeredAskTurn()]}
        isRunning={false}
        terminal={true}
        onSend={noop}
        onStop={noop}
        viewMode="simple"
      />,
    );
    expect(dom.textContent).toContain("Which approach do you prefer?");
    // The exchange reads as conversation, not as a filled-in form.
    expect(dom.textContent).not.toContain("Clarification answered");
    expect(dom.textContent).not.toContain("Q1.");
  });

  it("shows the Q/A recap for a multi-question batch", () => {
    const dom = mount(
      <ChatThread
        sessionId="s-1"
        messages={[answeredBatchAskTurn()]}
        isRunning={false}
        terminal={true}
        onSend={noop}
        onStop={noop}
        viewMode="simple"
      />,
    );
    expect(dom.textContent).toContain("Clarification answered");
    expect(dom.textContent).toContain("Which approach do you prefer?");
    expect(dom.textContent).toContain("Surogate Cron");
    expect(dom.textContent).toContain("eu-west");
  });
});
