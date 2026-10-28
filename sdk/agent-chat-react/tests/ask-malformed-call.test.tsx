// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// A malformed ask_user_question call (a question without a text
// ``prompt``) or one the tool rejected must not break the thread: the
// agent retries with a valid call, and the old one is not answerable.

import { act, type ReactElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AgentChatAdapterProvider, NO_BROWSER_ADAPTER } from "../src/adapter-context";
import { ChatThread } from "../src/components/chat/chat-thread";
import { TooltipProvider } from "@invergent/ui-sdk/tooltip";
import type { AgentChatAdapter, ChatMessage } from "../src/types";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT =
  true;

const submitSpy = vi.fn().mockResolvedValue(undefined);

function adapterStub(): AgentChatAdapter {
  return {
    ...NO_BROWSER_ADAPTER,
    listSessions: vi.fn().mockResolvedValue({ sessions: [], total: 0 }),
    createSession: vi.fn(),
    getSession: vi.fn(),
    sendMessage: vi.fn(),
    submitAskUserQuestionResponse: submitSpy,
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
  submitSpy.mockClear();
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

function askTurn({
  content = "",
  questions,
  status = "running",
  answers,
  result,
}: {
  content?: string;
  questions: unknown[];
  status?: "running" | "complete";
  answers?: { question: string; answer: string; is_other: boolean }[];
  result?: string;
}): ChatMessage {
  return {
    id: "asst-ask",
    role: "assistant",
    content,
    createdAt: new Date(),
    status: status === "running" ? "streaming" : "complete",
    turnId: "t-1",
    iterationIndex: 0,
    toolCalls: [
      {
        id: "call_ask",
        toolName: "ask_user_question",
        args: JSON.stringify({ questions }),
        status,
        ...(status === "complete" ? { result: result ?? "{}" } : {}),
        ...(answers ? { askUserQuestionAnswers: answers } : {}),
      },
    ],
  } as ChatMessage;
}

function render(message: ChatMessage, viewMode: "simple" | "expert" = "simple") {
  return mount(
    <ChatThread
      sessionId="s-1"
      messages={[message]}
      isRunning={message.status === "streaming"}
      terminal={false}
      onSend={noop}
      onStop={noop}
      viewMode={viewMode}
    />,
  );
}

describe("malformed or rejected ask_user_question calls", () => {
  const rejected = JSON.stringify({ error: "questions[0].prompt is required." });
  const malformed = [{
    question: "Deploy it?",
    options: [{ label: "Yes, deploy it" }, { label: "No, cancel" }],
  }];

  it("does not crash on a question without a prompt while it runs", () => {
    expect(() => render(askTurn({ questions: malformed }))).not.toThrow();
  });

  it("does not crash and offers no buttons for a call the tool rejected", () => {
    let dom!: HTMLDivElement;
    expect(() => {
      dom = render(askTurn({ questions: malformed, status: "complete", result: rejected }));
    }).not.toThrow();
    expect(dom.textContent).not.toContain("Yes, deploy it");
  });

  it("shows nothing to answer for a well-formed call the tool rejected", () => {
    const dom = render(askTurn({
      questions: [{ prompt: "Deploy it?", choices: [{ label: "Yes" }, { label: "No" }] }],
      status: "complete",
      result: rejected,
    }));
    const labels = [...dom.querySelectorAll("button")].map((b) => b.textContent);
    expect(labels).not.toContain("Yes");
    expect(labels).not.toContain("No");
    expect(dom.textContent).not.toContain("Deploy it?");
  });

  it("keeps the valid questions of a batch with one malformed question", () => {
    let dom!: HTMLDivElement;
    expect(() => {
      dom = render(askTurn({ questions: [{ prompt: "Which GPU?" }, { question: "broken" }] }));
    }).not.toThrow();
    expect(dom.textContent).toContain("Which GPU?");
  });

  it("still shows a normal question", () => {
    const dom = render(askTurn({ questions: [{ prompt: "How's school going?" }] }));
    expect(dom.textContent).toContain("How's school going?");
  });
});
