// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// The unread badge. The stream says "something changed", never how much,
// so the count has to come back from the list every time — counting
// nudges only ever pushed the number up.

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";
import { NO_BROWSER_ADAPTER } from "../src/adapter-context";
import { useInboxUnreadCount } from "../src/components/inbox/use-inbox-unread-count";
import type {
  AgentChatAdapter,
  AgentChatInboxItem,
  AgentChatInboxListInput,
  AgentChatInboxStreamEvent,
} from "../src/types";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT =
  true;

function inboxItem(id: number, readAt: string | null): AgentChatInboxItem {
  return {
    id,
    orgId: "org-1",
    userId: "user-1",
    sessionId: "session-1",
    sourceEventId: id,
    kind: "task_complete",
    status: "pending",
    title: `Item ${id}`,
    body: null,
    payload: {},
    actionRef: null,
    createdAt: "2026-08-01T00:00:00Z",
    updatedAt: "2026-08-01T00:00:00Z",
    readAt,
    respondedAt: null,
  };
}

function createHarness(pages: AgentChatInboxItem[][]) {
  const listInputs: AgentChatInboxListInput[] = [];
  const listeners = new Map<
    string,
    Array<(event: AgentChatInboxStreamEvent) => void>
  >();
  let closes = 0;
  let failure: Error | null = null;

  const adapter = {
    ...NO_BROWSER_ADAPTER,
    async listInbox(input: AgentChatInboxListInput = {}) {
      listInputs.push(input);
      if (failure) throw failure;
      const items = pages.length > 1 ? pages.shift() : pages[0];
      return { items: items ?? [], nextCursor: null };
    },
    openInboxStream() {
      return {
        addEventListener(
          type: string,
          listener: (event: AgentChatInboxStreamEvent) => void,
        ) {
          listeners.set(type, [...(listeners.get(type) ?? []), listener]);
        },
        close() {
          closes += 1;
        },
        onerror: null,
      };
    },
  } as unknown as AgentChatAdapter;

  return {
    adapter,
    listInputs,
    emit: (type: "item" | "snapshot", data: string) => {
      for (const listener of listeners.get(type) ?? []) listener({ data });
    },
    failWith: (error: Error | null) => {
      failure = error;
    },
    closed: () => closes,
  };
}

let root: Root | null = null;
let container: HTMLDivElement | null = null;

function Probe({ adapter }: { adapter: AgentChatAdapter }) {
  const { unreadCount } = useInboxUnreadCount(adapter);
  return <span data-testid="count">{unreadCount}</span>;
}

async function mount(adapter: AgentChatAdapter) {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => {
    root?.render(<Probe adapter={adapter} />);
    await Promise.resolve();
  });
  return () => container?.textContent;
}

afterEach(() => {
  if (root) act(() => root?.unmount());
  root = null;
  container?.remove();
  container = null;
});

describe("useInboxUnreadCount", () => {
  it("counts pending items the user has not opened", async () => {
    const harness = createHarness([
      [inboxItem(1, null), inboxItem(2, "2026-08-01T01:00:00Z")],
    ]);
    const count = await mount(harness.adapter);

    expect(count()).toBe("1");
    expect(harness.listInputs[0]?.status).toBe("pending");
  });

  it("goes back down when a nudge follows the user reading an item", async () => {
    const harness = createHarness([
      [inboxItem(1, null), inboxItem(2, null)],
      [inboxItem(1, "2026-08-01T01:00:00Z"), inboxItem(2, null)],
    ]);
    const count = await mount(harness.adapter);
    expect(count()).toBe("2");

    await act(async () => {
      harness.emit("item", JSON.stringify({ item_id: 1, kind: "task_complete" }));
      await Promise.resolve();
    });

    // Incrementing on the nudge would say 3 here, and would keep saying
    // 3 no matter what the user did next.
    expect(count()).toBe("1");
  });

  it("re-derives on the snapshot rather than trusting its length", async () => {
    const harness = createHarness([
      [inboxItem(1, null)],
      [inboxItem(1, null), inboxItem(2, null), inboxItem(3, null)],
    ]);
    const count = await mount(harness.adapter);
    expect(count()).toBe("1");

    await act(async () => {
      harness.emit("snapshot", JSON.stringify({ unread_ids: [1, 2, 3, 4, 5] }));
      await Promise.resolve();
    });

    expect(count()).toBe("3");
  });

  it("keeps the last count when a refetch fails", async () => {
    const harness = createHarness([[inboxItem(1, null), inboxItem(2, null)]]);
    const count = await mount(harness.adapter);
    expect(count()).toBe("2");

    harness.failWith(new Error("offline"));
    await act(async () => {
      harness.emit("item", JSON.stringify({ item_id: 9, kind: "task_complete" }));
      await Promise.resolve();
    });

    // A failed request is not evidence of an empty inbox.
    expect(count()).toBe("2");
  });

  it("closes the stream on unmount", async () => {
    const harness = createHarness([[]]);
    await mount(harness.adapter);

    act(() => root?.unmount());
    root = null;

    expect(harness.closed()).toBe(1);
  });
});
