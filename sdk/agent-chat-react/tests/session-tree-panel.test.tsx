import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";
import { SessionTreePanel } from "../src/components/sessions/session-tree-panel";
import type {
  AgentChatAdapter,
  AgentChatArtifactPayload,
  AgentChatSession,
  AgentChatSessionList,
  AgentChatWorkspaceFile,
  AgentChatWorkspaceTree,
  AgentChatWorkspaceUpload,
} from "../src/types";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT =
  true;

function session(input: Partial<AgentChatSession> & { id: string }): AgentChatSession {
  return {
    status: "completed",
    title: "Session",
    createdAt: "2026-01-01T00:00:00Z",
    updatedAt: "2026-01-01T00:00:00Z",
    ...input,
  };
}

function createAdapter(sessions: AgentChatSession[]) {
  return {
    async listSessions() {
      return { sessions, total: sessions.length };
    },
    async createSession() {
      return session({ id: "created" });
    },
    async getSession(input) {
      return session({ id: input.sessionId });
    },
    async sendMessage() {
      return { eventId: 1, status: "accepted" };
    },
    async pauseSession() {},
    async retrySession(input) {
      return session({ id: input.sessionId });
    },
    async deleteSession() {},
    async getArtifact(): Promise<AgentChatArtifactPayload> {
      throw new Error("not used by session tree tests");
    },
    async submitClarifyResponse() {
      return { eventId: 1 };
    },
    async getWorkspaceTree(): Promise<AgentChatWorkspaceTree> {
      return { root: "workspace", entries: [], truncated: false };
    },
    async getWorkspaceFile(): Promise<AgentChatWorkspaceFile> {
      throw new Error("not used by session tree tests");
    },
    async uploadWorkspaceFile(): Promise<AgentChatWorkspaceUpload> {
      return { path: "uploaded.txt", size: 4 };
    },
    async deleteWorkspaceFile() {},
    openEventStream() {
      throw new Error("not used by session tree tests");
    },
  } satisfies AgentChatAdapter;
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((nextResolve) => {
    resolve = nextResolve;
  });
  return { promise, resolve };
}

let root: Root | null = null;
let container: HTMLDivElement | null = null;

afterEach(() => {
  if (root) {
    act(() => root?.unmount());
  }
  root = null;
  container?.remove();
  container = null;
});

describe("SessionTreePanel", () => {
  it("renders agent sessions from the adapter even without an active session", async () => {
    const adapter = createAdapter([
      session({
        id: "s-1",
        title: "First session",
        agentId: "agent-1",
        model: "surogate",
        updatedAt: new Date(Date.now() - 3 * 60 * 60 * 1000).toISOString(),
      }),
    ]);
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);

    await act(async () => {
      root?.render(
        <SessionTreePanel
          adapter={adapter}
          agentId="agent-1"
          title="Sessions"
        />,
      );
      await Promise.resolve();
    });

    expect(container.textContent).toContain("Sessions");
    expect(container.textContent).toContain("First session");
    expect(container.textContent).toContain("surogate");
    expect(container.textContent).toContain("ago");
    expect(container.textContent).not.toContain("completed");
  });

  it("does not render compact message and tool counters", async () => {
    const adapter = createAdapter([
      session({
        id: "s-1",
        title: "First session",
        agentId: "agent-1",
        messageCount: 3,
        toolCallCount: 2,
      }),
    ]);
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);

    await act(async () => {
      root?.render(
        <SessionTreePanel
          adapter={adapter}
          agentId="agent-1"
          title="Sessions"
        />,
      );
      await Promise.resolve();
    });

    expect(container.textContent).toContain("First session");
    expect(container.textContent).not.toContain("3m/2t");
  });

  it("does not render the session channel as the row label", async () => {
    const adapter = createAdapter([
      session({
        id: "s-1",
        title: null,
        agentId: "agent-1",
        channel: "web",
      }),
    ]);
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);

    await act(async () => {
      root?.render(
        <SessionTreePanel
          adapter={adapter}
          agentId="agent-1"
          title="Sessions"
        />,
      );
      await Promise.resolve();
    });

    expect(container.textContent).toContain("New session");
    expect(container.textContent).not.toContain("web");
  });

  it("keeps the session list visible while selecting another session refetches", async () => {
    const sessions = [
      session({ id: "s-1", title: "First session", agentId: "agent-1" }),
    ];
    const pendingList = deferred<AgentChatSessionList>();
    let listCalls = 0;
    const adapter: AgentChatAdapter = {
      ...createAdapter(sessions),
      async listSessions() {
        listCalls += 1;
        if (listCalls === 1) {
          return { sessions, total: sessions.length };
        }
        return pendingList.promise;
      },
    };
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);

    await act(async () => {
      root?.render(
        <SessionTreePanel
          adapter={adapter}
          agentId="agent-1"
          title="Sessions"
        />,
      );
      await Promise.resolve();
    });

    expect(container.textContent).toContain("First session");

    await act(async () => {
      root?.render(
        <SessionTreePanel
          adapter={adapter}
          agentId="agent-1"
          sessionId="s-1"
          title="Sessions"
        />,
      );
      await Promise.resolve();
    });

    expect(listCalls).toBe(2);
    expect(container.textContent).toContain("First session");
    expect(container.textContent).not.toContain("Loading...");
    expect(
      container.querySelector('[aria-label="Loading sessions"]'),
    ).not.toBeNull();

    await act(async () => {
      pendingList.resolve({ sessions, total: sessions.length });
      await Promise.resolve();
    });
  });

  it("deletes a session from the hover action", async () => {
    let sessions = [
      session({ id: "s-1", title: "First session", agentId: "agent-1" }),
    ];
    const deletedSessionIds: string[] = [];
    const adapter: AgentChatAdapter = {
      ...createAdapter(sessions),
      async listSessions() {
        return { sessions, total: sessions.length };
      },
      async deleteSession(input) {
        deletedSessionIds.push(input.sessionId);
        sessions = sessions.filter((item) => item.id !== input.sessionId);
      },
    };
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);

    await act(async () => {
      root?.render(
        <SessionTreePanel
          adapter={adapter}
          agentId="agent-1"
          title="Sessions"
        />,
      );
      await Promise.resolve();
    });

    const deleteButton = container.querySelector<HTMLButtonElement>(
      'button[aria-label="Delete session"]',
    );

    expect(deleteButton).not.toBeNull();

    await act(async () => {
      deleteButton?.click();
      await Promise.resolve();
    });

    expect(deletedSessionIds).toEqual(["s-1"]);
    expect(container.textContent).not.toContain("First session");
  });
});
