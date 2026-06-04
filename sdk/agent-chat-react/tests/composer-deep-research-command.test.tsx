// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// The composer's builtin slash menu gates the ``/deep-research`` entry
// on the ``deepResearchEnabled`` prop.  Studio reads the flag from the
// agent record and passes it through; without the flag the command
// would dispatch a delegate_task to a sub-agent that isn't in the
// published bundle.

import { act, type ReactElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AgentChatAdapterProvider, NO_BROWSER_ADAPTER } from "../src/adapter-context";
import { ChatComposer } from "../src/components/chat/chat-composer";
import { TooltipProvider } from "../src/components/ui/tooltip";
import type { AgentChatAdapter } from "../src/types";

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
    listSlashCommands: vi.fn().mockResolvedValue([]),
    listScheduledWork: undefined,
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
      <AgentChatAdapterProvider
        value={{ adapter: adapterStub(), sessionId: "s-1" }}
      >
        <TooltipProvider>{node}</TooltipProvider>
      </AgentChatAdapterProvider>,
    );
  });
  return container;
}

// The Commands button is only rendered in expert mode.  Clicking it
// opens the slash menu in "commands" scope, which is where the builtin
// list (including the gated /deep-research entry) is rendered.
function openCommandsMenu(dom: HTMLElement): void {
  const button = Array.from(dom.querySelectorAll("button")).find(
    (b) => b.textContent?.trim() === "Commands",
  );
  if (!button) {
    throw new Error("Commands button not found");
  }
  act(() => {
    button.click();
  });
}

// cmdk renders the menu into a Radix Popover -- its content portals to
// document.body, not inside the composer's container.
function menuItems(): string[] {
  return Array.from(
    document.querySelectorAll("[role='option'], [cmdk-item]"),
  ).map((el) => (el.textContent ?? "").trim());
}

describe("Composer builtin /deep-research suggestion gating", () => {
  const sendFn = () => Promise.resolve();
  const stopFn = () => Promise.resolve();

  it("omits /deep-research from the menu by default", () => {
    const dom = mount(
      <ChatComposer
        onSend={sendFn}
        onStop={stopFn}
        isRunning={false}
        viewMode="expert"
        onViewModeChange={vi.fn()}
      />,
    );
    openCommandsMenu(dom);
    const labels = menuItems();
    expect(labels.length).toBeGreaterThan(0);
    expect(labels.some((l) => l.includes("/deep-research"))).toBe(false);
    // Sanity: the other builtins are still there.
    expect(labels.some((l) => l.includes("/clear"))).toBe(true);
  });

  it("shows /deep-research when deepResearchEnabled is true", () => {
    const dom = mount(
      <ChatComposer
        onSend={sendFn}
        onStop={stopFn}
        isRunning={false}
        viewMode="expert"
        onViewModeChange={vi.fn()}
        deepResearchEnabled
      />,
    );
    openCommandsMenu(dom);
    const labels = menuItems();
    expect(labels.some((l) => l.includes("/deep-research"))).toBe(true);
  });
});
