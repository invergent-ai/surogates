// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Rendering rules for a one-question ask_user_question: it is the
// agent talking, so it carries no form chrome, its choices are quick
// replies rather than a radio group, and a prompt the agent already
// wrote out in prose is not echoed underneath it.

import { act, type ReactElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AgentChatAdapterProvider, NO_BROWSER_ADAPTER } from "../src/adapter-context";
import { ChatThread } from "../src/components/chat/chat-thread";
import { TooltipProvider } from "../src/components/ui/tooltip";
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
}: {
  content?: string;
  questions: unknown[];
  status?: "running" | "complete";
  answers?: { question: string; answer: string; is_other: boolean }[];
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
        ...(status === "complete" ? { result: "{}" } : {}),
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

describe("conversational ask rendering", () => {
  it("shows the question with no card, tab header or Submit button", () => {
    const dom = render(
      askTurn({ questions: [{ prompt: "How's school going?" }] }),
    );

    expect(dom.textContent).toContain("How's school going?");
    expect(dom.textContent).not.toContain("Question 1");
    expect(dom.textContent).not.toContain("Esc to cancel");
    const labels = [...dom.querySelectorAll("button")].map((b) => b.textContent);
    expect(labels).not.toContain("Submit");
  });

  it("does not echo a prompt the agent already wrote in its message body", () => {
    // Agents routinely close their prose with the same sentence they
    // pass as the prompt; printing both reads as a stutter.
    const prompt = "What would feel like real progress to you?";
    const dom = render(
      askTurn({
        content: `You said you want the foundations. ${prompt}`,
        questions: [{ prompt }],
      }),
    );

    const occurrences = (dom.textContent ?? "").split(prompt).length - 1;
    expect(occurrences).toBe(1);
  });

  it("still shows the prompt when the body says something else", () => {
    const dom = render(
      askTurn({
        content: "Good. Let's build on that.",
        questions: [{ prompt: "What happens at 0 degrees?" }],
      }),
    );

    expect(dom.textContent).toContain("Let's build on that.");
    expect(dom.textContent).toContain("What happens at 0 degrees?");
  });

  it("offers choices as quick replies that submit on click", () => {
    const dom = render(
      askTurn({
        questions: [
          {
            prompt: "How often can you meet?",
            choices: [{ label: "Three a week" }, { label: "Twice a week" }],
          },
        ],
      }),
    );

    const chip = [...dom.querySelectorAll("button")].find(
      (b) => b.textContent === "Three a week",
    );
    expect(chip).toBeDefined();

    act(() => {
      chip?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });

    expect(submitSpy).toHaveBeenCalledWith({
      sessionId: "s-1",
      toolCallId: "call_ask",
      responses: [
        {
          question: "How often can you meet?",
          answer: "Three a week",
          is_other: false,
        },
      ],
    });
  });

  it("keeps the question visible once answered, without a status banner", () => {
    const dom = render(
      askTurn({
        questions: [{ prompt: "How's school going?" }],
        status: "complete",
        answers: [
          {
            question: "How's school going?",
            answer: "good, i like computers",
            is_other: false,
          },
        ],
      }),
    );

    expect(dom.textContent).toContain("How's school going?");
    expect(dom.textContent).not.toContain("Clarification answered");
    expect(dom.textContent).not.toContain("(other)");
  });

  it("notes quietly when the question ended with no answer", () => {
    // The 30-minute tool wait elapsed, or the session was paused while
    // the question was open.
    const dom = render(
      askTurn({
        questions: [{ prompt: "How's school going?" }],
        status: "complete",
      }),
    );

    expect(dom.textContent).toContain("No answer recorded.");
    expect(dom.textContent).not.toContain("Clarification cancelled");
  });
});

describe("batch ask keeps its form chrome", () => {
  const batch = () =>
    askTurn({
      questions: [{ prompt: "Which approach?" }, { prompt: "Which region?" }],
      status: "complete",
      answers: [
        { question: "Which approach?", answer: "Cron", is_other: false },
        { question: "Which region?", answer: "frankfurt", is_other: true },
      ],
    });

  it("hides the off-menu marker from end users in simple mode", () => {
    const dom = render(batch(), "simple");

    expect(dom.textContent).toContain("Clarification answered");
    expect(dom.textContent).toContain("frankfurt");
    expect(dom.textContent).not.toContain("(other)");
  });

  it("shows the off-menu marker to operators in expert mode", () => {
    // "The offered options did not fit" is review signal, useful to
    // whoever tunes the agent's choices.
    const dom = render(batch(), "expert");

    expect(dom.textContent).toContain("(other)");
  });
});
