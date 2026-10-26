// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Once the loaded history shows, its height still changes for a moment as
// markdown and code blocks render. The conversation's scroll must follow those
// changes without animating, so a long history does not look like it is
// scrolling; live streaming afterwards keeps gliding.

import { act, type ReactElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AgentChatAdapterProvider, NO_BROWSER_ADAPTER } from "../src/adapter-context";
import { ChatThread, useHistorySettled } from "../src/components/chat/chat-thread";
import { TooltipProvider } from "@invergent/ui-sdk/tooltip";
import type { AgentChatAdapter } from "../src/types";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT =
  true;

// The `resize` the conversation's scroll was last given, read from the one
// place it reaches the library.
const stick = vi.hoisted(() => ({ resizes: [] as unknown[] }));

vi.mock("use-stick-to-bottom", async (importOriginal) => {
  const actual = await importOriginal<typeof import("use-stick-to-bottom")>();
  const Original = actual.StickToBottom;
  const Recording = Object.assign(
    (props: Parameters<typeof Original>[0]) => {
      stick.resizes.push(props.resize);
      return Original(props);
    },
    { Content: Original.Content },
  );
  return { ...actual, StickToBottom: Recording };
});

let root: Root | null = null;
let container: HTMLDivElement | null = null;

beforeEach(() => {
  vi.useFakeTimers();
  stick.resizes.length = 0;
});

afterEach(() => {
  if (root) act(() => root?.unmount());
  root = null;
  container?.remove();
  container = null;
  vi.useRealTimers();
});

function mount(node: ReactElement) {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => {
    root?.render(node);
  });
}

describe("useHistorySettled", () => {
  let seen: boolean[] = [];

  function Probe({ loading }: { loading: boolean }) {
    seen.push(useHistorySettled(loading, 1500));
    return null;
  }

  const latest = () => seen[seen.length - 1];

  beforeEach(() => {
    seen = [];
  });

  it("starts settled for a session that never had a loading phase", () => {
    mount(<Probe loading={false} />);

    expect(seen.every(Boolean)).toBe(true);
  });

  it("is not settled while the history loads", () => {
    mount(<Probe loading />);
    act(() => {
      vi.advanceTimersByTime(5_000);
    });

    expect(latest()).toBe(false);
    expect(seen.some(Boolean)).toBe(false);
  });

  it("stays unsettled for the settle time after loading ends, then settles", () => {
    mount(<Probe loading />);
    act(() => {
      root?.render(<Probe loading={false} />);
    });
    expect(latest()).toBe(false);

    act(() => {
      vi.advanceTimersByTime(1_499);
    });
    expect(latest()).toBe(false);

    act(() => {
      vi.advanceTimersByTime(1);
    });
    expect(latest()).toBe(true);
  });

  it("goes unsettled again when another history starts loading", () => {
    mount(<Probe loading={false} />);
    expect(latest()).toBe(true);

    act(() => {
      root?.render(<Probe loading />);
    });

    expect(latest()).toBe(false);
  });
});

describe("ChatThread's scroll while the history shows", () => {
  const adapter = {
    ...NO_BROWSER_ADAPTER,
    getSession: vi.fn(),
    sendMessage: vi.fn(),
    openEventStream: vi.fn(),
  } as unknown as AgentChatAdapter;
  const noop = () => Promise.resolve();

  function thread(isLoadingHistory: boolean): ReactElement {
    return (
      <AgentChatAdapterProvider value={{ adapter, sessionId: "s-1" }}>
        <TooltipProvider>
          <ChatThread
            sessionId="s-1"
            messages={[
              { id: "u-1", role: "user", content: "hello", createdAt: new Date(), status: "complete" },
            ]}
            isRunning={false}
            terminal={false}
            isLoadingHistory={isLoadingHistory}
            onSend={noop}
            onStop={noop}
            viewMode="simple"
          />
        </TooltipProvider>
      </AgentChatAdapterProvider>
    );
  }

  const lastResize = () => stick.resizes[stick.resizes.length - 1];

  it("follows height changes without animating for the first moments, then glides", () => {
    mount(thread(true));
    act(() => {
      root?.render(thread(false));
    });
    expect(lastResize()).toBe("instant");

    act(() => {
      vi.advanceTimersByTime(1_000);
    });
    expect(lastResize()).toBe("instant");

    act(() => {
      vi.advanceTimersByTime(500);
    });
    expect(lastResize()).toBe("smooth");
  });

  it("glides from the start when the thread never loaded a history", () => {
    mount(thread(false));

    expect(stick.resizes.length).toBeGreaterThan(0);
    expect(stick.resizes.every((resize) => resize === "smooth")).toBe(true);
  });
});
