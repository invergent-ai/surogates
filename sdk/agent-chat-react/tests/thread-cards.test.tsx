/**
 * The cards of a project's master conversation (desktop design, Section 12):
 * a card per thread or worker the session started, updated in place by its
 * reports, and a card per proposal, whose threads the user starts, in the cloud or in a folder of their computer.
 */
import { act, type ReactElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AgentChatAdapterProvider, NO_BROWSER_ADAPTER } from "../src/adapter-context";
import { ChatThread } from "../src/components/chat/chat-thread";
import { TooltipProvider } from "../src/components/ui/tooltip";
import { applyAgentChatEvent, createInitialAgentChatState } from "../src/runtime/reducer";
import type { AgentChatAdapter, AgentChatRuntimeEvent, AgentChatState, AgentChatThreadRow } from "../src/types";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const THREAD = "2d8a5e3a-ac4f-4e7a-9b32-3c4d5e6f7081";
const PROPOSAL = "5d1c0e7a-3f42-4b8e-9a61-2c7d8e9f0a1b";

function applied(...events: Array<Omit<AgentChatRuntimeEvent, "eventId">>): AgentChatState {
  return events.reduce(
    (state, event, index) => applyAgentChatEvent(state, { ...event, eventId: index + 1 }),
    createInitialAgentChatState(),
  );
}

const spawned = (data: Record<string, unknown> = {}) => ({
  type: "worker.spawned" as const,
  data: { worker_id: THREAD, title: "Draft A", goal: "Draft the A memo as A.docx.", ...data },
});
const proposed = {
  type: "thread.proposed" as const,
  data: {
    proposal_id: PROPOSAL,
    threads: [
      { key: "1", title: "Draft A", goal: "Draft the A memo as A.docx.", where: "cloud" },
      { key: "2", title: "Summarise B", goal: "Summarise B.pdf.", where: "cloud" },
      { key: "3", title: "Check the totals", goal: "Check the totals in Budget.xlsx.", where: "device" },
    ],
  },
};

describe("the reducer's thread cards", () => {
  it("draws a thread's card when it starts and updates it in place with each report", () => {
    const started = applied(spawned());
    expect(started.messages).toHaveLength(1);
    expect(started.messages[0]).toMatchObject({
      id: `worker-${THREAD}`,
      role: "system",
      systemKind: "worker",
      worker: { id: THREAD, title: "Draft A", goal: "Draft the A memo as A.docx.", state: "working", report: null, files: [] },
    });

    const reported = applied(
      spawned(),
      { type: "llm.response", data: { message: { content: "I started a thread for A." } } },
      { type: "worker.complete", data: {
        worker_id: THREAD, title: "Draft A", result: "Drafted the memo.",
        files: [{ kind: "file", label: "A.docx", ref: "threads/Draft A/A.docx" }],
      } },
    );
    expect(reported.messages.map((message) => message.systemKind ?? message.role)).toEqual(["worker", "assistant"]);
    expect(reported.messages[0]?.worker).toMatchObject({
      state: "reported", report: "Drafted the memo.", files: [{ kind: "file", label: "A.docx", ref: "threads/Draft A/A.docx" }],
    });

    const failed = applied(spawned(), { type: "worker.failed", data: { worker_id: THREAD, error: "recovery_loop" } });
    expect(failed.messages[0]?.worker).toMatchObject({ state: "failed", report: "recovery_loop" });
  });

  it("draws nothing for a report from a worker it has no card for, as a delegated task's", () => {
    const before = applied({ type: "llm.response", data: { message: { content: "I delegated it." } } });
    for (const report of [
      { type: "worker.complete" as const, data: { worker_id: THREAD, result: "Done." } },
      { type: "worker.failed" as const, data: { worker_id: THREAD, error: "recovery_loop" } },
    ]) {
      const after = applyAgentChatEvent(before, { ...report, eventId: 2 });
      expect(after.messages).toEqual(before.messages);
    }
  });

  it("keeps the loop's control markup out of a mission worker's report", () => {
    const state = applied(spawned(), { type: "worker.complete", data: {
      worker_id: THREAD, result: 'Checked the figures.\n<next_action complexity="low">done</next_action>',
    } });
    expect(state.messages[0]?.worker?.report).toBe("Checked the figures.");
  });

  it("keeps only a report's files and artifacts on its card, as a thread's row does", () => {
    const state = applied(spawned(), { type: "worker.complete", data: { worker_id: THREAD, result: "Done.", files: [
      { kind: "file", label: "A.docx", ref: "threads/Draft A/A.docx" },
      { kind: "url", label: "https://example.com", ref: "https://example.com" },
      { kind: "command", label: "rm -rf build", ref: "rm -rf build" },
      { kind: "artifact", label: "Outlook", ref: "a-1" },
    ] } });
    expect(state.messages[0]?.worker?.files.map((file) => file.kind)).toEqual(["file", "artifact"]);
  });

  it("names a coordinator's worker by its goal", () => {
    const state = applied({ type: "worker.spawned", data: { worker_id: THREAD, goal: "Check the figures." } });
    expect(state.messages[0]?.worker).toMatchObject({ title: null, goal: "Check the figures." });
  });

  it("draws a proposal's card, and marks a card the user started", () => {
    const state = applied(proposed, spawned({ started_by: "user", proposal_id: PROPOSAL, key: "1" }));
    expect(state.messages[0]).toMatchObject({
      id: `proposal-${PROPOSAL}`,
      systemKind: "thread_proposal",
      proposal: { proposalId: PROPOSAL, started: { 1: THREAD } },
    });
    expect(state.messages[0]?.proposal?.threads.map((thread) => thread.where)).toEqual(["cloud", "cloud", "device"]);
    expect(state.messages[1]?.worker?.id).toBe(THREAD);
  });
});

let root: Root | null = null;
let container: HTMLDivElement | null = null;

afterEach(() => {
  if (root) act(() => root?.unmount());
  root = null;
  container?.remove();
  container = null;
});

function adapterStub(overrides: Partial<AgentChatAdapter> = {}): AgentChatAdapter {
  return {
    ...NO_BROWSER_ADAPTER,
    listSessions: vi.fn().mockResolvedValue({ sessions: [], total: 0 }),
    createSession: vi.fn(),
    getSession: vi.fn(),
    sendMessage: vi.fn(),
    openEventStream: vi.fn(() => ({ addEventListener: vi.fn(), close: vi.fn(), onerror: null })),
    ...overrides,
  } as unknown as AgentChatAdapter;
}

type CardContext = { projectId?: string; onOpenSession?: (id: string) => void };

function provided(node: ReactElement, adapter: AgentChatAdapter, context: CardContext) {
  return (
    <AgentChatAdapterProvider value={{ adapter, sessionId: "master", ...context }}>
      <TooltipProvider>{node}</TooltipProvider>
    </AgentChatAdapterProvider>
  );
}

function mount(node: ReactElement, adapter: AgentChatAdapter, context: CardContext = {}) {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => {
    root?.render(provided(node, adapter, context));
  });
  return container;
}

function thread(state: AgentChatState, viewMode: "simple" | "expert") {
  const noop = () => Promise.resolve();
  return (
    <ChatThread
      sessionId="master"
      messages={state.messages}
      isRunning={false}
      terminal={true}
      onSend={noop}
      onStop={noop}
      viewMode={viewMode}
    />
  );
}

function button(dom: HTMLElement, label: string, within: Element = dom): HTMLButtonElement {
  const found = [...within.querySelectorAll("button")].find((candidate) => candidate.textContent === label);
  if (!found) throw new Error(`no ${label} button`);
  return found;
}

describe("the cards in the conversation", () => {
  for (const viewMode of ["simple", "expert"] as const) {
    it(`${viewMode}: a thread's card shows its title, status and files, and opens the thread`, () => {
      const opened = vi.fn();
      const state = applied(
        spawned({ title: "Draft A‮" }),
        { type: "llm.response", data: { message: { content: "Started it." } } },
        { type: "worker.complete", data: {
          worker_id: THREAD,
          result: "## Summary\n\n**Drafted** the `A` memo, __twice__.\nThen checked it.",
          files: [{ kind: "file", label: "A.docx", ref: "threads/Draft A/A.docx" }],
        } },
      );
      const dom = mount(thread(state, viewMode), adapterStub(), { onOpenSession: opened });
      const card = dom.querySelector('[data-testid="worker-card"]')!;
      // Each field's bidi control stays inside its own isolate. The status line is
      // the report's first line that is not a heading, without its marks.
      const fields = ["Draft A‮", "Drafted the A memo, twice.", "A.docx"];
      expect([...card.querySelectorAll("bdi")].map((bdi) => bdi.textContent)).toEqual(fields);
      // A field cut short keeps its whole text in reach.
      expect([...card.querySelectorAll("[title]")].map((node) => node.getAttribute("title"))).toEqual(fields);
      expect(card.querySelector('[data-testid="worker-card-status"]')?.textContent).toBe("Reported");
      const view = button(dom, "View thread", card);
      expect(view.getAttribute("aria-label")).toBe("View thread Draft A‮");
      act(() => view.click());
      expect(opened).toHaveBeenCalledWith(THREAD);
    });
  }

  it("starts a proposed thread from its card, and offers Start all no more once one cloud thread waits", async () => {
    const startProposedThread = vi.fn(async ({ key }: { key: string }) => ({
      id: `thread-${key}`, title: "", group: "working" as const, reason: null, statusLine: null, progress: null, files: [],
    }));
    const dom = mount(thread(applied(proposed), "simple"), adapterStub({ startProposedThread }), { projectId: "project-1" });
    const cards = [...dom.querySelectorAll('[data-testid="proposed-thread"]')];
    expect(cards.map((card) => [...card.querySelectorAll("bdi")].map((bdi) => bdi.textContent))).toEqual([
      ["Draft A", "Draft the A memo as A.docx."],
      ["Summarise B", "Summarise B.pdf."],
      ["Check the totals", "Check the totals in Budget.xlsx."],
    ]);
    // A thread on the user's computer is started from Surogate Desktop, or in the cloud instead.
    expect([...cards[2]!.querySelectorAll("button")].map((found) => found.textContent)).toEqual(["Run in the cloud instead"]);

    await act(async () => button(dom, "Start", cards[0]).click());
    expect(startProposedThread).toHaveBeenCalledWith({ projectId: "project-1", proposalId: PROPOSAL, key: "1" });
    expect(cards[0]?.textContent).toContain("Started");

    await act(async () => button(dom, "Start", cards[1]).click());
    expect(startProposedThread).toHaveBeenCalledTimes(2);
    expect(dom.textContent).not.toContain("Start all");
  });

  for (const viewMode of ["simple", "expert"] as const) {
    it(`${viewMode}: a proposal's card starts its threads once the master writes on below it`, async () => {
      const startProposedThread = vi.fn(async ({ key }: { key: string }) => ({
        id: `thread-${key}`, title: "", group: "working" as const, reason: null, statusLine: null, progress: null, files: [],
      }));
      const state = applied(proposed, { type: "llm.response", data: { message: { content: "Start the ones you want." } } });
      const dom = mount(thread(state, viewMode), adapterStub({ startProposedThread }), { projectId: "project-1" });
      expect(dom.textContent).toContain("Start the ones you want.");
      const card = dom.querySelector('[data-testid="proposed-thread"]')!;
      await act(async () => button(dom, "Start", card).click());
      expect(startProposedThread).toHaveBeenCalledWith({ projectId: "project-1", proposalId: PROPOSAL, key: "1" });
      expect(card.textContent).toContain("Started");
    });
  }

  it("names the thread each button acts on, says it is starting, and gives focus to the started card", async () => {
    const finish: Record<string, () => void> = {};
    const startProposedThread = vi.fn(({ key }: { key: string }) => new Promise<AgentChatThreadRow>((resolve) => {
      finish[key] = () => resolve({ id: `thread-${key}`, title: "", group: "working", reason: null, statusLine: null, progress: null, files: [] });
    }));
    const dom = mount(thread(applied(proposed), "simple"), adapterStub({ startProposedThread }), {
      projectId: "project-1", onOpenSession: vi.fn(),
    });
    const cards = () => [...dom.querySelectorAll('[data-testid="proposed-thread"]')];
    const start = button(dom, "Start", cards()[0]);
    expect(start.getAttribute("aria-label")).toBe("Start Draft A");
    start.focus();
    await act(async () => start.click());
    expect(start.textContent).toBe("Starting…");
    expect(start.getAttribute("aria-label")).toBe("Starting Draft A");
    expect(start.disabled).toBe(true);

    await act(async () => finish["1"]!());
    expect(cards()[0]?.querySelector('[role="status"]')?.textContent).toBe("Started");
    const view = button(dom, "View thread", cards()[0]);
    expect(view.getAttribute("aria-label")).toBe("View thread Draft A");
    // Its Start went with the focus on it: focus moves to its View thread.
    expect(document.activeElement).toBe(view);

    // Focus the user put elsewhere stays there.
    await act(async () => button(dom, "Start", cards()[1]).click());
    await act(async () => finish["2"]!());
    expect(button(dom, "View thread", cards()[1])).toBeTruthy();
    expect(document.activeElement).toBe(view);
  });

  it("keeps a card's Starting… and the focus it owes across a change of view mode, which draws the card anew", async () => {
    let finish: () => void = () => {};
    const startProposedThread = vi.fn(() => new Promise<AgentChatThreadRow>((resolve) => {
      finish = () => resolve({ id: "thread-1", title: "", group: "working", reason: null, statusLine: null, progress: null, files: [] });
    }));
    const adapter = adapterStub({ startProposedThread });
    const context = { projectId: "project-1", onOpenSession: vi.fn() };
    // The master writes on below its card: each view mode draws the card in a place of its own.
    const state = applied(proposed, { type: "llm.response", data: { message: { content: "Start the ones you want." } } });
    const dom = mount(thread(state, "simple"), adapter, context);
    const card = () => dom.querySelector('[data-testid="proposed-thread"]')!;
    const start = button(dom, "Start", card());
    start.focus();
    await act(async () => start.click());
    act(() => root?.render(provided(thread(state, "expert"), adapter, context)));
    expect(card().contains(start)).toBe(false);
    expect(button(dom, "Starting…", card()).disabled).toBe(true);
    await act(async () => finish());
    expect(document.activeElement).toBe(button(dom, "View thread", card()));
    expect(startProposedThread).toHaveBeenCalledTimes(1);
  });

  it("owes no focus for a start that finished while its card was not drawn", async () => {
    let finish: () => void = () => {};
    const startProposedThread = vi.fn(() => new Promise<AgentChatThreadRow>((resolve) => {
      finish = () => resolve({ id: "thread-1", title: "", group: "working", reason: null, statusLine: null, progress: null, files: [] });
    }));
    const adapter = adapterStub({ startProposedThread });
    const context = { projectId: "project-1", onOpenSession: vi.fn() };
    const state = applied(proposed);
    const dom = mount(thread(state, "simple"), adapter, context);
    const card = () => dom.querySelector('[data-testid="proposed-thread"]')!;
    const start = button(dom, "Start", card());
    start.focus();
    await act(async () => start.click());
    // The user went elsewhere: the conversation, with its card, is not drawn when the start finishes.
    act(() => root?.render(provided(<p>Another session</p>, adapter, context)));
    await act(async () => finish());
    act(() => root?.render(provided(thread(state, "simple"), adapter, context)));
    expect(button(dom, "View thread", card())).toBeTruthy();
    expect(document.activeElement).toBe(document.body);
  });

  it("says Started in a status region that was there before it", async () => {
    const startProposedThread = vi.fn(async () => (
      { id: "thread-1", title: "", group: "working" as const, reason: null, statusLine: null, progress: null, files: [] }
    ));
    const dom = mount(thread(applied(proposed), "simple"), adapterStub({ startProposedThread }), { projectId: "project-1" });
    const card = dom.querySelector('[data-testid="proposed-thread"]')!;
    const region = card.querySelector('[role="status"]');
    expect(region?.textContent).toBe("");
    await act(async () => button(dom, "Start", card).click());
    expect(card.querySelector('[role="status"]')).toBe(region);
    expect(region?.textContent).toBe("Started");
  });

  it("offers Start all while two cloud threads wait", async () => {
    const startProposedThread = vi.fn(async ({ key }: { key: string }) => ({
      id: `thread-${key}`, title: "", group: "working" as const, reason: null, statusLine: null, progress: null, files: [],
    }));
    const dom = mount(thread(applied(proposed), "expert"), adapterStub({ startProposedThread }), { projectId: "project-1" });
    await act(async () => button(dom, "Start all").click());
    expect(startProposedThread.mock.calls.map(([input]) => input.key)).toEqual(["1", "2"]);
  });

  it("starts Start all's cards one at a time, and goes on past one that fails", async () => {
    const settle: Record<string, () => void> = {};
    const startProposedThread = vi.fn(({ key }: { key: string }) => new Promise<AgentChatThreadRow>((resolve, reject) => {
      settle[key] = key === "1"
        ? () => reject(new Error("The thread could not be started."))
        : () => resolve({ id: `thread-${key}`, title: "", group: "working", reason: null, statusLine: null, progress: null, files: [] });
    }));
    const dom = mount(thread(applied(proposed), "simple"), adapterStub({ startProposedThread }), { projectId: "project-1" });
    await act(async () => button(dom, "Start all").click());
    // The second card's start waits on the first's answer.
    expect(startProposedThread.mock.calls.map(([input]) => input.key)).toEqual(["1"]);
    await act(async () => settle["1"]!());
    expect(startProposedThread.mock.calls.map(([input]) => input.key)).toEqual(["1", "2"]);
    await act(async () => settle["2"]!());
    const cards = [...dom.querySelectorAll('[data-testid="proposed-thread"]')];
    expect(cards[0]?.querySelector('[role="alert"]')?.textContent).toBe("The thread could not be started.");
    expect(cards[1]?.textContent).toContain("Started");
  });

  it("does not start a card again with Start all while it is starting", async () => {
    let finish: () => void = () => {};
    const startProposedThread = vi.fn(({ key }: { key: string }) => new Promise<AgentChatThreadRow>((resolve) => {
      const row = { id: `thread-${key}`, title: "", group: "working" as const, reason: null, statusLine: null, progress: null, files: [] };
      if (key === "1") finish = () => resolve(row);
      else resolve(row);
    }));
    const three = { ...proposed, data: { ...proposed.data, threads: [
      ...proposed.data.threads.slice(0, 2), { key: "4", title: "List the risks", goal: "List five risks.", where: "cloud" },
    ] } };
    const dom = mount(thread(applied(three), "simple"), adapterStub({ startProposedThread }), { projectId: "project-1" });
    await act(async () => button(dom, "Start", dom.querySelector('[data-testid="proposed-thread"]')!).click());
    await act(async () => button(dom, "Start all").click());
    await act(async () => finish());
    expect(startProposedThread.mock.calls.map(([input]) => input.key)).toEqual(["1", "2", "4"]);
  });

  it("says why a card did not start", async () => {
    const startProposedThread = vi.fn(async () => {
      throw new Error("This thread was already started.");
    });
    const dom = mount(thread(applied(proposed), "simple"), adapterStub({ startProposedThread }), { projectId: "project-1" });
    const card = dom.querySelector('[data-testid="proposed-thread"]')!;
    await act(async () => button(dom, "Start", card).click());
    expect(card.querySelector('[role="alert"]')?.textContent).toBe("This thread was already started.");
  });

  it("shows a card started elsewhere as Started, with no alert for its own start refused", async () => {
    let refuse: () => void = () => {};
    const startProposedThread = vi.fn(() => new Promise<AgentChatThreadRow>((_resolve, reject) => {
      refuse = () => reject(new Error("This thread was already started."));
    }));
    const adapter = adapterStub({ startProposedThread });
    const context = { projectId: "project-1" };
    const dom = mount(thread(applied(proposed), "simple"), adapter, context);
    await act(async () => button(dom, "Start", dom.querySelector('[data-testid="proposed-thread"]')!).click());
    // Another device started it: its thread's event arrives before this start's refusal.
    const startedElsewhere = applied(proposed, spawned({ started_by: "user", proposal_id: PROPOSAL, key: "1" }));
    act(() => root?.render(provided(thread(startedElsewhere, "simple"), adapter, context)));
    await act(async () => refuse());
    const card = dom.querySelector('[data-testid="proposed-thread"]')!;
    expect(card.textContent).toContain("Started");
    expect(card.querySelector('[role="alert"]')).toBeNull();
  });

  it("asks for a folder on this computer for a thread proposed there, from Surogate Desktop", async () => {
    const startLocalThread = vi.fn(async ({ key }: { key: string }) => ({
      id: `thread-${key}`, title: "", group: "working" as const, reason: null, statusLine: null, progress: null, files: [],
    }));
    const startProposedThread = vi.fn();
    const dom = mount(thread(applied(proposed), "simple"), adapterStub({ startProposedThread, startLocalThread }), {
      projectId: "project-1",
    });
    const card = [...dom.querySelectorAll('[data-testid="proposed-thread"]')][2]!;
    expect(card.textContent).toContain("Allow Surogate to work in a folder on your device");
    const allow = button(dom, "Allow", card);
    expect(allow.getAttribute("aria-label")).toBe("Allow Check the totals");
    await act(async () => allow.click());
    expect(startLocalThread).toHaveBeenCalledWith({ projectId: "project-1", proposalId: PROPOSAL, key: "3", title: "Check the totals" });
    expect(card.textContent).toContain("Started");
    expect(startProposedThread).not.toHaveBeenCalled();
    // Start all is the cloud's: it never asks for a folder.
    expect(dom.textContent).toContain("Start all");
  });

  it("runs a thread proposed for the user's computer in the cloud instead, when the user says so", async () => {
    const startProposedThread = vi.fn(async ({ key }: { key: string }) => ({
      id: `thread-${key}`, title: "", group: "working" as const, reason: null, statusLine: null, progress: null, files: [],
    }));
    const dom = mount(thread(applied(proposed), "simple"), adapterStub({ startProposedThread }), { projectId: "project-1" });
    const card = [...dom.querySelectorAll('[data-testid="proposed-thread"]')][2]!;
    // In a browser: no folder of this computer to ask for.
    expect(card.textContent).toContain("This works in a folder on your device, which needs Surogate Desktop");
    const cloud = button(dom, "Run in the cloud instead", card);
    expect(cloud.getAttribute("aria-label")).toBe("Run Check the totals in the cloud instead");
    await act(async () => cloud.click());
    expect(startProposedThread).toHaveBeenCalledWith({ projectId: "project-1", proposalId: PROPOSAL, key: "3" });
    expect(card.textContent).toContain("Started");
  });

  it("says only the button pressed is starting, and lets neither start the thread again meanwhile", async () => {
    const pending = () => new Promise<AgentChatThreadRow>(() => {});
    for (const [pressed, other] of [["Run in the cloud instead", "Allow"], ["Allow", "Run in the cloud instead"]] as const) {
      const dom = mount(thread(applied(proposed), "simple"), adapterStub({
        startProposedThread: vi.fn(pending), startLocalThread: vi.fn(pending),
      }), { projectId: "project-1" });
      const card = [...dom.querySelectorAll('[data-testid="proposed-thread"]')][2]!;
      await act(async () => button(dom, pressed, card).click());
      const starting = button(dom, "Starting…", card);
      expect(starting.getAttribute("aria-label")).toBe("Starting Check the totals");
      expect(starting.disabled).toBe(true);
      // The other way keeps its words, and waits.
      expect(button(dom, other, card).disabled).toBe(true);
      act(() => root?.unmount());
      dom.remove();
    }
  });

  it("says why a thread did not start on this computer, and lets the user allow it again", async () => {
    const startLocalThread = vi.fn()
      .mockRejectedValueOnce(new Error("No folder was chosen for this thread"))
      .mockResolvedValueOnce({ id: "thread-3", title: "", group: "working", reason: null, statusLine: null, progress: null, files: [] });
    const dom = mount(thread(applied(proposed), "simple"), adapterStub({ startProposedThread: vi.fn(), startLocalThread }), {
      projectId: "project-1",
    });
    const card = [...dom.querySelectorAll('[data-testid="proposed-thread"]')][2]!;
    await act(async () => button(dom, "Allow", card).click());
    expect(card.querySelector('[role="alert"]')?.textContent).toBe("No folder was chosen for this thread");
    await act(async () => button(dom, "Allow", card).click());
    expect(card.textContent).toContain("Started");
    expect(card.querySelector('[role="alert"]')).toBeNull();
  });

  it("offers no Start outside a project's master", () => {
    const dom = mount(thread(applied(proposed), "simple"), adapterStub({ startProposedThread: vi.fn() }));
    expect(dom.querySelector('[data-testid="thread-proposal-card"] button')).toBeNull();
  });
});
