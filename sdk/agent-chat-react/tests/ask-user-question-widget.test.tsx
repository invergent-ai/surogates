// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// UX contract of the ask_user_question widget: picking a choice
// auto-advances to the next unanswered question, "Other" waits for
// typed input (Enter commits), and the footer carries one visible
// primary action — "Next question →" while questions remain, an amber
// "Submit" once everything is answered.

import { act, type ReactElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  AgentChatAdapterProvider,
  NO_BROWSER_ADAPTER,
} from "../src/adapter-context";
import { AskUserQuestionToolBlock } from "../src/components/chat/tools/ask-user-question-tool";
import type { AgentChatAdapter, ToolCallInfo } from "../src/types";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT =
  true;

let root: Root | null = null;
let container: HTMLDivElement | null = null;

afterEach(() => {
  if (root) act(() => root?.unmount());
  root = null;
  container?.remove();
  container = null;
});

function askToolCall(): ToolCallInfo {
  return {
    id: "tc-1",
    toolName: "ask_user_question",
    args: JSON.stringify({
      questions: [
        {
          prompt: "Where should I save the file?",
          choices: [
            { label: "Repo-style guide", description: "A project file" },
            { label: "Root-level file", description: "Simple single file" },
          ],
          allow_other: true,
        },
        {
          prompt: "What tone should it use?",
          choices: [
            { label: "Formal" },
            { label: "Casual" },
          ],
          allow_other: false,
        },
      ],
    }),
    status: "running",
  };
}

function mount(node: ReactElement, adapter: AgentChatAdapter): HTMLDivElement {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => {
    root?.render(
      <AgentChatAdapterProvider value={{ adapter, sessionId: "s-1" }}>
        {node}
      </AgentChatAdapterProvider>,
    );
  });
  return container;
}

function adapterStub(): AgentChatAdapter {
  return {
    ...NO_BROWSER_ADAPTER,
    submitAskUserQuestionResponse: vi.fn().mockResolvedValue({ eventId: 1 }),
    pauseSession: vi.fn().mockResolvedValue(undefined),
  } as unknown as AgentChatAdapter;
}

function clickByText(dom: HTMLElement, text: string): void {
  const target = [...dom.querySelectorAll("button")].find((b) =>
    b.textContent?.includes(text),
  );
  if (!target) throw new Error(`No button containing "${text}"`);
  act(() => {
    target.click();
  });
}

describe("ask_user_question widget UX", () => {
  it("auto-advances to the next unanswered question when a choice is picked", () => {
    const dom = mount(<AskUserQuestionToolBlock tc={askToolCall()} />, adapterStub());
    expect(dom.textContent).toContain("Where should I save the file?");

    clickByText(dom, "Repo-style guide");

    // The widget moved on to question 2 by itself.
    expect(dom.textContent).toContain("What tone should it use?");
    // And the footer's primary action became Submit-ready once the
    // second answer lands.
    expect(dom.textContent).toContain("1 of 2 answered");
  });

  it("shows a Next question action while questions remain, then an enabled Submit", () => {
    const dom = mount(<AskUserQuestionToolBlock tc={askToolCall()} />, adapterStub());
    expect(
      [...dom.querySelectorAll("button")].some((b) =>
        b.textContent?.includes("Next question"),
      ),
    ).toBe(true);

    clickByText(dom, "Repo-style guide");
    clickByText(dom, "Formal");

    const submit = [...dom.querySelectorAll("button")].find((b) =>
      b.textContent?.trim() === "Submit",
    );
    expect(submit).toBeDefined();
    expect(submit!.disabled).toBe(false);
    expect(dom.textContent).toContain("2 of 2 answered");
  });

  it("submits every answer through the adapter", async () => {
    const adapter = adapterStub();
    const dom = mount(<AskUserQuestionToolBlock tc={askToolCall()} />, adapter);

    clickByText(dom, "Repo-style guide");
    clickByText(dom, "Formal");
    await act(async () => {
      [...dom.querySelectorAll("button")]
        .find((b) => b.textContent?.trim() === "Submit")!
        .click();
    });

    expect(adapter.submitAskUserQuestionResponse).toHaveBeenCalledWith({
      sessionId: "s-1",
      toolCallId: "tc-1",
      responses: [
        {
          question: "Where should I save the file?",
          answer: "Repo-style guide",
          is_other: false,
        },
        { question: "What tone should it use?", answer: "Formal", is_other: false },
      ],
    });
  });

  it("does not auto-advance for 'Other' until Enter commits the typed answer", () => {
    const dom = mount(<AskUserQuestionToolBlock tc={askToolCall()} />, adapterStub());

    const otherInput = dom.querySelector<HTMLInputElement>(
      'input[placeholder="Type your answer…"]',
    )!;
    act(() => {
      otherInput.focus();
    });
    // Still on question 1 — a focused empty "Other" is not an answer.
    expect(dom.textContent).toContain("Where should I save the file?");

    act(() => {
      const setter = Object.getOwnPropertyDescriptor(
        HTMLInputElement.prototype,
        "value",
      )!.set!;
      setter.call(otherInput, "a custom place");
      otherInput.dispatchEvent(new Event("input", { bubbles: true }));
    });
    expect(dom.textContent).toContain("Where should I save the file?");

    act(() => {
      otherInput.dispatchEvent(
        new KeyboardEvent("keydown", { key: "Enter", bubbles: true }),
      );
    });
    expect(dom.textContent).toContain("What tone should it use?");
  });
});
