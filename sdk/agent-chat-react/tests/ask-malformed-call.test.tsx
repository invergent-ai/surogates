// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// A malformed ask_user_question call (one the server's
// ``_validate_questions`` would refuse) or one the tool rejected asks
// nothing: no card, no composer lock, no listening orb.  The agent
// retries with a valid call, and the old one is not answerable.

import { act, type ReactElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AgentChatAdapterProvider, NO_BROWSER_ADAPTER } from "../src/adapter-context";
import { ChatThread } from "../src/components/chat/chat-thread";
import { messageOrbState } from "../src/runtime/orb-state";
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

function composer(dom: HTMLDivElement): HTMLTextAreaElement {
  const textarea = dom.querySelector<HTMLTextAreaElement>("textarea");
  if (!textarea) throw new Error("composer textarea not rendered");
  return textarea;
}

/** The composer reads as it does with no question pending. */
function expectNoAskComposer(dom: HTMLDivElement) {
  const textarea = composer(dom);
  expect(textarea.disabled).toBe(false);
  expect(textarea.placeholder).toMatch(/^Send a message/);
}

function buttonLabels(dom: HTMLDivElement): (string | null)[] {
  return [...dom.querySelectorAll("button")].map((b) => b.textContent);
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

  it("shows nothing and leaves the composer alone for a malformed call that runs", () => {
    for (const viewMode of ["simple", "expert"] as const) {
      const dom = render(askTurn({ questions: malformed }), viewMode);
      expect(dom.textContent).not.toContain("no questions provided");
      expect(dom.textContent).not.toContain("Yes, deploy it");
      expectNoAskComposer(dom);
      act(() => root?.unmount());
      root = null;
      container?.remove();
    }
  });

  it("offers nothing to answer for a batch with one malformed question", () => {
    const dom = render(askTurn({ questions: [{ prompt: "Which GPU?" }, { question: "broken" }] }));
    expect(dom.textContent).not.toContain("Which GPU?");
    const labels = buttonLabels(dom);
    expect(labels).not.toContain("Submit");
    expect(labels).not.toContain("Next question →");
    expectNoAskComposer(dom);
  });

  it("treats a whitespace prompt as no question", () => {
    const dom = render(askTurn({
      questions: [{ prompt: "   ", choices: [{ label: "Yes" }, { label: "No" }] }],
    }));
    expect(buttonLabels(dom)).not.toContain("Yes");
    expectNoAskComposer(dom);
  });

  it("treats choices given as a string as no question", () => {
    const dom = render(askTurn({ questions: [{ prompt: "Deploy it?", choices: "Yes, No" }] }));
    expect(dom.textContent).not.toContain("Deploy it?");
    expectNoAskComposer(dom);
  });

  it("treats a null choice as no question", () => {
    let dom!: HTMLDivElement;
    expect(() => {
      dom = render(askTurn({ questions: [{ prompt: "Deploy it?", choices: [null] }] }));
    }).not.toThrow();
    expect(dom.textContent).not.toContain("Deploy it?");
    expectNoAskComposer(dom);
  });

  it("does not show a malformed running call as listening", () => {
    const message = askTurn({ questions: [{ prompt: "Deploy it?", choices: [null] }] });
    expect(messageOrbState(message)).toBe("working");
  });

  it("says a rejected call could not be shown, with nothing to answer", () => {
    const dom = render(
      askTurn({
        questions: [{ prompt: "Deploy it?", choices: [{ label: "Yes" }, { label: "No" }] }],
        status: "complete",
        result: rejected,
      }),
      "expert",
    );
    expect(dom.textContent).toContain("The agent's question could not be shown.");
    expect(dom.textContent).not.toContain("Deploy it?");
    const labels = buttonLabels(dom);
    expect(labels).not.toContain("Yes");
    expect(labels).not.toContain("No");
  });

  it("offers no buttons for a malformed call the tool rejected", () => {
    let dom!: HTMLDivElement;
    expect(() => {
      dom = render(askTurn({ questions: malformed, status: "complete", result: rejected }));
    }).not.toThrow();
    expect(dom.textContent).not.toContain("Yes, deploy it");
  });

  it("still shows a normal question and opens the composer for it", () => {
    const dom = render(askTurn({ questions: [{ prompt: "How's school going?" }] }));
    expect(dom.textContent).toContain("How's school going?");
    expect(composer(dom).placeholder).toContain("Type your answer…");
    expect(messageOrbState(askTurn({ questions: [{ prompt: "How's school going?" }] })))
      .toBe("listening");
  });

  it("still shows a paused question as unanswered", () => {
    const dom = render(askTurn({
      questions: [{ prompt: "How's school going?" }],
      status: "complete",
      result: "[interrupted]",
    }));
    expect(dom.textContent).toContain("How's school going?");
    expect(dom.textContent).toContain("No answer recorded.");
  });
});

describe("an answered question whose tool then failed", () => {
  it("keeps the question and the recorded answer", () => {
    const dom = render(askTurn({
      questions: [{ prompt: "Which GPU?" }, { prompt: "How many?" }],
      status: "complete",
      result: JSON.stringify({ error: "Tool execution failed: database is down" }),
      answers: [
        { question: "Which GPU?", answer: "RTX 5090", is_other: true },
        { question: "How many?", answer: "1", is_other: true },
      ],
    }), "expert");
    expect(dom.textContent).toContain("Which GPU?");
    expect(dom.textContent).toContain("RTX 5090");
    expect(dom.textContent).not.toContain("could not be shown");
  });
});
