import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";
import { NO_BROWSER_ADAPTER } from "../src/adapter-context";
import { MissionsPanel } from "../src/components/missions/missions-panel";
import type {
  AgentChatAdapter,
  AgentChatArtifactPayload,
  AgentChatMissionList,
  AgentChatMissionSummary,
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


function mission(
  input: Partial<AgentChatMissionSummary> & { id: string },
): AgentChatMissionSummary {
  return {
    orgId: "org-1",
    userId: "user-1",
    serviceAccountId: null,
    sessionId: "sess-1",
    agentId: "agent-1",
    description: "Train a 0.6B model",
    rubric: "gsm8k >= 0.8",
    status: "active",
    iteration: 0,
    maxIterations: 20,
    lastEvaluationResult: null,
    lastEvaluationExplanation: null,
    lastEvaluationFeedback: null,
    lastEvaluationAt: null,
    evaluatorParseFailures: 0,
    pausedReason: null,
    cancelledReason: null,
    createdAt: "2026-01-01T00:00:00Z",
    updatedAt: "2026-01-01T00:00:00Z",
    ...input,
  };
}


function createAdapter(
  missions: AgentChatMissionSummary[],
  overrides: Partial<AgentChatAdapter> = {},
): AgentChatAdapter {
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
      throw new Error("not used by missions tests");
    },
    async submitClarifyResponse() {
      return { eventId: 1 };
    },
    async getWorkspaceTree(): Promise<AgentChatWorkspaceTree> {
      return { root: "workspace", entries: [], truncated: false };
    },
    async getWorkspaceFile(): Promise<AgentChatWorkspaceFile> {
      throw new Error("not used by missions tests");
    },
    async uploadWorkspaceFile(): Promise<AgentChatWorkspaceUpload> {
      return { path: "uploaded.txt", size: 4 };
    },
    async deleteWorkspaceFile() {},
    getWorkspaceDownloadUrl(input) {
      return `/api/v1/sessions/${input.sessionId}/workspace/download?path=${encodeURIComponent(input.path)}`;
    },
    openEventStream() {
      throw new Error("not used by missions tests");
    },
    async listMissions(): Promise<AgentChatMissionList> {
      return { missions };
    },
    ...overrides,
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


describe("MissionsPanel", () => {
  it("renders active and paused missions from the adapter", async () => {
    const adapter = createAdapter([
      mission({
        id: "m-active",
        description: "Train 0.6B model",
        status: "active",
        iteration: 2,
        maxIterations: 20,
        lastEvaluationResult: "needs_revision",
        lastEvaluationAt: "2026-01-01T00:00:00Z",
      }),
      mission({
        id: "m-paused",
        description: "Backfill embeddings",
        status: "paused",
        pausedReason: "awaiting GPU quota",
      }),
    ]);
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);

    await act(async () => {
      root?.render(<MissionsPanel adapter={adapter} agentId="agent-1" />);
      await Promise.resolve();
    });

    expect(container.textContent).toContain("Missions");
    expect(container.textContent).toContain("Train 0.6B model");
    expect(container.textContent).toContain("active");
    expect(container.textContent).toContain("Iter 2/20");
    expect(container.textContent).toContain("Verdict: needs_revision");
    expect(container.textContent).toContain("Backfill embeddings");
    expect(container.textContent).toContain("paused");
    expect(container.textContent).toContain("Paused: awaiting GPU quota");
    // Active missions sort before paused.
    const text = container.textContent ?? "";
    expect(text.indexOf("Train 0.6B model")).toBeLessThan(
      text.indexOf("Backfill embeddings"),
    );
  });

  it("requests all mission statuses by default and renders completed missions", async () => {
    const listMissions = vi.fn(
      async (input?: { agentId?: string; status?: string }) => {
        expect(input?.agentId).toBe("agent-1");
        expect(input?.status).toBeUndefined();
        return {
          missions: [
            mission({
              id: "m-satisfied",
              description: "Ship final report",
              status: "satisfied",
              iteration: 4,
              maxIterations: 4,
              lastEvaluationResult: "satisfied",
            }),
          ],
        };
      },
    );
    const adapter = createAdapter([], { listMissions });
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);

    await act(async () => {
      root?.render(<MissionsPanel adapter={adapter} agentId="agent-1" />);
      await Promise.resolve();
    });

    expect(listMissions).toHaveBeenCalledTimes(1);
    expect(container.textContent).toContain("Ship final report");
    expect(container.textContent).toContain("satisfied");
    expect(container.textContent).toContain("Verdict: satisfied");
  });

  it("collapses and expands the missions list from the header", async () => {
    const adapter = createAdapter([
      mission({ id: "m-1", description: "Mission alpha" }),
    ]);
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);

    await act(async () => {
      root?.render(<MissionsPanel adapter={adapter} agentId="agent-1" />);
      await Promise.resolve();
    });

    const toggle = container.querySelector<HTMLButtonElement>(
      'button[aria-expanded="true"]',
    );
    expect(toggle).not.toBeNull();
    expect(container.textContent).toContain("Mission alpha");

    await act(async () => {
      toggle?.click();
      await Promise.resolve();
    });

    expect(toggle?.getAttribute("aria-expanded")).toBe("false");
    expect(container.textContent).toContain("Missions");
    expect(container.textContent).not.toContain("Mission alpha");

    await act(async () => {
      toggle?.click();
      await Promise.resolve();
    });

    expect(toggle?.getAttribute("aria-expanded")).toBe("true");
    expect(container.textContent).toContain("Mission alpha");
  });

  it("opens the mission dashboard when a row is clicked", async () => {
    const opened: string[] = [];
    const adapter = createAdapter([
      mission({ id: "m-1", description: "Mission alpha" }),
    ]);
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);

    await act(async () => {
      root?.render(
        <MissionsPanel
          adapter={adapter}
          agentId="agent-1"
          onMissionSelect={(id) => opened.push(id)}
        />,
      );
      await Promise.resolve();
    });

    const row = container.querySelector<HTMLDivElement>(
      'div[role="button"][aria-label="Open mission Mission alpha"]',
    );
    expect(row).not.toBeNull();
    await act(async () => {
      row?.click();
      await Promise.resolve();
    });
    expect(opened).toEqual(["m-1"]);
  });

  it("invokes pauseMission and cancelMission via inline actions", async () => {
    const paused: string[] = [];
    const cancelled: string[] = [];
    const adapter = createAdapter(
      [mission({ id: "m-1", description: "Mission alpha" })],
      {
        async pauseMission(input) {
          paused.push(input.missionId);
        },
        async cancelMission(input) {
          cancelled.push(input.missionId);
        },
      },
    );
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);

    await act(async () => {
      root?.render(
        <MissionsPanel adapter={adapter} agentId="agent-1" />,
      );
      await Promise.resolve();
    });

    const pauseBtn = container.querySelector<HTMLButtonElement>(
      'button[aria-label="Pause mission"]',
    );
    expect(pauseBtn).not.toBeNull();
    await act(async () => {
      pauseBtn?.click();
      await Promise.resolve();
    });
    expect(paused).toEqual(["m-1"]);

    const cancelBtn = container.querySelector<HTMLButtonElement>(
      'button[aria-label="Cancel mission"]',
    );
    expect(cancelBtn).not.toBeNull();
    await act(async () => {
      cancelBtn?.click();
      await Promise.resolve();
    });
    expect(cancelled).toEqual(["m-1"]);
  });

  it("renders nothing when the adapter omits listMissions", async () => {
    const adapter = createAdapter([], { listMissions: undefined });
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);

    await act(async () => {
      root?.render(<MissionsPanel adapter={adapter} agentId="agent-1" />);
      await Promise.resolve();
    });

    expect(container.textContent).not.toContain("Missions");
  });

  it("surfaces adapter errors as an inline message", async () => {
    const adapter = createAdapter([], {
      listMissions: vi.fn(async () => {
        throw new Error("server down");
      }),
    });
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);

    await act(async () => {
      root?.render(<MissionsPanel adapter={adapter} agentId="agent-1" />);
      await Promise.resolve();
    });
    // Allow the rejected promise to settle.
    await act(async () => {
      await Promise.resolve();
    });

    expect(container.textContent).toContain("server down");
  });
});
