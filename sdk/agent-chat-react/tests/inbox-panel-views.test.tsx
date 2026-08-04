// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Active is what needs the user; Updates is what the agent finished
// while they were away. Mixed together, one completed task per session
// buried the two questions that actually needed an answer.

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";
import { NON_INBOX_ADAPTER, inboxItem } from "./inbox-adapter-stub";
import { InboxPanel } from "../src/components/inbox/inbox-panel";
import type {
  AgentChatAdapter,
  AgentChatInboxItem,
  AgentChatInboxListInput,
} from "../src/types";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT =
  true;

interface Harness {
  adapter: AgentChatAdapter;
  listInputs: AgentChatInboxListInput[];
  emit: (type: "item" | "snapshot", data: string) => void;
}

function createHarness(items: AgentChatInboxItem[]): Harness {
  const listInputs: AgentChatInboxListInput[] = [];
  let listeners = new Map<string, Array<(event: { data: string }) => void>>();

  const adapter: AgentChatAdapter = {
    ...NON_INBOX_ADAPTER,
    async listInbox(input = {}) {
      listInputs.push(input);
      const statuses = [input.status ?? []].flat();
      const kinds = [input.kind ?? []].flat();
      return {
        items: items.filter(
          (item) =>
            (!statuses.length || statuses.includes(item.status)) &&
            (!kinds.length || kinds.includes(item.kind)),
        ),
        nextCursor: null,
      };
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
      listeners = new Map();
      return {
        addEventListener(type, listener) {
          listeners.set(type, [...(listeners.get(type) ?? []), listener]);
        },
        close() {},
        onerror: null,
      };
    },
  };

  return {
    adapter,
    listInputs,
    emit: (type, data) => {
      for (const listener of listeners.get(type) ?? []) listener({ data });
    },
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
});

describe("InboxPanel views", () => {
  it("asks for the answerable kinds in Active and the rest in Updates", async () => {
    const harness = createHarness([]);
    await mount(harness.adapter);

    expect(harness.listInputs[0]).toMatchObject({
      status: ["pending"],
      kind: ["input_required", "action_required", "governance_gate"],
    });

    await clickText("updates");
    expect(harness.listInputs[1]).toMatchObject({
      status: ["pending"],
      kind: ["task_complete", "progress_checkin"],
    });

    // History is about what is finished, whatever kind it was.
    await clickText("history");
    expect(harness.listInputs[2]).toMatchObject({
      status: ["acknowledged", "responded", "expired"],
    });
    expect(harness.listInputs[2].kind).toBeUndefined();
  });

  it("keeps a finished task out of Active and shows it under Updates", async () => {
    const harness = createHarness([
      inboxItem({ id: 1, kind: "task_complete", title: "Wrote the file" }),
      inboxItem({ id: 2, kind: "input_required", title: "Which colour?" }),
    ]);
    const view = await mount(harness.adapter);

    expect(view.textContent).toContain("Which colour?");
    expect(view.textContent).not.toContain("Wrote the file");

    await clickText("updates");
    expect(view.textContent).toContain("Wrote the file");
    expect(view.textContent).not.toContain("Which colour?");
  });

  it("shows an update the stream announces while Updates is open", async () => {
    // Updates is a second pending view, not history: work finishing
    // while the user watches that tab is exactly what it is for.
    const items = [inboxItem({ id: 1, kind: "task_complete", title: "First" })];
    const harness = createHarness(items);
    const view = await mount(harness.adapter);
    await clickText("updates");

    items.push(
      inboxItem({ id: 2, kind: "task_complete", title: "Arrived later" }),
    );
    await act(async () => {
      harness.emit("item", JSON.stringify({ item_id: 2, kind: "task_complete" }));
      await Promise.resolve();
    });

    expect(view.textContent).toContain("Arrived later");
  });

  it("does not let a nudge drop an update into Active", async () => {
    const items = [inboxItem({ id: 1, kind: "input_required", title: "Which colour?" })];
    const harness = createHarness(items);
    const view = await mount(harness.adapter);

    items.push(inboxItem({ id: 2, kind: "task_complete", title: "Wrote the file" }));
    await act(async () => {
      harness.emit("item", JSON.stringify({ item_id: 2, kind: "task_complete" }));
      await Promise.resolve();
    });

    expect(view.textContent).not.toContain("Wrote the file");
  });
});
