import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";

import { MissionActivityTab } from "../src/components/missions/mission-activity-tab";
import type { AgentChatMissionEvent } from "../src/types";

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

const EVENTS: AgentChatMissionEvent[] = [
  {
    id: 1,
    sessionId: "coord-1",
    type: "worker.spawned",
    data: { task_id: "T1", goal: "Implement" },
    createdAt: "2026-06-11T10:00:00Z",
  },
  {
    id: 2,
    sessionId: "tsess-1",
    type: "iteration.summary",
    data: { summary: "Reading files" },
    createdAt: "2026-06-11T10:01:00Z",
  },
  {
    id: 3,
    sessionId: "coord-1",
    type: "mission.evaluation.end",
    data: { result: "needs_revision", feedback: "Wait for verify" },
    createdAt: "2026-06-11T10:02:00Z",
  },
];

const FEED = {
  supported: true,
  events: EVENTS,
  sessions: {
    "coord-1": {
      taskId: null,
      agentDefName: null,
      kind: "coordinator" as const,
    },
    "tsess-1": {
      taskId: "T1",
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

describe("MissionActivityTab", () => {
  it("renders all events newest-first with actor labels", () => {
    render(<MissionActivityTab feed={FEED} />);
    const rows = Array.from(container!.querySelectorAll("li"));
    expect(rows).toHaveLength(3);
    expect(rows[0].textContent).toContain("needs_revision — Wait for verify");
    expect(rows[0].textContent).toContain("orchestrator");
    expect(rows[1].textContent).toContain("claude-coder");
    expect(rows[2].textContent).toContain("Implement");
  });

  it("filters by category chip", () => {
    render(<MissionActivityTab feed={FEED} />);
    const verdictChip = Array.from(container!.querySelectorAll("button")).find(
      (b) => b.textContent?.toLowerCase() === "verdict",
    )!;
    act(() => {
      verdictChip.click();
    });
    const rows = Array.from(container!.querySelectorAll("li"));
    expect(rows).toHaveLength(1);
    expect(rows[0].textContent).toContain("needs_revision");
  });

  it("shows an empty state without events", () => {
    render(
      <MissionActivityTab
        feed={{ supported: true, events: [], sessions: {} }}
      />,
    );
    expect(container!.textContent).toContain("No mission activity yet");
  });
});
