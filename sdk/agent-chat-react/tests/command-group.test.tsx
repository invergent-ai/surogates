// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Simple mode folds adjacent commands into one group: "Ran N commands"
// once done, the running command's label while the run goes on. Anything
// said between two commands ends the run.

import { act, type ReactElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AgentChatAdapterProvider, NO_BROWSER_ADAPTER } from "../src/adapter-context";
import { ChatThread } from "../src/components/chat/chat-thread";
import { TooltipProvider } from "../src/components/ui/tooltip";
import type { AgentChatAdapter, ChatMessage, ToolCallInfo } from "../src/types";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let root: Root | null = null;
let container: HTMLDivElement | null = null;

afterEach(() => {
  if (root) act(() => root?.unmount());
  root = null;
  container?.remove();
  container = null;
});

function render(messages: ChatMessage[], isRunning = false): HTMLDivElement {
  const adapter = {
    ...NO_BROWSER_ADAPTER,
    openEventStream: vi.fn(() => ({ addEventListener: vi.fn(), close: vi.fn(), onerror: null })),
  } as unknown as AgentChatAdapter;
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  const node: ReactElement = (
    <ChatThread
      sessionId="s-1"
      messages={messages}
      isRunning={isRunning}
      terminal={!isRunning}
      onSend={() => Promise.resolve()}
      onStop={() => Promise.resolve()}
      viewMode="simple"
    />
  );
  act(() => {
    root?.render(
      <AgentChatAdapterProvider value={{ adapter, sessionId: "s-1" }}>
        <TooltipProvider>{node}</TooltipProvider>
      </AgentChatAdapterProvider>,
    );
  });
  return container;
}

function command(id: string, summary: string, over: Partial<ChatMessage> = {}): ChatMessage {
  const call: ToolCallInfo = {
    id: `${id}-c`,
    toolName: "web_extract",
    args: "{}",
    status: "complete",
    result: JSON.stringify({ content: `page of ${id}` }),
  };
  return {
    id,
    role: "assistant",
    content: "",
    createdAt: new Date(),
    status: "complete",
    toolCalls: [call],
    iterationSummary: { iterationIndex: 0, summary, toolCallIds: [call.id], startedAt: "", endedAt: "" },
    ...over,
  };
}

const answer: ChatMessage = {
  id: "final",
  role: "assistant",
  content: "Here is what I found.",
  createdAt: new Date(),
  status: "complete",
};

function header(dom: HTMLElement, text: string): HTMLButtonElement | undefined {
  return [...dom.querySelectorAll<HTMLButtonElement>("button")].find((b) =>
    b.textContent?.includes(text),
  );
}

describe("Simple-mode command groups", () => {
  it("folds a finished run under 'Ran N commands' and opens it on click", () => {
    const dom = render([command("a", "Pulled the front page"), command("b", "Read the lead story"), answer]);
    const ran = header(dom, "Ran 2 commands");
    expect(ran?.getAttribute("aria-expanded")).toBe("false");
    expect(dom.textContent).not.toContain("Pulled the front page");

    act(() => ran!.click());
    expect(dom.textContent).toContain("Pulled the front page");
    expect(dom.textContent).toContain("Read the lead story");
  });

  it("ends a run where the agent says something between two commands", () => {
    const dom = render([
      command("a", "Pulled the front page"),
      command("b", "Read the lead story", {
        content: "<next_action>Checking the second source.</next_action>",
      }),
      answer,
    ]);
    expect(dom.textContent).not.toContain("Ran ");
    expect(dom.textContent).toContain("Pulled the front page");
    expect(dom.textContent).toContain("Checking the second source.");
    expect(dom.textContent).toContain("Read the lead story");
  });

  it("keeps a running run open under the running command's label", () => {
    const live = command("b", "unused while live", {
      status: "streaming",
      iterationSummary: undefined,
      toolCalls: [{ id: "b-c", toolName: "web_extract", args: "{}", status: "running" }],
    });
    const dom = render([command("a", "Pulled the front page"), live], true);
    expect(dom.textContent).not.toContain("Ran ");
    const running = header(dom, "Running");
    expect(running?.getAttribute("aria-expanded")).toBe("true");
    // The finished command shows, and so does the running one, as rows.
    const rows = dom.querySelector('[data-testid="command-group-rows"]');
    expect(rows?.textContent).toContain("Pulled the front page");
    expect(rows?.textContent).toContain("Running");
  });
});
