/**
 * Simple/Expert toggle in the composer tools row.
 *
 * Renders only when an onViewModeChange callback is supplied; shows the
 * current mode as aria-pressed; clicking the other segment fires the
 * callback with the new mode.
 */
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
    listSlashCommands: vi.fn().mockResolvedValue({ commands: [] }),
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


describe("Composer view-mode toggle", () => {
  const sendFn = () => Promise.resolve();
  const stopFn = () => Promise.resolve();

  it("renders no toggle when onViewModeChange is absent", () => {
    const dom = mount(
      <ChatComposer onSend={sendFn} onStop={stopFn} isRunning={false} />,
    );
    expect(dom.querySelector("[role='group'][aria-label='Chat view mode']"))
      .toBeNull();
  });

  it("renders every view segment when onViewModeChange is supplied", () => {
    const dom = mount(
      <ChatComposer
        onSend={sendFn}
        onStop={stopFn}
        isRunning={false}
        viewMode="simple"
        onViewModeChange={vi.fn()}
        whiteboardEnabled
      />,
    );
    const group = dom.querySelector("[role='group'][aria-label='Chat view mode']");
    expect(group).not.toBeNull();
    const buttons = group!.querySelectorAll("button");
    expect(buttons.length).toBe(3);
    // The accessible name is the same at every width; the icon (phone) and
    // the word (cursor) are two renderings of it, and both are in the DOM
    // with only a media query deciding which one shows.
    expect(Array.from(buttons).map((b) => b.getAttribute("aria-label"))).toEqual([
      "Simple view",
      "Advanced view",
      "Whiteboard view",
    ]);
    expect(Array.from(buttons).map((b) => b.textContent?.trim())).toEqual([
      "Simple",
      "Advanced",
      "Whiteboard",
    ]);
    for (const button of buttons) {
      const word = button.querySelector("span");
      const icon = button.querySelector("svg");
      expect(word?.className).toContain("hidden");
      expect(word?.className).toContain("md:inline");
      expect(icon?.getAttribute("class")).toContain("md:hidden");
    }
  });

  it("hides the whiteboard segment by default", () => {
    // Offered only on a session created as a board, on an agent that
    // still has the capability -- the harness loads whiteboard_draw on
    // exactly those two facts, so anywhere else the segment would open a
    // canvas the agent cannot draw on.
    const dom = mount(
      <ChatComposer
        onSend={sendFn}
        onStop={stopFn}
        isRunning={false}
        viewMode="simple"
        onViewModeChange={vi.fn()}
      />,
    );
    const group = dom.querySelector("[role='group'][aria-label='Chat view mode']")!;
    const labels = Array.from(group.querySelectorAll("button")).map((b) =>
      b.getAttribute("aria-label"),
    );
    expect(labels).toEqual(["Simple view", "Advanced view"]);
  });

  it("keeps the segments pill-shaped — no squared inner edges", () => {
    const dom = mount(
      <ChatComposer
        onSend={sendFn}
        onStop={stopFn}
        isRunning={false}
        viewMode="simple"
        onViewModeChange={vi.fn()}
      />,
    );
    const group = dom.querySelector("[role='group'][aria-label='Chat view mode']")!;
    // ButtonGroup squares off the inner edges of everything it holds, which
    // leaves each segment outlined inside the track's own outline. A plain
    // track keeps both halves round; nothing may reintroduce the group.
    expect(group.getAttribute("data-slot")).not.toBe("button-group");
    for (const button of group.querySelectorAll("button")) {
      expect(button.className).toContain("rounded-full");
      expect(button.className).not.toMatch(/rounded-[lr]-none/);
      expect(button.className).toContain("border-0");
    }
  });

  it("shows the current mode as aria-pressed", () => {
    const dom = mount(
      <ChatComposer
        onSend={sendFn}
        onStop={stopFn}
        isRunning={false}
        viewMode="expert"
        onViewModeChange={vi.fn()}
      />,
    );
    const group = dom.querySelector("[role='group'][aria-label='Chat view mode']")!;
    const [simple, expert] = Array.from(group.querySelectorAll("button"));
    expect(simple.getAttribute("aria-pressed")).toBe("false");
    expect(expert.getAttribute("aria-pressed")).toBe("true");
  });

  it("fires onViewModeChange when the other segment is clicked", () => {
    const onViewModeChange = vi.fn();
    const dom = mount(
      <ChatComposer
        onSend={sendFn}
        onStop={stopFn}
        isRunning={false}
        viewMode="simple"
        onViewModeChange={onViewModeChange}
      />,
    );
    const group = dom.querySelector("[role='group'][aria-label='Chat view mode']")!;
    const expert = Array.from(group.querySelectorAll("button")).find(
      (b) => b.getAttribute("aria-label") === "Advanced view",
    )!;
    act(() => expert.click());
    expect(onViewModeChange).toHaveBeenCalledWith("expert");
  });
});
