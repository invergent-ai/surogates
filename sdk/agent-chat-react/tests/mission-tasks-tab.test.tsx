import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { MissionTasksTab } from "../src/components/missions/mission-tasks-tab";
import type {
  AgentChatMissionEvent,
  AgentChatMissionTask,
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

function task(
  input: Partial<AgentChatMissionTask> & { id: string },
): AgentChatMissionTask {
  return {
    goal: `Goal of ${input.id}`,
    status: "todo",
    attemptCount: 1,
    maxAttempts: 3,
    agentDefName: "claude-coder",
    result: null,
    resultMetadata: null,
    parentIds: [],
    currentSessionId: null,
    createdAt: "2026-06-11T10:00:00Z",
    startedAt: null,
    completedAt: null,
    ...input,
  };
}

const TASKS: AgentChatMissionTask[] = [
  task({ id: "T-implement", goal: "Implement the feature", status: "done" }),
  task({
    id: "T-fix",
    goal: "Fix Findings from the review",
    status: "running",
    parentIds: ["T-implement"],
    currentSessionId: "sess-fix",
    startedAt: "2026-06-11T11:00:00Z",
  }),
  task({
    id: "T-verify",
    goal: "Verify everything",
    status: "todo",
    parentIds: ["T-fix"],
    agentDefName: "codex-reviewer",
  }),
];

const EVENTS: AgentChatMissionEvent[] = [
  {
    id: 1,
    sessionId: "sess-fix",
    type: "iteration.summary",
    data: { summary: "Applying fixes — 11 of 19 resolved." },
    createdAt: "2026-06-11T11:05:00Z",
  },
];

const FEED = {
  supported: true,
  events: EVENTS,
  sessions: {
    "sess-fix": {
      taskId: "T-fix",
      agentDefName: "claude-coder",
      kind: "task" as const,
    },
  },
};

function render(ui: React.ReactElement) {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => {
    root!.render(ui);
  });
}

describe("MissionTasksTab", () => {
  it("selects the first non-terminal task and shows its detail", () => {
    render(
      <MissionTasksTab tasks={TASKS} feed={FEED} onOpenTranscript={vi.fn()} />,
    );
    const detail = container!.querySelector('[data-testid="task-detail"]')!;
    expect(detail.textContent).toContain("Fix Findings from the review");
    expect(detail.textContent).toContain(
      "Applying fixes — 11 of 19 resolved.",
    );
  });

  it("renders depends-on and blocks chips and navigates on click", () => {
    render(
      <MissionTasksTab tasks={TASKS} feed={FEED} onOpenTranscript={vi.fn()} />,
    );
    const detail = container!.querySelector('[data-testid="task-detail"]')!;
    // Selected task T-fix depends on T-implement and blocks T-verify.
    expect(detail.textContent).toContain("Implement the feature");
    expect(detail.textContent).toContain("Verify everything");

    const blocksChip = Array.from(
      detail.querySelectorAll("button[data-task-chip]"),
    ).find((b) => b.textContent?.includes("Verify everything"))!;
    act(() => {
      (blocksChip as HTMLButtonElement).click();
    });
    const after = container!.querySelector('[data-testid="task-detail"]')!;
    // T-verify is now selected: its agent badge is unique to its detail
    // panel (T-fix's panel showed claude-coder).
    expect(after.textContent).toContain("codex-reviewer");
    // ...and its own depends-on chip points back at T-fix.
    expect(
      Array.from(after.querySelectorAll("button[data-task-chip]")).some(
        (b) => b.textContent?.includes("Fix Findings"),
      ),
    ).toBe(true);
  });

  it("offers the worker session link for the selected task", () => {
    const onOpenTranscript = vi.fn();
    render(
      <MissionTasksTab
        tasks={TASKS}
        feed={FEED}
        onOpenTranscript={onOpenTranscript}
      />,
    );
    const btn = container!.querySelector(
      '[data-testid="view-session"]',
    ) as HTMLButtonElement;
    act(() => {
      btn.click();
    });
    expect(onOpenTranscript).toHaveBeenCalledWith("sess-fix");
  });

  it("renders an empty state without tasks", () => {
    render(<MissionTasksTab tasks={[]} feed={FEED} />);
    expect(container!.textContent).toContain("No tasks spawned yet");
  });
});
