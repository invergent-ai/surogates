import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { MissionDashboard } from "../src/components/missions/mission-dashboard";
import type {
  AgentChatAdapter,
  AgentChatMissionEventsPage,
  AgentChatMissionSummary,
  AgentChatMissionTask,
  AgentChatMissionWorker,
} from "../src/types";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT =
  true;

let root: Root | null = null;
let container: HTMLDivElement | null = null;

afterEach(() => {
  if (root) act(() => root!.unmount());
  container?.remove();
  root = null;
  container = null;
});

const MISSION: AgentChatMissionSummary = {
  id: "m1",
  orgId: "org-1",
  userId: "user-1",
  serviceAccountId: null,
  sessionId: "coord-1",
  agentId: "agent-1",
  description: "Build the URL shortener",
  rubric: "All tests pass",
  status: "active",
  iteration: 2,
  maxIterations: 20,
  lastEvaluationResult: "needs_revision",
  lastEvaluationExplanation: "Verify has not run yet.",
  lastEvaluationFeedback: "Wait for Fix Findings and Verify to complete.",
  lastEvaluationAt: "2026-06-11T10:02:00Z",
  evaluatorParseFailures: 0,
  pausedReason: null,
  cancelledReason: null,
  createdAt: "2026-06-11T09:00:00Z",
  updatedAt: "2026-06-11T10:02:00Z",
};

const TASKS: AgentChatMissionTask[] = [
  {
    id: "T-fix",
    goal: "Fix Findings from the review",
    status: "running",
    attemptCount: 1,
    maxAttempts: 3,
    agentDefName: "claude-coder",
    result: null,
    resultMetadata: null,
    parentIds: [],
    currentSessionId: "sess-fix",
    createdAt: "2026-06-11T09:30:00Z",
    startedAt: "2026-06-11T09:31:00Z",
    completedAt: null,
  },
];

const WORKERS: AgentChatMissionWorker[] = [
  {
    kind: "task",
    taskId: "T-fix",
    workerSessionId: "sess-fix",
    agentDefName: "claude-coder",
    taskStatus: "running",
    sessionStatus: "active",
    latestEventId: 2,
    latestEventKind: "iteration.summary",
    latestEventAt: "2026-06-11T10:01:00Z",
    latestEventSummary: "Applying fixes",
    transcriptUrl: "/chat/sess-fix",
  },
];

const EVENTS_PAGE: AgentChatMissionEventsPage = {
  events: [
    {
      id: 1,
      sessionId: "coord-1",
      type: "worker.spawned",
      data: { task_id: "T-fix", goal: "Fix Findings from the review" },
      createdAt: "2026-06-11T09:30:00Z",
    },
    {
      id: 2,
      sessionId: "sess-fix",
      type: "iteration.summary",
      data: { summary: "Applying fixes — 11 of 19 resolved." },
      createdAt: "2026-06-11T10:01:00Z",
    },
  ],
  sessions: {
    "coord-1": { taskId: null, agentDefName: null, kind: "coordinator" },
    "sess-fix": {
      taskId: "T-fix",
      agentDefName: "claude-coder",
      kind: "task",
    },
  },
};

function makeAdapter(overrides: Partial<AgentChatAdapter> = {}) {
  return {
    getMission: vi.fn().mockResolvedValue(MISSION),
    getMissionTasks: vi.fn().mockResolvedValue({ tasks: TASKS }),
    getMissionWorkers: vi.fn().mockResolvedValue({ workers: WORKERS }),
    listMissionEvents: vi.fn().mockResolvedValue(EVENTS_PAGE),
    pauseMission: vi.fn().mockResolvedValue(undefined),
    resumeMission: vi.fn().mockResolvedValue(undefined),
    cancelMission: vi.fn().mockResolvedValue(undefined),
    ...overrides,
  } as unknown as AgentChatAdapter;
}

async function mount(adapter: AgentChatAdapter) {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => {
    root!.render(
      <MissionDashboard
        adapter={adapter}
        missionId="m1"
        pollIntervalMs={3_600_000}
      />,
    );
  });
  await act(async () => {
    await Promise.resolve();
  });
}

function clickTab(label: string) {
  const tab = Array.from(container!.querySelectorAll('[role="tab"]')).find(
    (t) => t.textContent?.includes(label),
  ) as HTMLElement;
  // Radix TabsTrigger activates on mousedown (not click).
  act(() => {
    tab.dispatchEvent(
      new MouseEvent("mousedown", { bubbles: true, button: 0 }),
    );
    tab.click();
  });
}

describe("MissionDashboard", () => {
  it("defaults to the Tasks tab with master-detail content", async () => {
    await mount(makeAdapter());
    expect(container!.textContent).toContain("Build the URL shortener");
    const detail = container!.querySelector('[data-testid="task-detail"]')!;
    expect(detail.textContent).toContain("Fix Findings from the review");
    expect(detail.textContent).toContain(
      "Applying fixes — 11 of 19 resolved.",
    );
  });

  it("renders Activity, Workers, and Metadata tabs", async () => {
    await mount(makeAdapter());
    clickTab("Activity");
    expect(container!.textContent).toContain("orchestrator");
    clickTab("Workers");
    expect(container!.textContent).toContain("Completed");
    clickTab("Metadata");
    expect(container!.textContent).toContain("Last verdict — needs_revision");
  });

  it("hides the Activity tab when the adapter lacks listMissionEvents", async () => {
    await mount(makeAdapter({ listMissionEvents: undefined }));
    const tabs = Array.from(container!.querySelectorAll('[role="tab"]')).map(
      (t) => t.textContent ?? "",
    );
    expect(tabs.some((t) => t.includes("Activity"))).toBe(false);
    expect(tabs.some((t) => t.includes("Tasks"))).toBe(true);
  });
});
