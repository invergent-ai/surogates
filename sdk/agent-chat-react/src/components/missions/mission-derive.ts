// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Pure derivers for the mission dashboard. Extracted so the logic can
// be unit-tested without rendering React and shared across host apps.
import type {
  AgentChatMissionEvent,
  AgentChatMissionEventSession,
  AgentChatMissionStatus,
  AgentChatMissionTask,
  AgentChatMissionWorker,
} from "../../types";


/** Mission statuses that are still actively producing events. */
export const ACTIVE_MISSION_STATUSES: ReadonlySet<AgentChatMissionStatus> =
  new Set(["active", "paused"]);


/** Mission statuses past which no further work happens. */
export function isTerminalMissionStatus(
  status: AgentChatMissionStatus,
): boolean {
  return !ACTIVE_MISSION_STATUSES.has(status);
}


/** Bucket label for grouping tasks in the dashboard. */
export type MissionTaskBucket =
  | "in_flight"
  | "done"
  | "blocked"
  | "failed_or_cancelled";


export function missionTaskBucket(status: string): MissionTaskBucket {
  if (status === "done") return "done";
  if (status === "blocked") return "blocked";
  if (status === "failed" || status === "cancelled") {
    return "failed_or_cancelled";
  }
  return "in_flight";
}


export function groupMissionTasksByBucket(
  tasks: AgentChatMissionTask[],
): Record<MissionTaskBucket, AgentChatMissionTask[]> {
  const buckets: Record<MissionTaskBucket, AgentChatMissionTask[]> = {
    in_flight: [],
    done: [],
    blocked: [],
    failed_or_cancelled: [],
  };
  for (const t of tasks) {
    buckets[missionTaskBucket(t.status)].push(t);
  }
  return buckets;
}


/**
 * Derive a human-friendly activity label for a worker row. The server
 * exposes raw event metadata; the client picks the most informative
 * signal in this order: a recent tool.call summary, a recent
 * llm.response, else the worker's session status.
 */
export function deriveMissionWorkerActivityLabel(
  worker: AgentChatMissionWorker,
): string {
  const kind = worker.latestEventKind;
  if (kind === "tool.call" && worker.latestEventSummary) {
    return `tool: ${truncate(worker.latestEventSummary, 80)}`;
  }
  if (kind === "llm.response") {
    return "thinking";
  }
  if (kind === "tool.result") {
    return "received tool result";
  }
  if (kind && worker.latestEventSummary) {
    return `${kind}: ${truncate(worker.latestEventSummary, 60)}`;
  }
  return worker.sessionStatus || "active";
}


function truncate(s: string, max: number): string {
  if (s.length <= max) return s;
  return `${s.slice(0, max - 1)}…`;
}


// ---------------------------------------------------------------------------
// Mission event feed derivers (Activity tab, per-task history, live output)
// ---------------------------------------------------------------------------

export type MissionEventCategory =
  | "spawn"
  | "output"
  | "done"
  | "verdict"
  | "system";

export const MISSION_EVENT_CATEGORIES: readonly MissionEventCategory[] = [
  "spawn",
  "output",
  "done",
  "verdict",
  "system",
];

export function missionEventCategory(type: string): MissionEventCategory {
  switch (type) {
    case "worker.spawned":
      return "spawn";
    case "iteration.summary":
      return "output";
    case "worker.complete":
      return "done";
    case "mission.evaluation.end":
      return "verdict";
    default:
      return "system";
  }
}

function dataString(
  data: Record<string, unknown> | null,
  key: string,
): string | null {
  const v = data?.[key];
  return typeof v === "string" && v.trim().length > 0 ? v.trim() : null;
}

/** One human line per event row. Keys match the harness emit sites:
 * worker.spawned {goal}, worker.complete {result}, iteration.summary
 * {summary}, mission.evaluation.end {result, feedback, explanation},
 * task.blocked {reason}. */
export function missionEventSummary(event: AgentChatMissionEvent): string {
  const d = event.data ?? null;
  switch (event.type) {
    case "worker.spawned":
      return dataString(d, "goal") ?? "Task spawned";
    case "worker.complete":
      return dataString(d, "result") ?? "Worker complete";
    case "iteration.summary":
      return dataString(d, "summary") ?? "";
    case "mission.evaluation.end": {
      const result = dataString(d, "result");
      const feedback =
        dataString(d, "feedback") ?? dataString(d, "explanation");
      if (result && feedback) return `${result} — ${feedback}`;
      return result ?? feedback ?? "Evaluation finished";
    }
    case "task.blocked":
      return dataString(d, "reason") ?? "Task blocked";
    case "task.failed":
      return "Task failed";
    default: {
      const text =
        dataString(d, "summary") ??
        dataString(d, "text") ??
        dataString(d, "message") ??
        dataString(d, "reason");
      if (text) return text;
      if (!d) return "";
      try {
        return truncate(JSON.stringify(d), 160);
      } catch {
        return "";
      }
    }
  }
}

/** Attribute an event to a mission task: explicit ``data.task_id``
 * (coordinator-side worker.spawned / worker.complete / task.failed)
 * wins, else the session→task label map from the events endpoint. */
export function missionEventTaskId(
  event: AgentChatMissionEvent,
  sessions: Record<string, AgentChatMissionEventSession>,
): string | null {
  return (
    dataString(event.data ?? null, "task_id") ??
    sessions[event.sessionId]?.taskId ??
    null
  );
}

export function missionEventActorLabel(
  event: AgentChatMissionEvent,
  sessions: Record<string, AgentChatMissionEventSession>,
): string {
  const meta = sessions[event.sessionId];
  if (!meta) return event.sessionId.slice(0, 8);
  if (meta.kind === "coordinator") return "orchestrator";
  return meta.agentDefName ?? "worker";
}

/** Invert parentIds: taskId → ids of tasks that depend on it. */
export function missionTaskBlocks(
  tasks: AgentChatMissionTask[],
): Record<string, string[]> {
  const blocks: Record<string, string[]> = {};
  for (const t of tasks) {
    for (const parent of t.parentIds) {
      (blocks[parent] ??= []).push(t.id);
    }
  }
  return blocks;
}

/** Tasks only have a full ``goal`` — derive a short display title from
 * its first non-empty line. */
export function missionTaskTitle(goal: string, max = 80): string {
  const line =
    goal.split("\n").find((l) => l.trim().length > 0)?.trim() ?? goal.trim();
  return truncate(line.replace(/\s+/g, " "), max);
}

export type MissionTaskRailGroupKey =
  | "running"
  | "blocked"
  | "queued"
  | "done"
  | "failed";

export type MissionTaskRailGroup = {
  key: MissionTaskRailGroupKey;
  label: string;
  tasks: AgentChatMissionTask[];
};

const RAIL_GROUP_DEFS: ReadonlyArray<{
  key: MissionTaskRailGroupKey;
  label: string;
  match: (status: string) => boolean;
}> = [
  { key: "running", label: "Running", match: (s) => s === "running" },
  { key: "blocked", label: "Blocked", match: (s) => s === "blocked" },
  {
    key: "queued",
    label: "Queued",
    match: (s) => s === "ready" || s === "todo",
  },
  { key: "done", label: "Done", match: (s) => s === "done" },
  {
    key: "failed",
    label: "Failed / cancelled",
    match: (s) => s === "failed" || s === "cancelled",
  },
];

/** Left-rail grouping for the Tasks tab — rail order, empty groups
 * dropped. Statuses outside the known set land in "queued" so new
 * server states never make tasks vanish from the rail. */
export function missionTaskRailGroups(
  tasks: AgentChatMissionTask[],
): MissionTaskRailGroup[] {
  const grouped = new Set<string>();
  const groups = RAIL_GROUP_DEFS.map((def) => ({
    key: def.key,
    label: def.label,
    tasks: tasks.filter((t) => {
      const hit = def.match(t.status);
      if (hit) grouped.add(t.id);
      return hit;
    }),
  }));
  const stray = tasks.filter((t) => !grouped.has(t.id));
  if (stray.length > 0) {
    const queued = groups.find((g) => g.key === "queued");
    if (queued) queued.tasks.push(...stray);
  }
  return groups.filter((g) => g.tasks.length > 0);
}

const TERMINAL_TASK_STATUSES = new Set(["done", "failed", "cancelled"]);

/** First non-terminal task in rail order, else the first task. */
export function defaultSelectedMissionTaskId(
  tasks: AgentChatMissionTask[],
): string | null {
  if (tasks.length === 0) return null;
  const ordered = missionTaskRailGroups(tasks).flatMap((g) => g.tasks);
  const firstLive = ordered.find(
    (t) => !TERMINAL_TASK_STATUSES.has(t.status),
  );
  return (firstLive ?? ordered[0] ?? tasks[0]).id;
}

/** Merge an event page into the accumulated feed: dedupe by id (the
 * incoming row wins), sorted ascending. Returns the existing array
 * unchanged for an empty page so React state doesn't churn. */
export function mergeMissionEvents(
  existing: AgentChatMissionEvent[],
  incoming: AgentChatMissionEvent[],
): AgentChatMissionEvent[] {
  if (incoming.length === 0) return existing;
  const byId = new Map<number, AgentChatMissionEvent>();
  for (const e of existing) byId.set(e.id, e);
  for (const e of incoming) byId.set(e.id, e);
  return [...byId.values()].sort((a, b) => a.id - b.id);
}

export function missionTaskStatusDotClass(status: string): string {
  switch (status) {
    case "running":
      return "bg-primary";
    case "blocked":
      return "bg-amber-500";
    case "done":
      return "bg-emerald-500";
    case "failed":
      return "bg-destructive";
    case "cancelled":
      return "bg-muted-foreground";
    default:
      return "bg-foreground/30";
  }
}

/** Completed / in-flight counts for a task-kind worker card, grouping
 * mission tasks by the worker's agentDefName. Null for worker /
 * delegation kinds (they have no Task rows to count). */
export function missionWorkerTaskCounts(
  worker: AgentChatMissionWorker,
  tasks: AgentChatMissionTask[],
): { completed: number; inFlight: number } | null {
  if (worker.kind !== "task") return null;
  const mine = tasks.filter((t) => t.agentDefName === worker.agentDefName);
  return {
    completed: mine.filter((t) => t.status === "done").length,
    inFlight: mine.filter(
      (t) => t.status === "running" || t.status === "blocked",
    ).length,
  };
}

export function formatMissionTimestamp(iso: string | null): string {
  if (!iso) return "";
  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) return iso;
  return parsed.toLocaleTimeString();
}
