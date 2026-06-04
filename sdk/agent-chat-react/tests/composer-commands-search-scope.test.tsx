// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// The composer's expert-mode buttons each open the shared command
// palette in a single scope: Commands -> builtins only, Skills ->
// app-provided skills only.  Searching inside the Commands popover must
// keep that scope: typing must not pull skills into the results.
//
// Regression guard: the Commands input is the controlled palette input
// that mirrors its query back into the textarea as "/<query>".  An
// effect that opens the unified menu on a leading "/" used to fire on
// that mirror write and silently widen the scope to "all", leaking
// skills into a Commands-only search.

import { act, type ReactElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AgentChatAdapterProvider, NO_BROWSER_ADAPTER } from "../src/adapter-context";
import { ChatComposer } from "../src/components/chat/chat-composer";
import { TooltipProvider } from "../src/components/ui/tooltip";
import type { AgentChatAdapter, AgentChatSlashCommand } from "../src/types";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT =
  true;

const SKILL: AgentChatSlashCommand = {
  value: "/web-search",
  label: "/web-search",
  description: "Search the web",
};

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
    listSlashCommands: vi.fn().mockResolvedValue([SKILL]),
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

function clickButton(dom: HTMLElement, label: string): void {
  const button = Array.from(dom.querySelectorAll("button")).find(
    (b) => b.textContent?.trim() === label,
  );
  if (!button) throw new Error(`${label} button not found`);
  act(() => button.click());
}

// cmdk renders the menu into a Radix Popover that portals to document.body.
function menuItems(): string[] {
  return Array.from(
    document.querySelectorAll("[role='option'], [cmdk-item]"),
  ).map((el) => (el.textContent ?? "").trim());
}

function commandInput(): HTMLInputElement {
  const input = document.querySelector<HTMLInputElement>(
    "[data-slot='command-input']",
  );
  if (!input) throw new Error("command input not found");
  return input;
}

// Drive a controlled React input the way a user keystroke would: set the
// value through the prototype setter (bypassing React's instance-level
// value tracker) then dispatch the input event cmdk listens for.
async function typeInto(input: HTMLInputElement, value: string): Promise<void> {
  const setter = Object.getOwnPropertyDescriptor(
    Object.getPrototypeOf(input),
    "value",
  )?.set;
  await act(async () => {
    setter?.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
    // Flush the best-effort listSlashCommands() fetch that a scope widen
    // would trigger, so a leak would actually render before we assert.
    await Promise.resolve();
    await Promise.resolve();
  });
}

describe("Composer Commands search scope", () => {
  const sendFn = () => Promise.resolve();
  const stopFn = () => Promise.resolve();

  it("keeps skills out of the Commands search results", async () => {
    const dom = mount(
      <ChatComposer
        onSend={sendFn}
        onStop={stopFn}
        isRunning={false}
        viewMode="expert"
        onViewModeChange={vi.fn()}
      />,
    );

    clickButton(dom, "Commands");
    await typeInto(commandInput(), "search");

    // The query registered (mirror write still works) ...
    expect(commandInput().value).toBe("search");
    // ... but the skill must not bleed into a Commands-only search.
    const labels = menuItems();
    expect(labels.some((l) => l.includes("/web-search"))).toBe(false);
  });

  it("still shows skills in the Skills search results", async () => {
    const dom = mount(
      <ChatComposer
        onSend={sendFn}
        onStop={stopFn}
        isRunning={false}
        viewMode="expert"
        onViewModeChange={vi.fn()}
      />,
    );

    clickButton(dom, "Skills");
    // Skills input is uncontrolled; flush the fetch then assert presence.
    await typeInto(commandInput(), "search");

    const labels = menuItems();
    expect(labels.some((l) => l.includes("/web-search"))).toBe(true);
  });
});