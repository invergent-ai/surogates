/**
 * A project thread's card marks each file as the project's files have it: being redone,
 * or not merged, after its name.
 */
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";

import { AgentChatAdapterProvider, NO_BROWSER_ADAPTER } from "../src/adapter-context";
import { ThreadCards } from "../src/components/chat/thread-cards";
import { applyAgentChatEvent, createInitialAgentChatState } from "../src/runtime/reducer";
import type { AgentChatAdapter, AgentChatThreadRow, ChatMessage } from "../src/types";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const THREAD = "2d8a5e3a-ac4f-4e7a-9b32-3c4d5e6f7081";
const card: ChatMessage = applyAgentChatEvent(createInitialAgentChatState(), {
  type: "worker.spawned", eventId: 1, data: { worker_id: THREAD, title: "Draft A", goal: "Draft the A memo." },
}).messages[0]!;
const ROW: AgentChatThreadRow = {
  id: THREAD, title: "Draft A", group: "idle", reason: null, statusLine: null, progress: null,
  files: [
    { kind: "file", label: "Budget.xlsx", ref: "Budget.xlsx", landing: "not_merged" },
    { kind: "file", label: "Notes.md", ref: "Notes.md", landing: "redoing" },
    { kind: "file", label: "A.docx", ref: "A.docx", landing: "landed" },
  ],
};

let root: Root | null = null;
let container: HTMLDivElement | null = null;

afterEach(() => {
  if (root) act(() => root?.unmount());
  root = null;
  container?.remove();
  container = null;
});

function drawn(row: AgentChatThreadRow | null = ROW) {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  const adapter = NO_BROWSER_ADAPTER as unknown as AgentChatAdapter;
  act(() => root?.render(
    <AgentChatAdapterProvider value={{ adapter, sessionId: "master", projectId: "project-1", threadRows: row ? { [THREAD]: row } : {} }}>
      <ThreadCards message={card} />
    </AgentChatAdapterProvider>,
  ));
  return container;
}

const lines = (within: Element) => [...within.querySelectorAll("li")].map((item) => item.textContent);

describe("a project thread's card", () => {
  it("marks each file that did not land as it is, and none that landed", () => {
    expect(lines(drawn())).toEqual(["Budget.xlsx · not merged", "Notes.md · being redone", "A.docx"]);
  });

  it("shows a file's name as text, apart from its mark", () => {
    const markup = '<img src=x onerror="alert(1)">‮gpj.md';
    const drawnCard = drawn({ ...ROW, files: [{ kind: "file", label: markup, ref: "a.md", landing: "not_merged" }] });
    expect(lines(drawnCard)).toEqual([`${markup} · not merged`]);
    expect(drawnCard.querySelector("img")).toBeNull();
    // The name alone is isolated, so that none of its characters reorders the mark after it.
    expect(drawnCard.querySelector("li bdi")!.textContent).toBe(markup);
    expect(drawnCard.querySelector("li [data-mark]")!.textContent).toBe(" · not merged");
  });

  it("names its first files and counts the rest, the marked ones among those it names when they come first", () => {
    const landed = Array.from({ length: 1999 }, (_, at) => ({ kind: "file" as const, label: `part-${at}.csv`, ref: `part-${at}.csv`, landing: "landed" as const }));
    const drawnCard = drawn({ ...ROW, files: [ROW.files[0]!, ...landed] });
    expect(lines(drawnCard)).toEqual(["Budget.xlsx · not merged", "part-0.csv", "part-1.csv", "+1997 more"]);
  });

  it("marks nothing on a file that carries no mark", () => {
    // An agent from before file history, an artifact, and a file of a thread that works on the real files.
    const drawnCard = drawn({ ...ROW, files: [
      { kind: "file", label: "Old.md", ref: "Old.md" },
      { kind: "artifact", label: "Sales chart", ref: "art-1", landing: null },
      { kind: "file", label: "Plan.md", ref: "Plan.md", landing: "undone" },
    ] });
    expect(lines(drawnCard)).toEqual(["Old.md", "Sales chart", "Plan.md · undone"]);
  });

  it("shows a file whose mark it does not know with its name alone, and every mark it knows", () => {
    // As a later server may send: a mark this card has no words for, and one that names something every object has.
    const later = ["kept_apart", "toString"].map((landing, at) => (
      { kind: "file", label: `Later-${at}.md`, ref: `Later-${at}.md`, landing } as unknown as AgentChatThreadRow["files"][number]
    ));
    const drawnCard = drawn({ ...ROW, files: [...later, ROW.files[0]!] });
    expect(lines(drawnCard)).toEqual(["Later-0.md", "Later-1.md", "Budget.xlsx · not merged"]);
    expect(drawnCard.querySelectorAll("[data-mark]")).toHaveLength(1);
  });

  it("marks nothing on a card with no row, which shows what its reports said", () => {
    expect(drawn(null).querySelector("[data-mark]")).toBeNull();
  });
});
