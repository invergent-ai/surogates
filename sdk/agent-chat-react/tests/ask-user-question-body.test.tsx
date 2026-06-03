/**
 * ask_user_question turns carry a full user-facing message body.
 *
 * Regression test for the bug where an assistant turn that emits both a
 * substantive ``content`` body (e.g. a proposed design the user must
 * approve) and an ``ask_user_question`` tool call rendered only the
 * first sentence of the body ("Great choices.") as a one-line
 * narration — the rest of the design was silently dropped and the user
 * was asked to approve something they could not see.
 *
 * Unlike normal tool calls (whose ``content`` is a throwaway preamble),
 * ``ask_user_question`` content is the message itself, so the renderer
 * must surface it in full, above the question widget, in both Simple
 * and Expert modes.
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

const DESIGN_BODY = [
  "Great choices. Here's the design:",
  "",
  "## Design: BTC Daily Price Checker",
  "",
  "A single Python script scheduled via Surogate cron. It fetches the",
  "current BTC/USD price from CoinGecko and emails it via Gmail SMTP.",
  "",
  "### Error Handling",
  "",
  "- API failure retries once after 10 seconds, then exits with an error.",
  "",
  "Does this design look right? Any changes before I build it?",
].join("\n");

function askTurn(): ChatMessage {
  return {
    id: "iter-ask",
    role: "assistant",
    content: DESIGN_BODY,
    createdAt: new Date(),
    // Complete status keeps useSmoothStream from char-revealing so the
    // assertion sees the whole body synchronously; the running tool is
    // what makes it an interactive ask.
    status: "complete",
    turnId: "t-1",
    iterationIndex: 0,
    toolCalls: [
      {
        id: "call_design_approve",
        toolName: "ask_user_question",
        args: JSON.stringify({
          questions: [
            {
              prompt: "Does this design work for you?",
              choices: [
                { label: "Yes, build it", description: "Proceed with implementation" },
                { label: "Changes needed", description: "I have modifications to suggest" },
              ],
              allow_other: false,
            },
          ],
        }),
        status: "running",
      },
    ],
  } as ChatMessage;
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

function expectFullBody(dom: HTMLDivElement) {
  // The question widget renders.
  expect(dom.textContent).toContain("Does this design work for you?");
  // ...and so does the full design body, not just the first sentence.
  expect(dom.textContent).toContain("BTC Daily Price Checker");
  expect(dom.textContent).toContain("CoinGecko");
  expect(dom.textContent).toContain("Error Handling");
  expect(dom.textContent).toContain("Does this design look right?");
}

const noop = () => Promise.resolve();

describe("ask_user_question turn body rendering", () => {
  it("Simple mode renders the full body above the question widget", () => {
    const dom = mount(
      <ChatThread
        sessionId="s-1"
        messages={[askTurn()]}
        isRunning={true}
        terminal={false}
        onSend={noop}
        onStop={noop}
        viewMode="simple"
      />,
    );
    expectFullBody(dom);
  });

  it("Expert mode renders the full body above the question widget", () => {
    const dom = mount(
      <ChatThread
        sessionId="s-1"
        messages={[askTurn()]}
        isRunning={true}
        terminal={false}
        onSend={noop}
        onStop={noop}
        viewMode="expert"
      />,
    );
    expectFullBody(dom);
  });

  it("Expert mode shows the full body immediately while the ask turn is still streaming", () => {
    // A parked ask keeps status "streaming"; the body must render in
    // full synchronously (no char-by-char reveal / perpetual rAF) since
    // it has already fully arrived by the time the widget appears.
    const streamingAsk = { ...askTurn(), status: "streaming" } as ChatMessage;
    const dom = mount(
      <ChatThread
        sessionId="s-1"
        messages={[streamingAsk]}
        isRunning={true}
        terminal={false}
        onSend={noop}
        onStop={noop}
        viewMode="expert"
      />,
    );
    expectFullBody(dom);
  });
});
