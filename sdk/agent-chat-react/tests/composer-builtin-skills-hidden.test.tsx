// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Platform built-ins (docx, pdf, pptx, xlsx, kanban, …) come back from
// listSlashCommands() flagged isBuiltin.  The composer hides these from
// the slash menu so it lists only the skills a tenant actually authored
// or attached -- the built-ins are noise the user can't manage.

import { act, type ReactElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AgentChatAdapterProvider, NO_BROWSER_ADAPTER } from "../src/adapter-context";
import { ChatComposer } from "../src/components/chat/chat-composer";
import { TooltipProvider } from "../src/components/ui/tooltip";
import type { AgentChatAdapter, AgentChatSlashCommand } from "../src/types";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT =
  true;

const USER_SKILL: AgentChatSlashCommand = {
  value: "/web-search",
  label: "/web-search",
  description: "Search the web",
};
const BUILTIN_SKILL: AgentChatSlashCommand = {
  value: "/pdf",
  label: "/pdf",
  description: "Work with PDF files",
  isBuiltin: true,
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
    listSlashCommands: vi.fn().mockResolvedValue([USER_SKILL, BUILTIN_SKILL]),
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

async function typeInto(input: HTMLInputElement, value: string): Promise<void> {
  const setter = Object.getOwnPropertyDescriptor(
    Object.getPrototypeOf(input),
    "value",
  )?.set;
  await act(async () => {
    setter?.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
    await Promise.resolve();
    await Promise.resolve();
  });
}

describe("Composer hides built-in skills", () => {
  const sendFn = () => Promise.resolve();
  const stopFn = () => Promise.resolve();

  it("drops isBuiltin skills but keeps tenant skills in the Skills menu", async () => {
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
    await typeInto(commandInput(), "");

    const labels = menuItems();
    expect(labels.some((l) => l.includes("/web-search"))).toBe(true);
    expect(labels.some((l) => l.includes("/pdf"))).toBe(false);
  });
});
