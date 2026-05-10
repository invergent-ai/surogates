import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";
import { NO_BROWSER_ADAPTER } from "../src/adapter-context";
import { ScheduledWorkPanel } from "../src/components/scheduled/scheduled-work-panel";
import type {
  AgentChatAdapter,
  AgentChatArtifactPayload,
  AgentChatScheduledWorkItem,
  AgentChatScheduledWorkList,
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

function scheduledWork(
  input: Partial<AgentChatScheduledWorkItem> & { id: string },
): AgentChatScheduledWorkItem {
  const { id, ...rest } = input;
  return {
    id,
    agentId: "agent-1",
    name: "Scheduled work",
    prompt: "check status",
    status: "active",
    kind: "cron",
    source: "tool",
    scheduleDisplay: "Every 10 minutes",
    runCount: 0,
    createdAt: "2026-01-01T00:00:00Z",
    updatedAt: "2026-01-01T00:00:00Z",
    ...rest,
  };
}

function createAdapter(items: AgentChatScheduledWorkItem[]): AgentChatAdapter {
  return {
    ...NO_BROWSER_ADAPTER,
    async listSessions(): Promise<AgentChatSessionList> {
      return { sessions: [], total: 0 };
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
    async getArtifact(): Promise<AgentChatArtifactPayload> {
      throw new Error("not used by scheduled work tests");
    },
    async submitClarifyResponse() {
      return { eventId: 1 };
    },
    async getWorkspaceTree(): Promise<AgentChatWorkspaceTree> {
      return { root: "workspace", entries: [], truncated: false };
    },
    async getWorkspaceFile(): Promise<AgentChatWorkspaceFile> {
      throw new Error("not used by scheduled work tests");
    },
    async uploadWorkspaceFile(): Promise<AgentChatWorkspaceUpload> {
      return { path: "uploaded.txt", size: 4 };
    },
    async deleteWorkspaceFile() {},
    getWorkspaceDownloadUrl(input) {
      return `/api/v1/sessions/${input.sessionId}/workspace/download?path=${encodeURIComponent(input.path)}`;
    },
    openEventStream() {
      throw new Error("not used by scheduled work tests");
    },
    async listScheduledWork(): Promise<AgentChatScheduledWorkList> {
      return { items, total: items.length };
    },
  };
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

describe("ScheduledWorkPanel", () => {
  it("renders active loop and cron schedules from the adapter", async () => {
    const adapter = createAdapter([
      scheduledWork({
        id: "loop-1",
        kind: "dynamic_loop",
        name: "Bitcoin monitor",
        prompt: "check bitcoin price",
        scheduleDisplay: "Dynamic loop (1 minute to 1 hour)",
        nextRunAt: "2026-01-01T00:10:00Z",
        lastSessionId: "run-1",
        runCount: 2,
      }),
      scheduledWork({
        id: "cron-1",
        kind: "cron",
        name: "Deploy check",
        prompt: "check deploys",
        scheduleDisplay: "Every 5 minutes",
        runCount: 7,
      }),
    ]);
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);

    await act(async () => {
      root?.render(<ScheduledWorkPanel adapter={adapter} agentId="agent-1" />);
      await Promise.resolve();
    });

    expect(container.textContent).toContain("Scheduled Work");
    expect(container.textContent).toContain("Bitcoin monitor");
    expect(container.textContent).toContain("Dynamic loop");
    expect(container.textContent).not.toContain(
      "Dynamic loop (1 minute to 1 hour)",
    );
    expect(container.textContent).toContain("Next");
    expect(container.textContent).toContain("Deploy check");
    expect(container.textContent).toContain("Cron");
    expect(container.textContent).toContain("Every 5 minutes");
    expect(container.textContent).toContain("7 runs");
    expect(container.textContent).not.toContain(" · ");
    expect(
      (container.textContent ?? "").indexOf("Bitcoin monitor"),
    ).toBeLessThan((container.textContent ?? "").indexOf("Dynamic loop"));
  });

  it("collapses and expands scheduled work from the header", async () => {
    const adapter = createAdapter([
      scheduledWork({
        id: "loop-1",
        kind: "dynamic_loop",
        name: "Bitcoin monitor",
      }),
    ]);
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);

    await act(async () => {
      root?.render(<ScheduledWorkPanel adapter={adapter} agentId="agent-1" />);
      await Promise.resolve();
    });

    const toggle = container.querySelector<HTMLButtonElement>(
      'button[aria-expanded="true"]',
    );
    expect(toggle).not.toBeNull();
    expect(container.textContent).toContain("Bitcoin monitor");

    await act(async () => {
      toggle?.click();
      await Promise.resolve();
    });

    expect(toggle?.getAttribute("aria-expanded")).toBe("false");
    expect(container.textContent).toContain("Scheduled Work");
    expect(container.textContent).not.toContain("Bitcoin monitor");

    await act(async () => {
      toggle?.click();
      await Promise.resolve();
    });

    expect(toggle?.getAttribute("aria-expanded")).toBe("true");
    expect(container.textContent).toContain("Bitcoin monitor");
  });

  it("opens the last run from the row when available", async () => {
    const opened: string[] = [];
    const adapter = createAdapter([
      scheduledWork({
        id: "loop-1",
        lastSessionId: "run-1",
      }),
    ]);
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);

    await act(async () => {
      root?.render(
        <ScheduledWorkPanel
          adapter={adapter}
          agentId="agent-1"
          onSessionSelect={(sessionId) => opened.push(sessionId)}
        />,
      );
      await Promise.resolve();
    });

    const openButton = container.querySelector<HTMLButtonElement>(
      'button[aria-label="Open last run"]',
    );
    expect(openButton).toBeNull();
    const row = container.querySelector<HTMLElement>(
      '[role="button"][aria-label="Open last run for Scheduled work"]',
    );
    expect(row).not.toBeNull();

    await act(async () => {
      row?.click();
      await Promise.resolve();
    });

    expect(opened).toEqual(["run-1"]);
  });

  it("runs and cancels schedules through adapter actions", async () => {
    let items = [
      scheduledWork({
        id: "one-shot-1",
        kind: "one_shot",
        name: "Deploy check",
        lastSessionId: "run-1",
        repeatLimit: 1,
      }),
    ];
    const runs: string[] = [];
    const cancels: string[] = [];
    const opened: string[] = [];
    const adapter: AgentChatAdapter = {
      ...createAdapter(items),
      async listScheduledWork() {
        return { items, total: items.length };
      },
      async runScheduledWorkNow(input) {
        runs.push(input.scheduleId);
      },
      async cancelScheduledWork(input) {
        cancels.push(input.scheduleId);
        items = items.filter((item) => item.id !== input.scheduleId);
      },
    };
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);

    await act(async () => {
      root?.render(
        <ScheduledWorkPanel
          adapter={adapter}
          agentId="agent-1"
          onSessionSelect={(sessionId) => opened.push(sessionId)}
        />,
      );
      await Promise.resolve();
    });

    const runButton = container.querySelector<HTMLButtonElement>(
      'button[aria-label="Run schedule now"]',
    );
    const cancelButton = container.querySelector<HTMLButtonElement>(
      'button[aria-label="Cancel schedule"]',
    );
    expect(runButton).not.toBeNull();
    expect(cancelButton).not.toBeNull();

    await act(async () => {
      runButton?.click();
      await Promise.resolve();
    });
    await act(async () => {
      cancelButton?.click();
      await Promise.resolve();
    });

    expect(runs).toEqual(["one-shot-1"]);
    expect(cancels).toEqual(["one-shot-1"]);
    expect(opened).toEqual([]);
    expect(container.textContent).not.toContain("Deploy check");
  });

  it("does not show run-now for recurring loop schedules", async () => {
    const adapter: AgentChatAdapter = {
      ...createAdapter([
        scheduledWork({
          id: "loop-1",
          kind: "dynamic_loop",
          name: "Bitcoin monitor",
          repeatLimit: null,
        }),
      ]),
      async runScheduledWorkNow() {
        throw new Error("loop schedules should not expose run-now");
      },
      async cancelScheduledWork() {},
    };
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);

    await act(async () => {
      root?.render(<ScheduledWorkPanel adapter={adapter} agentId="agent-1" />);
      await Promise.resolve();
    });

    const runButton = container.querySelector<HTMLButtonElement>(
      'button[aria-label="Run schedule now"]',
    );
    const cancelButton = container.querySelector<HTMLButtonElement>(
      'button[aria-label="Cancel schedule"]',
    );
    expect(runButton).toBeNull();
    expect(cancelButton).not.toBeNull();
  });

  it("does not render when the adapter does not support scheduled work", async () => {
    const adapter = createAdapter([]);
    delete adapter.listScheduledWork;
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);

    await act(async () => {
      root?.render(<ScheduledWorkPanel adapter={adapter} agentId="agent-1" />);
      await Promise.resolve();
    });

    expect(container.textContent).toBe("");
  });
});
