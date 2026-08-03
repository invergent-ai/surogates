// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// The inbox answer form's dropdown. Option values are choice indexes so
// an agent-supplied label cannot collide with the "other" sentinel, and
// that indexing is what makes the empty placeholder value dangerous:
// Number("") is 0, a valid index.

import { act, type ReactElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AgentChatAdapterProvider, NO_BROWSER_ADAPTER } from "../src/adapter-context";
import { InboxPanel } from "../src/components/inbox/inbox-panel";
import { TooltipProvider } from "../src/components/ui/tooltip";
import type { AgentChatAdapter, AgentChatInboxItem } from "../src/types";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT =
  true;

const submitAskUserQuestionResponse = vi.fn().mockResolvedValue(undefined);

function inboxItem(question: Record<string, unknown>): AgentChatInboxItem {
  return {
    id: 1,
    sessionId: "s-1",
    kind: "input_required",
    status: "pending",
    title: "Needs your answer",
    body: "",
    createdAt: new Date().toISOString(),
    payload: { tool_call_id: "tc-1", questions: [question], context: "" },
  } as unknown as AgentChatInboxItem;
}

function adapterStub(item: AgentChatInboxItem): AgentChatAdapter {
  return {
    ...NO_BROWSER_ADAPTER,
    listInbox: vi.fn().mockResolvedValue({ items: [item], total: 1 }),
    getInboxItem: vi.fn().mockResolvedValue(item),
    markInboxItemRead: vi.fn().mockResolvedValue(item),
    acknowledgeInboxItem: vi.fn().mockResolvedValue(item),
    respondGovernanceInboxItem: vi.fn().mockResolvedValue(item),
    submitAskUserQuestionResponse,
    openInboxStream: vi.fn(() => ({ addEventListener: vi.fn(), close: vi.fn() })),
  } as unknown as AgentChatAdapter;
}

let root: Root | null = null;
let container: HTMLDivElement | null = null;

afterEach(() => {
  if (root) act(() => root?.unmount());
  root = null;
  container?.remove();
  container = null;
  submitAskUserQuestionResponse.mockClear();
});

async function mount(node: ReactElement): Promise<HTMLDivElement> {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => {
    root?.render(
      <AgentChatAdapterProvider value={{ adapter: adapterStub(ITEM), sessionId: "s-1" }}>
        <TooltipProvider>{node}</TooltipProvider>
      </AgentChatAdapterProvider>,
    );
  });
  return container;
}

const MENU_QUESTION = {
  prompt: "Which region?",
  choices: [{ label: "eu-west" }, { label: "us-east" }],
  allow_other: true,
};
const ITEM = inboxItem(MENU_QUESTION);

function setSelect(dom: HTMLDivElement, value: string) {
  const select = dom.querySelector<HTMLSelectElement>("select");
  if (!select) throw new Error("select not rendered");
  const setter = Object.getOwnPropertyDescriptor(
    HTMLSelectElement.prototype,
    "value",
  )?.set;
  act(() => {
    setter?.call(select, value);
    select.dispatchEvent(new Event("change", { bubbles: true }));
  });
  return select;
}

describe("inbox question dropdown", () => {
  it("offers Other when the agent allows it", async () => {
    const dom = await mount(<InboxPanel adapter={adapterStub(ITEM)} selectedId={1} />);
    const labels = [...dom.querySelectorAll("option")].map((o) => o.textContent);
    expect(labels).toContain("eu-west");
    expect(labels).toContain("Other");
  });

  it("reveals the free-text field only once Other is chosen", async () => {
    const dom = await mount(<InboxPanel adapter={adapterStub(ITEM)} selectedId={1} />);
    expect(dom.querySelector('input[placeholder="Type your answer…"]')).toBeNull();

    setSelect(dom, "other");

    expect(
      dom.querySelector('input[placeholder="Type your answer…"]'),
    ).not.toBeNull();
  });

  it("does not let the placeholder be chosen back", async () => {
    // Its value is "" and Number("") is 0, so reselecting it would read
    // as the first choice. Disabled means the browser never offers it.
    const dom = await mount(<InboxPanel adapter={adapterStub(ITEM)} selectedId={1} />);

    const placeholder = dom.querySelector<HTMLOptionElement>('option[value=""]');
    expect(placeholder?.disabled).toBe(true);
  });

  it("selects by index, so a label cannot collide with the sentinel", async () => {
    const dom = await mount(<InboxPanel adapter={adapterStub(ITEM)} selectedId={1} />);
    const values = [...dom.querySelectorAll("option")].map((o) => o.getAttribute("value"));
    expect(values).toEqual(["", "0", "1", "other"]);
  });
});
