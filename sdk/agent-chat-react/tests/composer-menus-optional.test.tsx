// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// An embedder can switch the composer's Commands and Skills menus off.
// With both off there is no toggle for either, typing "/" opens no menu,
// and a message that starts with "/" is sent as typed. Left at their
// defaults, both toggles and the "/" menu show as before.

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { NO_BROWSER_ADAPTER } from "../src/adapter-context";
import { AgentChat, type AgentChatProps } from "../src/agent-chat";
import type { AgentChatAdapter, AgentChatEventStream } from "../src/types";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT =
  true;

const SKILL = {
  value: "/review",
  label: "/review",
  description: "Review the current work",
};

function adapterStub() {
  const stream: AgentChatEventStream = {
    onerror: null,
    addEventListener: vi.fn(),
    close: vi.fn(),
  };
  return {
    ...NO_BROWSER_ADAPTER,
    listSessions: vi.fn().mockResolvedValue({ sessions: [], total: 0 }),
    createSession: vi.fn(),
    getSession: vi.fn().mockResolvedValue({ id: "s-1", status: "active" }),
    sendMessage: vi.fn().mockResolvedValue({ eventId: 1, status: "accepted" }),
    openEventStream: vi.fn(() => stream),
    listSlashCommands: vi.fn().mockResolvedValue([SKILL]),
  } satisfies Partial<AgentChatAdapter>;
}

let root: Root | null = null;
let container: HTMLDivElement | null = null;

afterEach(() => {
  if (root) act(() => root?.unmount());
  root = null;
  container?.remove();
  container = null;
});

async function mount(
  adapter: ReturnType<typeof adapterStub>,
  props: Partial<AgentChatProps> = {},
): Promise<HTMLDivElement> {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => {
    root?.render(
      <AgentChat
        adapter={adapter as unknown as AgentChatAdapter}
        sessionId="s-1"
        {...props}
      />,
    );
    await Promise.resolve();
  });
  return container;
}

function toggleLabels(dom: HTMLElement): string[] {
  return Array.from(
    dom.querySelectorAll<HTMLButtonElement>("[data-composer-menu-toggle]"),
  ).map((b) => (b.textContent ?? "").trim());
}

function textarea(dom: HTMLElement): HTMLTextAreaElement {
  const el = dom.querySelector<HTMLTextAreaElement>("textarea");
  if (!el) throw new Error("composer textarea not rendered");
  return el;
}

// React installs its own value setter on the textarea; go through the
// prototype's so the change reaches its onChange.
async function type(dom: HTMLElement, value: string): Promise<void> {
  const el = textarea(dom);
  const setter = Object.getOwnPropertyDescriptor(
    HTMLTextAreaElement.prototype,
    "value",
  )?.set;
  await act(async () => {
    setter?.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
    // Flush the skills fetch a "/" menu would start.
    await Promise.resolve();
    await Promise.resolve();
  });
}

// cmdk renders the menu into a Radix Popover portalled to document.body.
function slashMenu(): Element | null {
  return document.querySelector("[data-slot='command-input']");
}

function menuItems(): string[] {
  return Array.from(document.querySelectorAll("[cmdk-item]")).map((el) =>
    (el.textContent ?? "").trim(),
  );
}

describe("composer menus an embedder can switch off", () => {
  it("draws the Commands and Skills toggles and opens the / menu by default", async () => {
    const adapter = adapterStub();
    const dom = await mount(adapter);

    expect(toggleLabels(dom)).toEqual(
      expect.arrayContaining(["Commands", "Skills"]),
    );
    expect(textarea(dom).placeholder).toContain("/ for commands");

    await type(dom, "/");

    expect(slashMenu()).not.toBeNull();
    const items = menuItems();
    expect(items.some((l) => l.includes("/clear"))).toBe(true);
    expect(items.some((l) => l.includes("/review"))).toBe(true);
  });

  it("draws neither toggle and opens no / menu with both off, and sends the / message as typed", async () => {
    const adapter = adapterStub();
    const dom = await mount(adapter, {
      commandsEnabled: false,
      skillsEnabled: false,
    });

    const labels = toggleLabels(dom);
    expect(labels).not.toContain("Commands");
    expect(labels).not.toContain("Skills");
    expect(textarea(dom).placeholder).not.toContain("/ for commands");

    await type(dom, "/");

    expect(slashMenu()).toBeNull();
    expect(document.body.textContent).not.toContain("No skills attached");
    expect(document.body.textContent).not.toContain("Skills & Tools");
    expect(adapter.listSlashCommands).not.toHaveBeenCalled();

    await type(dom, "/hello there");
    expect(slashMenu()).toBeNull();
    await act(async () => {
      textarea(dom).dispatchEvent(
        new KeyboardEvent("keydown", { key: "Enter", bubbles: true }),
      );
      await Promise.resolve();
    });

    expect(adapter.sendMessage).toHaveBeenCalledTimes(1);
    expect(adapter.sendMessage.mock.calls[0]?.[0]).toMatchObject({
      sessionId: "s-1",
      content: "/hello there",
    });
  });

  it("lists no skills, and fetches none, with skills off", async () => {
    const adapter = adapterStub();
    const dom = await mount(adapter, { skillsEnabled: false });

    expect(toggleLabels(dom)).toContain("Commands");
    expect(toggleLabels(dom)).not.toContain("Skills");

    await type(dom, "/");

    expect(slashMenu()).not.toBeNull();
    const items = menuItems();
    expect(items.some((l) => l.includes("/clear"))).toBe(true);
    expect(items.some((l) => l.includes("/review"))).toBe(false);
    expect(adapter.listSlashCommands).not.toHaveBeenCalled();
  });

  it("lists no commands with commands off", async () => {
    const adapter = adapterStub();
    const dom = await mount(adapter, { commandsEnabled: false });

    expect(toggleLabels(dom)).not.toContain("Commands");
    expect(toggleLabels(dom)).toContain("Skills");

    await type(dom, "/");

    expect(slashMenu()).not.toBeNull();
    const items = menuItems();
    expect(items.some((l) => l.includes("/review"))).toBe(true);
    expect(items.some((l) => l.includes("/clear"))).toBe(false);
    expect(items.some((l) => l.includes("/goal"))).toBe(false);
  });
});
