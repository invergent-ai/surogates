// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// The composer stays usable beside an open question, which means the
// turn is still "running" while the user types. Everything that keys
// off isRunning has to know the difference, and the failure modes are
// silent: sending would cancel the very question being answered, and a
// slash command would leave it open until it timed out.

import { act, type ReactElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AgentChatAdapterProvider, NO_BROWSER_ADAPTER } from "../src/adapter-context";
import { ChatThread } from "../src/components/chat/chat-thread";
import { TooltipProvider } from "../src/components/ui/tooltip";
import type { AgentChatAdapter, ChatMessage } from "../src/types";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT =
  true;

const pauseSession = vi.fn().mockResolvedValue(undefined);

function adapterStub(): AgentChatAdapter {
  return {
    ...NO_BROWSER_ADAPTER,
    listSessions: vi.fn().mockResolvedValue({ sessions: [], total: 0 }),
    createSession: vi.fn(),
    getSession: vi.fn(),
    sendMessage: vi.fn(),
    pauseSession,
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
  pauseSession.mockClear();
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

function openAskTurn(): ChatMessage {
  return {
    id: "asst-ask",
    role: "assistant",
    content: "",
    createdAt: new Date(),
    status: "streaming",
    turnId: "t-1",
    iterationIndex: 0,
    toolCalls: [
      {
        id: "call_ask",
        toolName: "ask_user_question",
        args: JSON.stringify({
          questions: [{ prompt: "What subjects do you like at school?" }],
        }),
        status: "running",
      },
    ],
  } as ChatMessage;
}

function renderWithPendingAsk(handlers: {
  onSend: (text: string) => Promise<void>;
  onStop: () => Promise<void>;
}) {
  return mount(
    <ChatThread
      sessionId="s-1"
      messages={[openAskTurn()]}
      // The ask keeps the turn running even though the agent is idle.
      isRunning={true}
      terminal={false}
      onSend={handlers.onSend}
      onStop={handlers.onStop}
      viewMode="simple"
    />,
  );
}

function typeAndSubmit(dom: HTMLDivElement, text: string) {
  const textarea = dom.querySelector<HTMLTextAreaElement>("textarea");
  if (!textarea) throw new Error("composer textarea not rendered");
  const setValue = Object.getOwnPropertyDescriptor(
    HTMLTextAreaElement.prototype,
    "value",
  )?.set;
  act(() => {
    setValue?.call(textarea, text);
    textarea.dispatchEvent(new Event("input", { bubbles: true }));
  });
  act(() => {
    textarea.dispatchEvent(
      new KeyboardEvent("keydown", { key: "Enter", bubbles: true }),
    );
  });
  return textarea;
}

describe("composer while an answer is awaited", () => {
  it("sends the answer without stopping the session", async () => {
    // onStop pauses the session, and a paused session makes the pending
    // ask return cancelled -- so routing through it would cancel the
    // question the user is answering.
    const onStop = vi.fn().mockResolvedValue(undefined);
    const onSend = vi.fn().mockResolvedValue(undefined);
    const dom = renderWithPendingAsk({ onSend, onStop });

    typeAndSubmit(dom, "computers and sports");
    await act(async () => {});

    expect(onStop).not.toHaveBeenCalled();
    expect(onSend).toHaveBeenCalledTimes(1);
    expect(onSend.mock.calls[0]?.[0]).toBe("computers and sports");
  });

  it("does not open the slash menu", () => {
    // Slash commands route around sendMessage entirely, so the question
    // would sit open until its timeout with nothing happening.
    const dom = renderWithPendingAsk({ onSend: noop, onStop: noop });
    const textarea = dom.querySelector<HTMLTextAreaElement>("textarea");
    const setValue = Object.getOwnPropertyDescriptor(
      HTMLTextAreaElement.prototype,
      "value",
    )?.set;
    act(() => {
      setValue?.call(textarea, "/");
      textarea?.dispatchEvent(new Event("input", { bubbles: true }));
    });

    expect(dom.textContent).not.toContain("Scheduled Tasks");
  });

  it("pauses the session on Escape, the only way left to decline", async () => {
    renderWithPendingAsk({ onSend: noop, onStop: noop });

    act(() => {
      window.dispatchEvent(
        new KeyboardEvent("keydown", { key: "Escape", bubbles: true }),
      );
    });
    await act(async () => {});

    expect(pauseSession).toHaveBeenCalledWith({ sessionId: "s-1" });
  });

  it("does not pause on Escape once the question is answered", async () => {
    const answered = {
      ...openAskTurn(),
      status: "complete",
      toolCalls: [
        {
          id: "call_ask",
          toolName: "ask_user_question",
          args: JSON.stringify({
            questions: [{ prompt: "What subjects do you like at school?" }],
          }),
          status: "complete",
          result: "{}",
          askUserQuestionAnswers: [
            {
              question: "What subjects do you like at school?",
              answer: "computers",
              is_other: false,
            },
          ],
        },
      ],
    } as ChatMessage;

    mount(
      <ChatThread
        sessionId="s-1"
        messages={[answered]}
        isRunning={false}
        terminal={false}
        onSend={noop}
        onStop={noop}
        viewMode="simple"
      />,
    );

    act(() => {
      window.dispatchEvent(
        new KeyboardEvent("keydown", { key: "Escape", bubbles: true }),
      );
    });
    await act(async () => {});

    expect(pauseSession).not.toHaveBeenCalled();
  });
});
