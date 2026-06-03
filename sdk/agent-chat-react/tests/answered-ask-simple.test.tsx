/**
 * A completed ask_user_question keeps its rich Q/A block in Simple mode.
 *
 * While running, IterationGroup renders the interactive
 * AskUserQuestionToolBlock; once answered it used to collapse to a
 * generic "Ask User Question" tool row, dropping the question and the
 * user's chosen answer from the visible thread. The block already has
 * an answered/locked view (AskUserQuestionLocked), so the completed
 * iteration should render it too — preserving the decision record.
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

describe("answered ask_user_question in Simple mode", () => {
  it("shows the question and the chosen answer (locked block), not a bare tool row", () => {
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
    expect(dom.textContent).toContain("Clarification answered");
    expect(dom.textContent).toContain("Which approach do you prefer?");
    expect(dom.textContent).toContain("Surogate Cron");
  });
});
