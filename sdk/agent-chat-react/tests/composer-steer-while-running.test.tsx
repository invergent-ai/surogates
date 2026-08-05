// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Typing a correction while the agent is working must steer the running
// turn, not kill it.  The harness folds a mid-turn user.message into the
// live wake at the next iteration boundary; stopping first instead pauses
// the session, compensates its sagas and destroys the sandbox pod, so the
// user pays a cold restart for a conversational correction.

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

// React installs its own value setter on the textarea, so assigning
// `.value` directly is invisible to it.  Go through the prototype setter
// and then fire the input event React actually listens for.
function typeInto(dom: HTMLElement, text: string): void {
  const textarea = dom.querySelector("textarea");
  if (!textarea) throw new Error("composer textarea not found");
  const setter = Object.getOwnPropertyDescriptor(
    HTMLTextAreaElement.prototype,
    "value",
  )?.set;
  act(() => {
    setter?.call(textarea, text);
    textarea.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

async function submit(dom: HTMLElement): Promise<void> {
  const form = dom.querySelector("form");
  if (!form) throw new Error("composer form not found");
  await act(async () => {
    form.dispatchEvent(
      new Event("submit", { bubbles: true, cancelable: true }),
    );
  });
}

describe("Composer submit while the agent is running", () => {
  it("steers instead of stopping the current turn", async () => {
    const onSend = vi.fn().mockResolvedValue(undefined);
    const onStop = vi.fn().mockResolvedValue(undefined);

    const dom = mount(
      <ChatComposer
        onSend={onSend}
        onStop={onStop}
        isRunning={true}
        viewMode="expert"
        onViewModeChange={vi.fn()}
      />,
    );

    typeInto(dom, "actually, do it the other way");
    await submit(dom);

    expect(onStop).not.toHaveBeenCalled();
    expect(onSend).toHaveBeenCalledTimes(1);
    expect(onSend.mock.calls[0][0]).toBe("actually, do it the other way");
  });

  it("still sends normally when the agent is idle", async () => {
    const onSend = vi.fn().mockResolvedValue(undefined);
    const onStop = vi.fn().mockResolvedValue(undefined);

    const dom = mount(
      <ChatComposer
        onSend={onSend}
        onStop={onStop}
        isRunning={false}
        viewMode="expert"
        onViewModeChange={vi.fn()}
      />,
    );

    typeInto(dom, "hello");
    await submit(dom);

    expect(onStop).not.toHaveBeenCalled();
    expect(onSend).toHaveBeenCalledTimes(1);
  });
});
