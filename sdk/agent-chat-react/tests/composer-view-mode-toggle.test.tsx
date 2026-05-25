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

  it("renders both Simple and Advanced segments when onViewModeChange is supplied", () => {
    const dom = mount(
      <ChatComposer
        onSend={sendFn}
        onStop={stopFn}
        isRunning={false}
        viewMode="simple"
        onViewModeChange={vi.fn()}
      />,
    );
    const group = dom.querySelector("[role='group'][aria-label='Chat view mode']");
    expect(group).not.toBeNull();
    const buttons = group!.querySelectorAll("button");
    expect(buttons.length).toBe(2);
    expect(Array.from(buttons).map((b) => b.textContent)).toEqual([
      "Simple",
      "Advanced",
    ]);
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
      (b) => b.textContent === "Advanced",
    )!;
    act(() => expert.click());
    expect(onViewModeChange).toHaveBeenCalledWith("expert");
  });
});
