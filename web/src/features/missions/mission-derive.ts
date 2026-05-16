// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Pure derivers for the mission dashboard. Extracted so the logic can
// be unit-tested without rendering React.
import type {
  AgentChatMissionStatus,
  AgentChatMissionTask,
  AgentChatMissionWorker,
} from "@invergent/agent-chat-react";

/** Mission statuses that are still actively producing events. */
export const ACTIVE_STATUSES: ReadonlySet<AgentChatMissionStatus> = new Set([
  "active",
  "paused",
]);

/** Mission statuses past which no further work happens. */
export function isTerminalStatus(status: AgentChatMissionStatus): boolean {
  return !ACTIVE_STATUSES.has(status);
}

/** Bucket label for grouping tasks in the dashboard. */
export type TaskBucket = "in_flight" | "done" | "blocked" | "failed_or_cancelled";

export function taskBucket(status: string): TaskBucket {
  if (status === "done") return "done";
  if (status === "blocked") return "blocked";
  if (status === "failed" || status === "cancelled") {
    return "failed_or_cancelled";
  }
  return "in_flight";
}

export function groupTasksByBucket(
  tasks: AgentChatMissionTask[],
): Record<TaskBucket, AgentChatMissionTask[]> {
  const buckets: Record<TaskBucket, AgentChatMissionTask[]> = {
    in_flight: [],
    done: [],
    blocked: [],
    failed_or_cancelled: [],
  };
  for (const t of tasks) {
    buckets[taskBucket(t.status)].push(t);
  }
  return buckets;
}

/**
 * Derive a human-friendly activity label for a worker row. The server
 * exposes raw event metadata; the client picks the most informative
 * signal in this order: a recent tool.call summary, a recent
 * llm.response, else the worker's session status.
 */
export function deriveWorkerActivityLabel(
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
