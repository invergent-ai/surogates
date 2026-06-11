import { describe, expect, it } from "vitest";

import {
  defaultSelectedMissionTaskId,
  formatMissionTimestamp,
  mergeMissionEvents,
  missionEventActorLabel,
  missionEventCategory,
  missionEventSummary,
  missionEventTaskId,
  missionTaskBlocks,
  missionTaskRailGroups,
  missionTaskStatusDotClass,
  missionTaskTitle,
  missionWorkerTaskCounts,
  stripMissionControlMarkup,
} from "../src/components/missions/mission-derive";
import type {
  AgentChatMissionEvent,
  AgentChatMissionEventSession,
  AgentChatMissionTask,
  AgentChatMissionWorker,
} from "../src/types";

function task(
  input: Partial<AgentChatMissionTask> & { id: string },
): AgentChatMissionTask {
  return {
    goal: `goal for ${input.id}`,
    status: "todo",
    attemptCount: 0,
    maxAttempts: 3,
    agentDefName: null,
    result: null,
    resultMetadata: null,
    parentIds: [],
    currentSessionId: null,
    createdAt: null,
    startedAt: null,
    completedAt: null,
    ...input,
  };
}

function event(
  input: Partial<AgentChatMissionEvent> & { id: number; type: string },
): AgentChatMissionEvent {
  return {
    sessionId: "sess-1",
    data: null,
    createdAt: "2026-06-11T12:00:00Z",
    ...input,
  };
}

describe("missionEventCategory", () => {
  it("maps the four named types and defaults to system", () => {
    expect(missionEventCategory("worker.spawned")).toBe("spawn");
    expect(missionEventCategory("iteration.summary")).toBe("output");
    expect(missionEventCategory("worker.complete")).toBe("done");
    expect(missionEventCategory("mission.evaluation.end")).toBe("verdict");
    expect(missionEventCategory("task.blocked")).toBe("system");
    expect(missionEventCategory("session.title_updated")).toBe("system");
  });
});

describe("missionEventSummary", () => {
  it("extracts the payload key matching each event type", () => {
    expect(
      missionEventSummary(
        event({ id: 1, type: "worker.spawned", data: { goal: "fix bugs" } }),
      ),
    ).toBe("fix bugs");
    expect(
      missionEventSummary(
        event({ id: 2, type: "worker.complete", data: { result: "all fixed" } }),
      ),
    ).toBe("all fixed");
    expect(
      missionEventSummary(
        event({
          id: 3,
          type: "iteration.summary",
          data: { summary: "reading files" },
        }),
      ),
    ).toBe("reading files");
    expect(
      missionEventSummary(
        event({
          id: 4,
          type: "mission.evaluation.end",
          data: { result: "needs_revision", feedback: "wait for verify" },
        }),
      ),
    ).toBe("needs_revision — wait for verify");
    expect(
      missionEventSummary(
        event({ id: 5, type: "task.blocked", data: { reason: "needs creds" } }),
      ),
    ).toBe("needs creds");
  });

  it("falls back to common keys then truncated JSON for unknown types", () => {
    expect(
      missionEventSummary(
        event({ id: 6, type: "custom.thing", data: { message: "hello" } }),
      ),
    ).toBe("hello");
    const json = missionEventSummary(
      event({ id: 7, type: "custom.thing", data: { foo: 1 } }),
    );
    expect(json).toContain("foo");
    expect(missionEventSummary(event({ id: 8, type: "x", data: null }))).toBe(
      "",
    );
  });
});

const SESSIONS: Record<string, AgentChatMissionEventSession> = {
  "coord-1": { taskId: null, agentDefName: null, kind: "coordinator" },
  "tsess-1": { taskId: "T1", agentDefName: "claude-coder", kind: "task" },
  "wsess-1": { taskId: null, agentDefName: null, kind: "worker" },
};

describe("missionEventTaskId", () => {
  it("prefers data.task_id, then the sessions map", () => {
    expect(
      missionEventTaskId(
        event({
          id: 1,
          type: "worker.spawned",
          sessionId: "coord-1",
          data: { task_id: "T9" },
        }),
        SESSIONS,
      ),
    ).toBe("T9");
    expect(
      missionEventTaskId(
        event({ id: 2, type: "iteration.summary", sessionId: "tsess-1" }),
        SESSIONS,
      ),
    ).toBe("T1");
    expect(
      missionEventTaskId(
        event({ id: 3, type: "tool.call", sessionId: "wsess-1" }),
        SESSIONS,
      ),
    ).toBeNull();
  });
});

describe("missionEventActorLabel", () => {
  it("labels coordinator, named agents, and falls back", () => {
    expect(
      missionEventActorLabel(
        event({ id: 1, type: "x", sessionId: "coord-1" }),
        SESSIONS,
      ),
    ).toBe("orchestrator");
    expect(
      missionEventActorLabel(
        event({ id: 2, type: "x", sessionId: "tsess-1" }),
        SESSIONS,
      ),
    ).toBe("claude-coder");
    expect(
      missionEventActorLabel(
        event({ id: 3, type: "x", sessionId: "wsess-1" }),
        SESSIONS,
      ),
    ).toBe("worker");
    expect(
      missionEventActorLabel(
        event({ id: 4, type: "x", sessionId: "aabbccdd-unknown" }),
        SESSIONS,
      ),
    ).toBe("aabbccdd");
  });
});

describe("missionTaskBlocks", () => {
  it("inverts parentIds across siblings", () => {
    const tasks = [
      task({ id: "A" }),
      task({ id: "B", parentIds: ["A"] }),
      task({ id: "C", parentIds: ["A", "B"] }),
    ];
    expect(missionTaskBlocks(tasks)).toEqual({ A: ["B", "C"], B: ["C"] });
  });
});

describe("missionTaskTitle", () => {
  it("takes the first non-empty line, collapsed and truncated", () => {
    expect(missionTaskTitle("\n\n  Fix the bug   now\nMore detail")).toBe(
      "Fix the bug now",
    );
    expect(missionTaskTitle("x".repeat(200)).length).toBeLessThanOrEqual(80);
  });
});

describe("missionTaskRailGroups", () => {
  it("groups in rail order and drops empty groups", () => {
    const tasks = [
      task({ id: "d", status: "done" }),
      task({ id: "r", status: "running" }),
      task({ id: "q1", status: "ready" }),
      task({ id: "q2", status: "todo" }),
      task({ id: "x", status: "cancelled" }),
    ];
    const groups = missionTaskRailGroups(tasks);
    expect(groups.map((g) => g.key)).toEqual([
      "running",
      "queued",
      "done",
      "failed",
    ]);
    expect(groups[1].tasks.map((t) => t.id)).toEqual(["q1", "q2"]);
  });

  it("routes unknown statuses into the queued group", () => {
    const groups = missionTaskRailGroups([
      task({ id: "weird", status: "someday" }),
    ]);
    expect(groups).toHaveLength(1);
    expect(groups[0].key).toBe("queued");
    expect(groups[0].tasks.map((t) => t.id)).toEqual(["weird"]);
  });
});

describe("defaultSelectedMissionTaskId", () => {
  it("picks the first non-terminal task in rail order", () => {
    expect(
      defaultSelectedMissionTaskId([
        task({ id: "d", status: "done" }),
        task({ id: "b", status: "blocked" }),
        task({ id: "r", status: "running" }),
      ]),
    ).toBe("r");
  });
  it("falls back to the first task when all are terminal", () => {
    expect(
      defaultSelectedMissionTaskId([
        task({ id: "d1", status: "done" }),
        task({ id: "d2", status: "failed" }),
      ]),
    ).toBe("d1");
    expect(defaultSelectedMissionTaskId([])).toBeNull();
  });
});

describe("mergeMissionEvents", () => {
  it("dedupes by id and sorts ascending", () => {
    const merged = mergeMissionEvents(
      [event({ id: 2, type: "a" }), event({ id: 1, type: "b" })],
      [event({ id: 2, type: "a2" }), event({ id: 3, type: "c" })],
    );
    expect(merged.map((e) => e.id)).toEqual([1, 2, 3]);
    expect(merged[1].type).toBe("a2");
  });
  it("returns the existing array untouched for an empty page", () => {
    const existing = [event({ id: 1, type: "a" })];
    expect(mergeMissionEvents(existing, [])).toBe(existing);
  });
});

describe("missionWorkerTaskCounts", () => {
  const worker = (
    input: Partial<AgentChatMissionWorker>,
  ): AgentChatMissionWorker => ({
    kind: "task",
    taskId: "T1",
    workerSessionId: "w1",
    agentDefName: "claude-coder",
    taskStatus: "running",
    sessionStatus: "active",
    latestEventId: null,
    latestEventKind: null,
    latestEventAt: null,
    latestEventSummary: null,
    transcriptUrl: "/chat/w1",
    ...input,
  });

  it("counts done vs running/blocked tasks for the worker's agent", () => {
    const tasks = [
      task({ id: "1", agentDefName: "claude-coder", status: "done" }),
      task({ id: "2", agentDefName: "claude-coder", status: "running" }),
      task({ id: "3", agentDefName: "claude-coder", status: "blocked" }),
      task({ id: "4", agentDefName: "codex-reviewer", status: "done" }),
    ];
    expect(missionWorkerTaskCounts(worker({}), tasks)).toEqual({
      completed: 1,
      inFlight: 2,
    });
  });
  it("returns null for non-task workers", () => {
    expect(missionWorkerTaskCounts(worker({ kind: "worker" }), [])).toBeNull();
  });
});

describe("stripMissionControlMarkup", () => {
  it("removes next_action blocks (attrs, multiline, self-closing)", () => {
    expect(
      stripMissionControlMarkup(
        'Done.\n\n<next_action complexity="low" summary="hide"> done </next_action>',
      ),
    ).toBe("Done.");
    expect(
      stripMissionControlMarkup(
        "Before <next_action>multi\nline</next_action> after",
      ),
    ).toBe("Before  after");
    expect(
      stripMissionControlMarkup('x <next_action complexity="high" /> y'),
    ).toBe("x  y");
    expect(stripMissionControlMarkup("plain text")).toBe("plain text");
  });

  it("is applied to worker.complete summaries", () => {
    expect(
      missionEventSummary(
        event({
          id: 9,
          type: "worker.complete",
          data: {
            result:
              'All done. <next_action complexity="low" summary="hide"> done </next_action>',
          },
        }),
      ),
    ).toBe("All done.");
  });
});

describe("misc", () => {
  it("status dot classes cover every task status", () => {
    const statuses = [
      "running",
      "blocked",
      "done",
      "failed",
      "cancelled",
      "ready",
      "todo",
    ];
    for (const s of statuses) {
      expect(missionTaskStatusDotClass(s)).toMatch(/^bg-/);
    }
  });
  it("formatMissionTimestamp survives bad input", () => {
    expect(formatMissionTimestamp("not-a-date")).toBe("not-a-date");
    expect(formatMissionTimestamp(null)).toBe("");
  });
});
