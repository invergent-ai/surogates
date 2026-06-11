// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Tasks tab — master-detail view of the mission's task DAG. Left rail
// groups tasks by status; the right panel leads with a status-dependent
// outcome card (result when done, live output while running, waiting /
// blocked context otherwise), then dependency chips (depends-on from
// parentIds, blocks from the client-side inversion), the markdown
// brief, and the per-task slice of the mission event feed.
// Event-driven sections render only when the adapter supports
// listMissionEvents.
import { useMemo, useState } from "react";
import type { ReactNode } from "react";
import {
  ArrowRight,
  Ban,
  CheckCircle2,
  CircleDot,
  Clock,
  XCircle,
} from "lucide-react";

import { MessageResponse } from "../ai-elements/message";
import { Badge } from "../ui/badge";
import { Button } from "../ui/button";
import { Card, CardContent } from "../ui/card";

import type {
  AgentChatMissionEvent,
  AgentChatMissionTask,
} from "../../types";

import {
  defaultSelectedMissionTaskId,
  formatMissionTimestamp,
  missionEventCategory,
  missionEventSummary,
  missionEventTaskId,
  missionTaskBlocks,
  missionTaskRailGroups,
  missionTaskStatusDotClass,
  missionTaskTitle,
} from "./mission-derive";
import type { MissionEventsFeed } from "./use-mission-events";


export interface MissionTasksTabProps {
  tasks: AgentChatMissionTask[];
  feed: MissionEventsFeed;
  onOpenTranscript?: (workerSessionId: string) => void;
}


const TASK_STATUS_BADGE_CLASS: Record<string, string> = {
  running: "border-primary/30 bg-primary/10 text-primary",
  blocked: "border-amber-500/30 bg-amber-500/10 text-amber-600",
  done: "border-emerald-500/30 bg-emerald-500/10 text-emerald-600",
  failed: "border-destructive/30 bg-destructive/10 text-destructive",
  cancelled: "border-border bg-muted text-muted-foreground",
};

function statusBadgeClass(status: string): string {
  return (
    TASK_STATUS_BADGE_CLASS[status] ??
    "border-foreground/20 bg-foreground/5 text-foreground/70"
  );
}


function TaskChip({
  task,
  onSelect,
}: {
  task: AgentChatMissionTask;
  onSelect: (taskId: string) => void;
}) {
  return (
    <button
      type="button"
      data-task-chip
      onClick={() => onSelect(task.id)}
      className="inline-flex max-w-full items-center gap-1.5 rounded-full border border-border/60 bg-background px-2.5 py-1 text-xs hover:bg-muted/40"
    >
      <span
        className={`size-1.5 shrink-0 rounded-full ${missionTaskStatusDotClass(task.status)}`}
      />
      <span className="truncate text-foreground/85">
        {missionTaskTitle(task.goal, 40)}
      </span>
      <span className="shrink-0 font-mono text-[9px] text-muted-foreground/60">
        {task.id.slice(0, 8)}
      </span>
    </button>
  );
}


function TaskRailRow({
  task,
  selected,
  onSelect,
}: {
  task: AgentChatMissionTask;
  selected: boolean;
  onSelect: (taskId: string) => void;
}) {
  const preview =
    task.status === "done" && task.result ? task.result : task.goal;
  return (
    <button
      type="button"
      onClick={() => onSelect(task.id)}
      className={`w-full rounded-md border px-3 py-2 text-left text-sm transition-colors ${
        selected
          ? "border-primary/40 bg-primary/5"
          : "border-transparent hover:bg-muted/40"
      }`}
    >
      <div className="flex items-center gap-2">
        <span
          className={`size-2 shrink-0 rounded-full ${missionTaskStatusDotClass(task.status)}`}
        />
        <span className="min-w-0 flex-1 truncate font-medium text-foreground/90">
          {missionTaskTitle(task.goal, 60)}
        </span>
        <span className="shrink-0 font-mono text-[9px] text-muted-foreground/50">
          {task.id.slice(0, 8)}
        </span>
      </div>
      <div className="mt-1 flex items-center gap-2 pl-4">
        {task.agentDefName ? (
          <span className="shrink-0 text-[10px] text-muted-foreground/70">
            {task.agentDefName}
          </span>
        ) : null}
        <span className="line-clamp-2 min-w-0 flex-1 text-xs text-muted-foreground/70">
          {preview}
        </span>
      </div>
    </button>
  );
}


export function MissionTasksTab({
  tasks,
  feed,
  onOpenTranscript,
}: MissionTasksTabProps) {
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const groups = useMemo(() => missionTaskRailGroups(tasks), [tasks]);
  const blocks = useMemo(() => missionTaskBlocks(tasks), [tasks]);
  const byId = useMemo(() => new Map(tasks.map((t) => [t.id, t])), [tasks]);

  // Fall back to the default pick when nothing is selected or the
  // selected task disappeared from the feed.
  const effectiveId =
    selectedId && byId.has(selectedId)
      ? selectedId
      : defaultSelectedMissionTaskId(tasks);
  const selected = effectiveId ? (byId.get(effectiveId) ?? null) : null;

  const taskEvents = useMemo(() => {
    if (!selected || !feed.supported) return [];
    return feed.events.filter(
      (e) => missionEventTaskId(e, feed.sessions) === selected.id,
    );
  }, [selected, feed]);

  const liveOutput = useMemo(() => {
    if (!selected || selected.status !== "running") return null;
    const outputs = taskEvents.filter(
      (e) => missionEventCategory(e.type) === "output",
    );
    return outputs.length > 0 ? outputs[outputs.length - 1] : null;
  }, [selected, taskEvents]);

  if (tasks.length === 0) {
    return (
      <div className="py-8 text-center text-sm text-muted-foreground/60">
        No tasks spawned yet.
      </div>
    );
  }

  return (
    <div className="flex h-full min-h-0 gap-4">
      {/* ----- Left rail -------------------------------------------- */}
      <div className="w-72 shrink-0 space-y-4 overflow-y-auto pr-1">
        {groups.map((group) => (
          <div key={group.key}>
            <div className="mb-1 px-1 font-mono text-[10px] uppercase tracking-widest text-muted-foreground/70">
              {group.label} ({group.tasks.length})
            </div>
            <div className="space-y-1">
              {group.tasks.map((t) => (
                <TaskRailRow
                  key={t.id}
                  task={t}
                  selected={t.id === effectiveId}
                  onSelect={setSelectedId}
                />
              ))}
            </div>
          </div>
        ))}
      </div>

      {/* ----- Detail panel ----------------------------------------- */}
      <div
        data-testid="task-detail"
        className="min-w-0 flex-1 space-y-4 overflow-y-auto"
      >
        {selected ? (
          <TaskDetail
            task={selected}
            allTasks={byId}
            blockedTaskIds={blocks[selected.id] ?? []}
            taskEvents={taskEvents}
            liveOutput={liveOutput}
            eventsSupported={feed.supported}
            onSelect={setSelectedId}
            onOpenTranscript={onOpenTranscript}
          />
        ) : null}
      </div>
    </div>
  );
}


function TaskDetail({
  task,
  allTasks,
  blockedTaskIds,
  taskEvents,
  liveOutput,
  eventsSupported,
  onSelect,
  onOpenTranscript,
}: {
  task: AgentChatMissionTask;
  allTasks: Map<string, AgentChatMissionTask>;
  blockedTaskIds: string[];
  taskEvents: AgentChatMissionEvent[];
  liveOutput: AgentChatMissionEvent | null;
  eventsSupported: boolean;
  onSelect: (taskId: string) => void;
  onOpenTranscript?: (workerSessionId: string) => void;
}) {
  const parents = task.parentIds
    .map((id) => allTasks.get(id))
    .filter((t): t is AgentChatMissionTask => Boolean(t));
  const children = blockedTaskIds
    .map((id) => allTasks.get(id))
    .filter((t): t is AgentChatMissionTask => Boolean(t));
  // Started → completed range for terminal tasks; "not started" until a
  // worker claims an attempt (mirrors the dispatcher's started_at write).
  const timeLabel =
    task.startedAt && task.completedAt
      ? `${formatMissionTimestamp(task.startedAt)} → ${formatMissionTimestamp(task.completedAt)}`
      : task.startedAt
        ? `started ${formatMissionTimestamp(task.startedAt)}`
        : "not started";
  const outcome = deriveTaskOutcome({ task, taskEvents, liveOutput, parents });

  return (
    <div className="space-y-5">
      {/* Header */}
      <div className="space-y-2">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 className="min-w-0 flex-1 truncate text-lg font-semibold text-foreground">
            {missionTaskTitle(task.goal)}
          </h2>
          <span className="shrink-0 font-mono text-[10px] text-muted-foreground/60">
            {task.id.slice(0, 8)}
          </span>
        </div>
        <div className="flex flex-wrap items-center gap-2 text-xs">
          <Badge
            variant="outline"
            className={`font-mono text-[9px] uppercase tracking-wide ${statusBadgeClass(task.status)}`}
          >
            {task.status}
          </Badge>
          {task.agentDefName ? (
            <span className="text-muted-foreground/80">
              {task.agentDefName}
            </span>
          ) : null}
          {task.attemptCount > 1 ? (
            <span className="text-muted-foreground/60">
              attempts {task.attemptCount}/{task.maxAttempts}
            </span>
          ) : null}
          <span className="ml-auto inline-flex items-center gap-1 text-muted-foreground/60">
            <Clock className="size-3" />
            {timeLabel}
          </span>
        </div>
      </div>

      {/* Outcome — the first thing in the pane: result for done tasks,
          live output while running, waiting/blocked context otherwise. */}
      <OutcomeCard outcome={outcome} />

      {/* Dependencies */}
      {parents.length > 0 || children.length > 0 ? (
        <div className="space-y-2">
          <div className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground/70">
            Dependencies
          </div>
          {parents.length > 0 ? (
            <div className="flex flex-wrap items-center gap-1.5">
              <span className="mr-1 text-[10px] uppercase text-muted-foreground/50">
                Depends on
              </span>
              {parents.map((p) => (
                <TaskChip key={p.id} task={p} onSelect={onSelect} />
              ))}
            </div>
          ) : null}
          {children.length > 0 ? (
            <div className="flex flex-wrap items-center gap-1.5">
              <span className="mr-1 text-[10px] uppercase text-muted-foreground/50">
                Blocks
              </span>
              {children.map((c) => (
                <TaskChip key={c.id} task={c} onSelect={onSelect} />
              ))}
            </div>
          ) : null}
        </div>
      ) : null}

      {/* Brief — goals are authored as markdown by the coordinator. */}
      <div>
        <div className="mb-1 font-mono text-[10px] uppercase tracking-widest text-muted-foreground/70">
          Brief
        </div>
        <MessageResponse className="text-sm leading-relaxed text-foreground/90">
          {task.goal}
        </MessageResponse>
      </div>

      {task.resultMetadata ? (
        <details>
          <summary className="cursor-pointer font-mono text-[10px] uppercase tracking-widest text-muted-foreground/70">
            Result metadata
          </summary>
          <pre className="mt-1 max-h-64 overflow-auto whitespace-pre-wrap rounded bg-muted/30 px-2 py-1.5 text-[11px] text-foreground/90">
            {JSON.stringify(task.resultMetadata, null, 2)}
          </pre>
        </details>
      ) : null}

      {/* History */}
      {eventsSupported && taskEvents.length > 0 ? (
        <div>
          <div className="mb-2 font-mono text-[10px] uppercase tracking-widest text-muted-foreground/70">
            History ({taskEvents.length})
          </div>
          <ol className="space-y-1.5">
            {taskEvents.map((e) => (
              <li key={e.id} className="flex items-start gap-3 text-xs">
                <span className="w-16 shrink-0 font-mono text-[10px] text-muted-foreground/60">
                  {formatMissionTimestamp(e.createdAt)}
                </span>
                <span className="shrink-0 font-mono text-[9px] uppercase tracking-wide text-muted-foreground/70">
                  {missionEventCategory(e.type)}
                </span>
                <span className="min-w-0 flex-1 text-foreground/80">
                  {missionEventSummary(e)}
                </span>
              </li>
            ))}
          </ol>
        </div>
      ) : null}

      {/* Worker session link */}
      {task.currentSessionId && onOpenTranscript ? (
        <Button
          data-testid="view-session"
          variant="outline"
          size="sm"
          onClick={() => onOpenTranscript(task.currentSessionId!)}
        >
          <ArrowRight className="size-4" />
          View {task.agentDefName ?? "worker"} session
        </Button>
      ) : null}
    </div>
  );
}


// ---------------------------------------------------------------------------
// Outcome card — the status-dependent block pinned to the top of the
// detail pane: result (done), live output (running), waiting context
// (blocked / queued), failure / cancellation notices.
// ---------------------------------------------------------------------------

type TaskOutcome = {
  label: string;
  /** Card tint + ring overrides applied on top of the base Card. */
  tone: string;
  /** Label/icon color. */
  labelTone: string;
  icon: ReactNode;
  timestamp: string | null;
  /** Rendered through the markdown renderer when set. */
  markdown?: string;
  /** Plain-text fallback body. */
  text?: string;
};

function deriveTaskOutcome({
  task,
  taskEvents,
  liveOutput,
  parents,
}: {
  task: AgentChatMissionTask;
  taskEvents: AgentChatMissionEvent[];
  liveOutput: AgentChatMissionEvent | null;
  parents: AgentChatMissionTask[];
}): TaskOutcome {
  switch (task.status) {
    case "done":
      return {
        label: "Result",
        tone: "bg-emerald-500/5 ring-emerald-500/25",
        labelTone: "text-emerald-600",
        icon: <CheckCircle2 className="size-3" />,
        timestamp: task.completedAt,
        markdown: task.result ?? undefined,
        text: task.result ? undefined : "Completed.",
      };
    case "failed":
      return {
        label: "Failed",
        tone: "bg-destructive/5 ring-destructive/25",
        labelTone: "text-destructive",
        icon: <XCircle className="size-3" />,
        timestamp: task.completedAt,
        markdown: task.result ?? undefined,
        text: task.result
          ? undefined
          : `Failed after ${task.attemptCount}/${task.maxAttempts} attempts.`,
      };
    case "cancelled":
      return {
        label: "Cancelled",
        tone: "bg-muted/40 ring-border",
        labelTone: "text-muted-foreground",
        icon: <Ban className="size-3" />,
        timestamp: task.completedAt,
        text: "Task cancelled.",
      };
    case "running":
      return {
        label: "Live output",
        tone: "bg-amber-500/5 ring-amber-500/25",
        labelTone: "text-amber-600",
        icon: <CircleDot className="size-3 animate-pulse" />,
        timestamp: liveOutput?.createdAt ?? null,
        text: liveOutput
          ? missionEventSummary(liveOutput)
          : "Working — no output yet.",
      };
    case "blocked": {
      // worker_block writes a one-sentence reason into a task.blocked
      // event on the coordinator session; surface the latest one.
      const blockedEvent = [...taskEvents]
        .reverse()
        .find((e) => e.type === "task.blocked");
      return {
        label: "Waiting",
        tone: "bg-amber-500/5 ring-amber-500/20",
        labelTone: "text-amber-700 dark:text-amber-500",
        icon: <Clock className="size-3" />,
        timestamp: blockedEvent?.createdAt ?? null,
        text: blockedEvent
          ? missionEventSummary(blockedEvent)
          : "Blocked — awaiting input.",
      };
    }
    default: {
      // todo / ready (and any future pre-run status): explain what the
      // dispatcher is waiting for.
      const pending = parents.filter((p) => p.status !== "done");
      const text =
        pending.length > 0
          ? `Waiting on ${pending
              .map((p) => missionTaskTitle(p.goal, 40))
              .join(" · ")} to complete.`
          : "Queued — waiting for a worker slot.";
      return {
        label: "Waiting",
        tone: "bg-muted/30 ring-border",
        labelTone: "text-muted-foreground",
        icon: <Clock className="size-3" />,
        timestamp: null,
        text,
      };
    }
  }
}

function OutcomeCard({ outcome }: { outcome: TaskOutcome }) {
  return (
    <Card
      data-testid="task-outcome"
      size="sm"
      className={`gap-0 py-0 shadow-none ${outcome.tone}`}
    >
      <CardContent className="space-y-1.5 px-4 py-3">
        <div className="flex items-center justify-between gap-2">
          <span
            className={`inline-flex items-center gap-1.5 font-mono text-[9px] uppercase tracking-widest ${outcome.labelTone}`}
          >
            {outcome.icon}
            {outcome.label}
          </span>
          {outcome.timestamp ? (
            <span className="font-mono text-[10px] text-muted-foreground/60">
              {formatMissionTimestamp(outcome.timestamp)}
            </span>
          ) : null}
        </div>
        {outcome.markdown ? (
          <MessageResponse className="text-sm leading-relaxed text-foreground/90">
            {outcome.markdown}
          </MessageResponse>
        ) : (
          <p className="whitespace-pre-wrap text-sm leading-relaxed text-foreground/90">
            {outcome.text}
          </p>
        )}
      </CardContent>
    </Card>
  );
}
