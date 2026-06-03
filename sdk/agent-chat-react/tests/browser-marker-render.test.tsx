/**
 * Browser lifecycle markers must surface in the chat.
 *
 * The reducer emits browser state transitions as system messages
 * (systemKind "browser_marker" / "browser_marker_warning") with content
 * like "A user took control of the browser." Neither messageToEntries
 * nor OrphanSystemMarker rendered these kinds, so the state silently
 * vanished. They should render as a small status row (warning-styled
 * for the _warning variant) in both Simple and Expert modes.
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

const noop = () => Promise.resolve();

function controlGrantedMarker(): ChatMessage {
  return {
    id: "browser-marker-9",
    role: "system",
    content: "A user took control of the browser.",
    createdAt: new Date(),
    status: "complete",
    systemKind: "browser_marker_warning",
  } as ChatMessage;
}

describe("browser lifecycle marker rendering", () => {
  for (const viewMode of ["simple", "expert"] as const) {
    it(`${viewMode}: renders a folded browser marker inside the assistant turn`, () => {
      // Folded into an assistant group (marker followed by assistant text).
      const messages: ChatMessage[] = [
        controlGrantedMarker(),
        {
          id: "asst-1",
          role: "assistant",
          content: "Resuming now.",
          createdAt: new Date(),
          status: "complete",
          turnId: "t-1",
          iterationIndex: 0,
        } as ChatMessage,
      ];
      const dom = mount(
        <ChatThread
          sessionId="s-1"
          messages={messages}
          isRunning={false}
          terminal={true}
          onSend={noop}
          onStop={noop}
          viewMode={viewMode}
        />,
      );
      expect(dom.textContent).toContain("A user took control of the browser.");
    });
  }

  it("renders an orphan browser marker (no following assistant turn)", () => {
    const dom = mount(
      <ChatThread
        sessionId="s-1"
        messages={[controlGrantedMarker()]}
        isRunning={false}
        terminal={true}
        onSend={noop}
        onStop={noop}
        viewMode="simple"
      />,
    );
    expect(dom.textContent).toContain("A user took control of the browser.");
  });
});
