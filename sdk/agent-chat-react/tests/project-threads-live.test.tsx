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

class FakeProjectStream implements AgentChatProjectStream {
  onerror: (() => void) | null = null;
  closed = false;
  private readonly listeners = new Map<string, Array<(event: AgentChatInboxStreamEvent) => void>>();

  addEventListener(type: "ready" | "change", listener: (event: AgentChatInboxStreamEvent) => void): void {
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), listener]);
  }

  close(): void {
    this.closed = true;
  }

  emit(type: "ready" | "change", data: Record<string, unknown> = {}): void {
    for (const listener of this.listeners.get(type) ?? []) listener({ data: JSON.stringify(data) });
  }
}

function row(overrides: Partial<AgentChatThreadRow> = {}): AgentChatThreadRow {
  return { id: THREAD, title: "Draft A", group: "working", reason: null, statusLine: null, progress: null, files: [], ...overrides };
}

function project(stream: FakeProjectStream, rows: () => AgentChatThreadRow[]) {
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
    for (let i = 0; i < 5; i++) await Promise.resolve();
  });
}

describe("a thread's card, live", () => {
  it("moves from working to waiting on a question, to idle, to resolved, as the stream tells it", async () => {
    const stream = new FakeProjectStream();
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

  it("follows the project of a master's conversation, and only a master's", async () => {
    for (const [config, followed] of [
      [{ workstream_role: "coordinator", workstream_id: "project-1" }, true],
      [{ workstream_role: "thread", workstream_id: "project-1" }, false],
      [{}, false],
    ] as const) {
      const stream = new FakeProjectStream();
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
