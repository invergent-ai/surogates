/**
 * A project master's thread cards follow the project's stream: the rows are
 * read whole when the stream is ready, and one row when a change names it.
 */
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AgentChat } from "../src/agent-chat";
import { AgentChatAdapterProvider, NO_BROWSER_ADAPTER } from "../src/adapter-context";
import { ThreadCards } from "../src/components/chat/thread-cards";
import { useProjectThreads } from "../src/components/chat/use-project-threads";
import { applyAgentChatEvent, createInitialAgentChatState } from "../src/runtime/reducer";
import type {
  AgentChatAdapter,
  AgentChatInboxStreamEvent,
  AgentChatProjectStream,
  AgentChatThreadRow,
  ChatMessage,
} from "../src/types";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const THREAD = "2d8a5e3a-ac4f-4e7a-9b32-3c4d5e6f7081";

// A project's stream, or a session's, whose events carry their ids.
class FakeStream implements AgentChatProjectStream {
  onerror: (() => void) | null = null;
  closed = false;
  private readonly listeners = new Map<string, Array<(event: AgentChatInboxStreamEvent) => void>>();

  addEventListener(type: string, listener: (event: AgentChatInboxStreamEvent) => void): void {
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), listener]);
  }

  close(): void {
    this.closed = true;
  }

  emit(type: string, data: Record<string, unknown> = {}, eventId?: number): void {
    const event = { data: JSON.stringify(data), lastEventId: eventId === undefined ? undefined : String(eventId) };
    for (const listener of this.listeners.get(type) ?? []) listener(event);
  }
}

function row(overrides: Partial<AgentChatThreadRow> = {}): AgentChatThreadRow {
  return { id: THREAD, title: "Draft A", group: "working", reason: null, statusLine: null, progress: null, files: [], ...overrides };
}

function project(stream: FakeStream, rows: () => AgentChatThreadRow[]) {
  return {
    listProjectThreads: vi.fn(async ({ threadId }: { projectId: string; threadId?: string }) =>
      rows().filter((found) => threadId === undefined || found.id === threadId)),
    openProjectStream: vi.fn(() => stream),
  };
}

const card: ChatMessage = applyAgentChatEvent(createInitialAgentChatState(), {
  type: "worker.spawned", eventId: 1, data: { worker_id: THREAD, title: "Draft A", goal: "Draft the A memo." },
}).messages[0]!;

let root: Root | null = null;
let container: HTMLDivElement | null = null;

afterEach(() => {
  if (root) act(() => root?.unmount());
  root = null;
  container?.remove();
  container = null;
});

function Live({ adapter }: { adapter: AgentChatAdapter }) {
  const threadRows = useProjectThreads(adapter, "project-1");
  return (
    <AgentChatAdapterProvider value={{ adapter, sessionId: "master", projectId: "project-1", threadRows }}>
      <ThreadCards message={card} />
    </AgentChatAdapterProvider>
  );
}

async function settle(): Promise<void> {
  await act(async () => {
    for (let i = 0; i < 20; i++) await Promise.resolve();
  });
}

// The ids of the rows the hook holds.
function Rows({ adapter }: { adapter: AgentChatAdapter }) {
  const rows = useProjectThreads(adapter, "project-1");
  return <span>{Object.keys(rows).sort().join(",")}</span>;
}

function mountRows(adapter: AgentChatAdapter): () => string | null {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => root?.render(<Rows adapter={adapter} />));
  return () => container!.textContent;
}

describe("a project's rows", () => {
  it("reads a failed read again at the next change, and loses no other read to it", async () => {
    const stream = new FakeStream();
    let failing = true;
    const listProjectThreads = vi.fn(async ({ threadId }: { projectId: string; threadId?: string }) => {
      if (threadId === "B" && failing) {
        failing = false;
        // Heard while B is read.
        stream.emit("change", { thread_id: "D" });
        throw new Error("Bad Gateway");
      }
      return [row({ id: threadId })];
    });
    const rows = mountRows({ ...NO_BROWSER_ADAPTER, listProjectThreads, openProjectStream: () => stream } as unknown as AgentChatAdapter);
    // B and C are heard while A is read, and read together after it.
    act(() => {
      for (const threadId of ["A", "B", "C"]) stream.emit("change", { thread_id: threadId });
    });
    await settle();
    expect(rows()).toBe("A,C,D");
    act(() => stream.emit("change", { thread_id: "E" }));
    await settle();
    expect(rows()).toBe("A,B,C,D,E");
    expect(listProjectThreads.mock.calls.map(([input]) => input.threadId)).toEqual(["A", "B", "C", "D", "E", "B"]);
  });

  it("drops a read that lands after the project's stream ended", async () => {
    const stream = new FakeStream();
    let land: () => void = () => {};
    const listProjectThreads = vi.fn(() => new Promise<AgentChatThreadRow[]>((resolve) => {
      land = () => resolve([row()]);
    }));
    const rows = mountRows({ ...NO_BROWSER_ADAPTER, listProjectThreads, openProjectStream: () => stream } as unknown as AgentChatAdapter);
    act(() => stream.emit("ready"));
    act(() => stream.onerror?.());
    await act(async () => land());
    await settle();
    expect(listProjectThreads).toHaveBeenCalledTimes(1);
    expect(rows()).toBe("");
  });

  it("calls the adapter's own methods, as a class instance's", async () => {
    class ProjectAdapter {
      readonly stream = new FakeStream();
      readonly rows = [row()];
      async listProjectThreads() {
        return this.rows;
      }
      openProjectStream() {
        return this.stream;
      }
    }
    const adapter = new ProjectAdapter();
    const rows = mountRows(adapter as unknown as AgentChatAdapter);
    act(() => adapter.stream.emit("ready"));
    await settle();
    expect(rows()).toBe(THREAD);
  });
});

describe("a thread's card, live", () => {
  it("moves from working to waiting on a question, to idle, to resolved, as the stream tells it", async () => {
    const stream = new FakeStream();
    let current = row({ statusLine: "Writing the outlook", progress: { done: 1, total: 2 } });
    const live = project(stream, () => [current]);
    const adapter = { ...NO_BROWSER_ADAPTER, ...live } as unknown as AgentChatAdapter;
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    act(() => root?.render(<Live adapter={adapter} />));
    const status = () => container!.querySelector('[data-testid="worker-card-status"]')?.textContent;
    // Before the stream is ready, the card says what the events said.
    expect(status()).toBe("Working");

    stream.emit("ready");
    await settle();
    expect(live.listProjectThreads).toHaveBeenLastCalledWith({ projectId: "project-1" });
    expect(status()).toBe("Working · 1/2");
    expect(container.textContent).toContain("Writing the outlook");

    for (const [next, label] of [
      [row({ group: "waiting", reason: "question", statusLine: "Which year?" }), "Waiting on you"],
      [row({ group: "idle", statusLine: "Drafted the memo." }), "Idle"],
      [row({ group: "resolved", statusLine: "Drafted the memo." }), "Resolved"],
    ] as const) {
      current = next;
      stream.emit("change", { thread_id: THREAD, type: "session.complete" });
      await settle();
      expect(live.listProjectThreads).toHaveBeenLastCalledWith({ projectId: "project-1", threadId: THREAD });
      expect(status()).toBe(label);
      expect(container.textContent).toContain(next.statusLine!);
    }

    // A project-wide change reads the rows whole; a thread that left them drops its row.
    current = row({ id: "another" });
    stream.emit("change", { thread_id: null, type: "worker.spawned" });
    await settle();
    expect(live.listProjectThreads).toHaveBeenLastCalledWith({ projectId: "project-1" });
    expect(status()).toBe("Working");

    // The stream ends only when the project is gone: the card says what its events said.
    current = row({ group: "idle" });
    stream.emit("change", { thread_id: null, type: "worker.complete" });
    await settle();
    expect(status()).toBe("Idle");
    act(() => stream.onerror?.());
    expect(status()).toBe("Working");
  });

  it("gives a master's cards, through AgentChat, their rows, their Start and their View thread", async () => {
    const PROPOSAL = "5d1c0e7a-3f42-4b8e-9a61-2c7d8e9f0a1b";
    const events = new FakeStream();
    const projectStream = new FakeStream();
    const live = project(projectStream, () => [row({ group: "waiting", reason: "question", statusLine: "Which year?" })]);
    const startProposedThread = vi.fn(async ({ key }: { key: string }) => row({ id: `thread-${key}` }));
    const onSessionChange = vi.fn();
    const adapter = {
      ...NO_BROWSER_ADAPTER,
      ...live,
      startProposedThread,
      listSessions: async () => ({ sessions: [], total: 0 }),
      getSession: async ({ sessionId }: { sessionId: string }) => ({
        id: sessionId, status: "completed", config: { workstream_role: "coordinator", workstream_id: "project-1" },
      }),
      openEventStream: () => events,
    } as unknown as AgentChatAdapter;
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    await act(async () => {
      root?.render(<AgentChat adapter={adapter} sessionId="master" onSessionChange={onSessionChange} />);
    });
    await settle();
    act(() => {
      events.emit("thread.proposed", { proposal_id: PROPOSAL, threads: [
        { key: "1", title: "Draft A", goal: "Draft the A memo.", where: "cloud" },
        { key: "2", title: "Summarise B", goal: "Summarise B.pdf.", where: "cloud" },
      ] }, 1);
      events.emit("worker.spawned", {
        worker_id: THREAD, title: "Draft A", goal: "Draft the A memo.", started_by: "user", proposal_id: PROPOSAL, key: "1",
      }, 2);
    });
    projectStream.emit("ready");
    await settle();

    const workerCard = container.querySelector('[data-testid="worker-card"]')!;
    expect(workerCard.querySelector('[data-testid="worker-card-status"]')?.textContent).toBe("Waiting on you");
    expect(workerCard.textContent).toContain("Which year?");

    act(() => [...workerCard.querySelectorAll("button")].find((found) => found.textContent === "View thread")?.click());
    expect(onSessionChange).toHaveBeenLastCalledWith(THREAD);

    const second = container.querySelectorAll('[data-testid="proposed-thread"]')[1]!;
    await act(async () => [...second.querySelectorAll("button")].find((found) => found.textContent === "Start")?.click());
    expect(startProposedThread).toHaveBeenCalledWith({ projectId: "project-1", proposalId: PROPOSAL, key: "2" });
  });

  it("follows the project of a master's conversation, and only a master's", async () => {
    for (const [config, followed] of [
      [{ workstream_role: "coordinator", workstream_id: "project-1" }, true],
      [{ workstream_role: "thread", workstream_id: "project-1" }, false],
      [{}, false],
    ] as const) {
      const stream = new FakeStream();
      const live = project(stream, () => []);
      const adapter = {
        ...NO_BROWSER_ADAPTER,
        ...live,
        listSessions: async () => ({ sessions: [], total: 0 }),
        getSession: async ({ sessionId }: { sessionId: string }) => ({ id: sessionId, status: "completed", config }),
        openEventStream: () => ({ addEventListener: vi.fn(), close: vi.fn(), onerror: null }),
      } as unknown as AgentChatAdapter;
      container = document.createElement("div");
      document.body.appendChild(container);
      root = createRoot(container);
      await act(async () => {
        root?.render(<AgentChat adapter={adapter} sessionId="master" />);
      });
      await settle();
      expect(live.openProjectStream.mock.calls).toEqual(followed ? [[{ projectId: "project-1" }]] : []);
      act(() => root?.unmount());
      root = null;
      container.remove();
      expect(stream.closed).toBe(followed);
    }
  });
});
