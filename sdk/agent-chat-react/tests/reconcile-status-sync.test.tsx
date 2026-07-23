// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// The reconciler must adopt the authoritative session status when the
// terminal event never reached the client: a stream that died mid-turn
// used to leave "Working on it…" on screen forever. With the status
// sync, one reconcile tick against a non-active session clears the
// running indicator via the synthetic ``session.done``.

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { NO_BROWSER_ADAPTER } from "../src/adapter-context";
import { AgentChat } from "../src/agent-chat";
import type {
  AgentChatAdapter,
  AgentChatEventStream,
  AgentChatEventType,
  AgentChatSession,
  AgentChatSseMessageEvent,
} from "../src/types";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT =
  true;

class FakeEventStream implements AgentChatEventStream {
  onerror: (() => void) | null = null;
  readonly listeners = new Map<
    AgentChatEventType,
    Array<(event: AgentChatSseMessageEvent) => void>
  >();

  addEventListener(
    type: AgentChatEventType,
    listener: (event: AgentChatSseMessageEvent) => void,
  ): void {
    const listeners = this.listeners.get(type) ?? [];
    listeners.push(listener);
    this.listeners.set(type, listeners);
  }

  close(): void {}

  emit(type: AgentChatEventType, eventId: number, data: Record<string, unknown>) {
    const event = {
      data: JSON.stringify(data),
      lastEventId: String(eventId),
    };
    for (const listener of this.listeners.get(type) ?? []) {
      listener(event);
    }
  }
}

function createAdapter(
  stream: FakeEventStream,
  status: { current: AgentChatSession["status"] },
): AgentChatAdapter {
  return {
    ...NO_BROWSER_ADAPTER,
    listSessions: vi.fn().mockResolvedValue({ sessions: [], total: 0 }),
    createSession: vi.fn(),
    getSession: vi
      .fn()
      .mockImplementation(async (input: { sessionId: string }) => ({
        id: input.sessionId,
        status: status.current,
      })),
    sendMessage: vi.fn().mockResolvedValue({ eventId: 1, status: "accepted" }),
    listSlashCommands: vi.fn().mockResolvedValue([]),
    openEventStream: () => stream,
  } as unknown as AgentChatAdapter;
}

let root: Root | null = null;
let container: HTMLDivElement | null = null;

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  if (root) act(() => root?.unmount());
  root = null;
  container?.remove();
  container = null;
  vi.useRealTimers();
});

function mount(adapter: AgentChatAdapter): HTMLDivElement {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => {
    root?.render(<AgentChat adapter={adapter} sessionId="s-1" />);
  });
  return container;
}

async function advance(ms: number): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

describe("reconciler status sync", () => {
  it("clears a stuck running indicator when the session is no longer active", async () => {
    const stream = new FakeEventStream();
    const status = { current: "active" as AgentChatSession["status"] };
    const dom = mount(createAdapter(stream, status));

    act(() => {
      stream.emit("user.message", 1, { content: "hi" });
      stream.emit("llm.request", 2, {});
    });

    // Past the 250ms indicator delay: the client believes a turn is
    // running (the terminal event was "lost" — never emitted here).
    await advance(300);
    expect(dom.textContent).toContain("Working on it");

    // The turn ends server-side but the terminal event never reaches
    // this client. The next reconcile tick sees the authoritative
    // status, nothing new drains, and the synthetic session.done must
    // clear the indicator.
    status.current = "completed";
    await advance(8100);
    expect(dom.textContent).not.toContain("Working on it");
  });

  it("keeps the indicator while the session is genuinely active", async () => {
    const stream = new FakeEventStream();
    const dom = mount(createAdapter(stream, { current: "active" }));

    act(() => {
      stream.emit("user.message", 1, { content: "hi" });
      stream.emit("llm.request", 2, {});
    });

    await advance(300);
    expect(dom.textContent).toContain("Working on it");

    await advance(4000);
    expect(dom.textContent).toContain("Working on it");
  });
});
