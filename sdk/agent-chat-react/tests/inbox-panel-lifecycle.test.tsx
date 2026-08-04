// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// What the inbox owes the user beyond rendering a list: that a new item
// shows up without a reload, that history is reachable, that a failed
// action says so, and that a question the agent stopped waiting for
// cannot be answered into the void.

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";
import { NON_INBOX_ADAPTER } from "./inbox-adapter-stub";
import { InboxPanel } from "../src/components/inbox/inbox-panel";
import type {
  AgentChatAdapter,
  AgentChatInboxItem,
  AgentChatInboxListInput,
  AgentChatInboxStreamEvent,
} from "../src/types";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT =
  true;

function inboxItem(
  input: Partial<AgentChatInboxItem> & { id: number },
): AgentChatInboxItem {
  const createdAt = new Date().toISOString();
  return {
    orgId: "org-1",
    userId: "user-1",
    sessionId: "session-1",
    sourceEventId: input.id,
    kind: "task_complete",
    status: "pending",
    title: `Item ${input.id}`,
    body: "Body",
    payload: {},
    actionRef: null,
    createdAt,
    updatedAt: createdAt,
    readAt: null,
    respondedAt: null,
    ...input,
  };
}

interface Harness {
  adapter: AgentChatAdapter;
  listInputs: AgentChatInboxListInput[];
  emit: (type: "item" | "snapshot", data: string) => void;
  closed: () => number;
}

function createHarness(
  items: AgentChatInboxItem[],
  overrides: Partial<AgentChatAdapter> = {},
): Harness {
  const listInputs: AgentChatInboxListInput[] = [];
  // Listeners belong to one stream instance, and a closed stream
  // delivers nothing — otherwise a reopened subscription would look like
  // it still carries the handlers of the one it replaced.
  interface Subscription {
    listeners: Map<string, Array<(event: AgentChatInboxStreamEvent) => void>>;
    closed: boolean;
  }
  let live: Subscription | null = null;
  let closes = 0;

  const adapter: AgentChatAdapter = {
    ...NON_INBOX_ADAPTER,
    async listInbox(input = {}) {
      listInputs.push(input);
      const wanted = [input.status ?? []].flat();
      const visible = wanted.length
        ? items.filter((item) => wanted.includes(item.status))
        : items;
      return { items: [...visible], nextCursor: null };
    },
    async getInboxItem(input) {
      const item = items.find((candidate) => candidate.id === input.itemId);
      if (!item) throw new Error("missing item");
      return item;
    },
    async markInboxItemRead(input) {
      const item = items.find((candidate) => candidate.id === input.itemId);
      if (!item) throw new Error("missing item");
      item.readAt = item.readAt ?? new Date().toISOString();
      return item;
    },
    async acknowledgeInboxItem(input) {
      const item = items.find((candidate) => candidate.id === input.itemId);
      if (!item) throw new Error("missing item");
      item.status = "acknowledged";
      return item;
    },
    async respondGovernanceInboxItem(input) {
      const item = items.find((candidate) => candidate.id === input.itemId);
      if (!item) throw new Error("missing item");
      item.status = "responded";
      return item;
    },
    openInboxStream() {
      const subscription: Subscription = { listeners: new Map(), closed: false };
      live = subscription;
      return {
        addEventListener(type, listener) {
          subscription.listeners.set(type, [
            ...(subscription.listeners.get(type) ?? []),
            listener,
          ]);
        },
        close() {
          subscription.closed = true;
          closes += 1;
        },
        onerror: null,
      };
    },
    ...overrides,
  };

  return {
    adapter,
    listInputs,
    emit: (type, data) => {
      if (!live || live.closed) return;
      for (const listener of live.listeners.get(type) ?? []) listener({ data });
    },
    closed: () => closes,
  };
}

let root: Root | null = null;
let container: HTMLDivElement | null = null;

async function mount(adapter: AgentChatAdapter) {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => {
    root?.render(<InboxPanel adapter={adapter} />);
    await Promise.resolve();
  });
  return container;
}

function click(selector: string) {
  return act(async () => {
    container?.querySelector<HTMLButtonElement>(selector)?.click();
    await Promise.resolve();
  });
}

function clickText(text: string) {
  const button = [
    ...(container?.querySelectorAll<HTMLButtonElement>("button") ?? []),
  ].find((candidate) => candidate.textContent?.trim() === text);
  return act(async () => {
    button?.click();
    await Promise.resolve();
  });
}

afterEach(() => {
  if (root) act(() => root?.unmount());
  root = null;
  container?.remove();
  container = null;
  vi.useRealTimers();
});

describe("InboxPanel lifecycle", () => {
  it("shows an item the stream announces after the list loaded", async () => {
    const items = [inboxItem({ id: 1, title: "First" })];
    const harness = createHarness(items);
    const view = await mount(harness.adapter);

    items.push(
      inboxItem({ id: 2, title: "Arrived later", kind: "input_required" }),
    );
    await act(async () => {
      harness.emit(
        "item",
        JSON.stringify({ item_id: 2, kind: "input_required" }),
      );
      await Promise.resolve();
    });

    // Before, applyItem only replaced rows the list already had, so a
    // brand new one was dropped and the inbox looked empty all day.
    expect(view.textContent).toContain("Arrived later");
    expect(view.textContent).toContain("First");
  });

  it("does not let a nudge drop a pending item into History", async () => {
    const items = [inboxItem({ id: 1, title: "Old news", status: "responded" })];
    const harness = createHarness(items);
    const view = await mount(harness.adapter);
    await clickText("history");
    expect(view.textContent).toContain("Old news");

    items.push(inboxItem({ id: 2, title: "Brand new" }));
    await act(async () => {
      harness.emit("item", JSON.stringify({ item_id: 2, kind: "task_complete" }));
      await Promise.resolve();
    });

    // Something the agent just raised is the opposite of history.
    expect(view.textContent).not.toContain("Brand new");
  });

  it("files an action that lands after a tab switch under the new view", async () => {
    const items = [
      inboxItem({ id: 1, title: "Still pending" }),
      inboxItem({ id: 2, title: "Old news", status: "responded" }),
    ];
    let releaseRead: (() => void) | null = null;
    const harness = createHarness(items, {
      async markInboxItemRead(input) {
        await new Promise<void>((resolve) => {
          releaseRead = resolve;
        });
        const item = items.find((candidate) => candidate.id === input.itemId);
        if (!item) throw new Error("missing item");
        item.readAt = new Date().toISOString();
        return item;
      },
    });
    const view = await mount(harness.adapter);

    // Open a pending item, then switch tabs while its read receipt is
    // still in flight.
    await click('button[aria-label="Open inbox item Still pending"]');
    await clickText("history");
    expect(view.textContent).toContain("Old news");

    await act(async () => {
      releaseRead?.();
      await Promise.resolve();
      await Promise.resolve();
    });

    // A callback holding the view it started in would file this pending
    // item under History.
    expect(view.textContent).not.toContain("Still pending");
  });

  it("keeps an item the stream announced while the first page was loading", async () => {
    const items = [inboxItem({ id: 1, title: "From the list" })];
    let releaseList: (() => void) | null = null;
    let listCalls = 0;
    const harness = createHarness(items, {
      async listInbox() {
        listCalls += 1;
        if (listCalls === 1) {
          await new Promise<void>((resolve) => {
            releaseList = resolve;
          });
        }
        return { items: [items[0]!], nextCursor: null };
      },
    });
    const view = await mount(harness.adapter);

    items.push(
      inboxItem({ id: 2, title: "Arrived mid-load", kind: "input_required" }),
    );
    await act(async () => {
      harness.emit(
        "item",
        JSON.stringify({ item_id: 2, kind: "input_required" }),
      );
      await Promise.resolve();
    });
    await act(async () => {
      releaseList?.();
      await Promise.resolve();
      await Promise.resolve();
    });

    // The list snapshot predates the nudge, so replacing rather than
    // merging would drop the item the agent just raised.
    expect(view.textContent).toContain("Arrived mid-load");
    expect(view.textContent).toContain("From the list");
  });

  it("ignores a malformed stream frame instead of throwing", async () => {
    const harness = createHarness([inboxItem({ id: 1, title: "First" })]);
    const view = await mount(harness.adapter);

    await act(async () => {
      harness.emit("item", "not json");
      await Promise.resolve();
    });

    expect(view.textContent).toContain("First");
  });

  it("asks for pending in Active and the resolved statuses in History", async () => {
    const harness = createHarness([
      inboxItem({ id: 1, title: "Live" }),
      inboxItem({ id: 2, title: "Gone", status: "expired" }),
    ]);
    const view = await mount(harness.adapter);

    expect(harness.listInputs[0]?.status).toEqual(["pending"]);
    expect(view.textContent).toContain("Live");
    expect(view.textContent).not.toContain("Gone");

    await clickText("history");

    expect(harness.listInputs[1]?.status).toEqual([
      "acknowledged",
      "responded",
      "expired",
    ]);
    expect(view.textContent).toContain("Gone");
    expect(view.textContent).not.toContain("Live");
  });

  it("reports a failed acknowledgement instead of silently reverting", async () => {
    const harness = createHarness([inboxItem({ id: 1, title: "Done" })], {
      async acknowledgeInboxItem() {
        throw new Error("server said no");
      },
    });
    const view = await mount(harness.adapter);

    await click('button[aria-label="Open inbox item Done"]');
    await click('button[aria-label="Acknowledge inbox item"]');

    expect(view.textContent).toContain("server said no");
  });

  it("refuses to submit a question past its answer window", async () => {
    const stale = inboxItem({
      id: 1,
      kind: "input_required",
      title: "Which color?",
      payload: { tool_call_id: "tc-1", questions: [{ prompt: "Which color?" }] },
    });
    stale.expiresAt = new Date(Date.now() - 1000).toISOString();
    const submitted: unknown[] = [];
    const harness = createHarness([stale], {
      async submitAskUserQuestionResponse(input) {
        submitted.push(input);
        return { eventId: 1 };
      },
    });
    const view = await mount(harness.adapter);

    await click('button[aria-label="Open inbox item Which color?"]');
    const input = container?.querySelector<HTMLInputElement>("input");
    await act(async () => {
      if (input) {
        input.value = "blue";
        input.dispatchEvent(new Event("input", { bubbles: true }));
      }
      await Promise.resolve();
    });
    await click('button[aria-label="Submit inbox response"]');

    expect(submitted).toEqual([]);
    expect(view.textContent).toContain("stopped waiting");
  });

  it("keeps Submit disabled when the payload carries no questions", async () => {
    const harness = createHarness([
      inboxItem({
        id: 1,
        kind: "input_required",
        title: "Empty ask",
        payload: { tool_call_id: "tc-1", questions: [] },
      }),
    ]);
    await mount(harness.adapter);

    await click('button[aria-label="Open inbox item Empty ask"]');
    const submit = container?.querySelector<HTMLButtonElement>(
      'button[aria-label="Submit inbox response"]',
    );

    // every() is vacuously true on an empty batch, and the server
    // rejects a submission with no answers in it.
    expect(submit?.disabled).toBe(true);
  });
});
