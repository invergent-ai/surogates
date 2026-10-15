// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Workers tab — one card per mission child. Task-kind workers get
// Completed / In-flight counts computed from the mission tasks
// grouped by agentDefName; spawn_worker / delegate_task children
// (no Task rows) show session status + latest event only.
import { Chip, TonePill } from "@invergent/ui-sdk/chip";
import { EmptyState } from "@invergent/ui-sdk/empty-state";
import { TextLink } from "@invergent/ui-sdk/text-link";
import { Tooltip, TooltipContent, TooltipTrigger } from "@invergent/ui-sdk/tooltip";

import type {
  AgentChatMissionTask,
  AgentChatMissionWorker,
} from "../../types";

import {
  deriveMissionWorkerActivityLabel,
  formatMissionTimestamp,
  missionWorkerTaskCounts,
} from "./mission-derive";
import { missionTaskStatusTone } from "./mission-tasks-tab";


export interface MissionWorkersTabProps {
  workers: AgentChatMissionWorker[];
  tasks: AgentChatMissionTask[];
  onOpenTranscript?: (workerSessionId: string) => void;
}


// Order of mention matches the durability gradient: ``task`` is
// durable + retried, ``worker`` durable + one-shot, ``delegation``
// ephemeral.
const MISSION_WORKER_KIND_TOOLTIP: Record<
  AgentChatMissionWorker["kind"],
  string
> = {
  task: "Durable Task row created by spawn_task. Retried by the dispatcher; survives across coordinator wakes.",
  worker:
    "Async one-shot session spawned by spawn_worker. Durable session, no retry/DAG.",
  delegation:
    "Sync fork-join child spawned by delegate_task. Coordinator's wake blocked until it finished.",
};


export function MissionWorkersTab({
  workers,
  tasks,
  onOpenTranscript,
}: MissionWorkersTabProps) {
  if (workers.length === 0) {
    return (
      <EmptyState
        className="py-8"
        title="No workers attached to this mission right now"
      />
    );
  }
  return (
    <div className="grid gap-3 sm:grid-cols-2">
      {workers.map((w) => (
        <WorkerCard
          key={w.workerSessionId}
          worker={w}
          tasks={tasks}
          onOpenTranscript={onOpenTranscript}
        />
      ))}
    </div>
  );
}


function WorkerCard({
  worker,
  tasks,
  onOpenTranscript,
}: {
  worker: AgentChatMissionWorker;
  tasks: AgentChatMissionTask[];
  onOpenTranscript?: (workerSessionId: string) => void;
}) {
  const counts = missionWorkerTaskCounts(worker, tasks);
  const statusLabel =
    worker.kind === "task" ? (worker.taskStatus ?? "—") : worker.sessionStatus;
  const onTranscriptClick = (e: React.MouseEvent<HTMLAnchorElement>) => {
    if (onOpenTranscript) {
      e.preventDefault();
      onOpenTranscript(worker.workerSessionId);
    }
  };

  return (
    <div className="flex flex-col gap-3 rounded-md border border-border/60 bg-card p-4 shadow-sm">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="truncate text-sm font-semibold text-foreground">
            {worker.agentDefName ?? "worker"}
          </div>
          <Tooltip>
            {/* Chip, not TonePill: the trigger hands its props and ref to
                the child, and only Chip passes them through. */}
            <TooltipTrigger asChild>
              <Chip size="sm" className="mt-1 capitalize">
                {worker.kind}
              </Chip>
            </TooltipTrigger>
            <TooltipContent className="max-w-xs">
              {MISSION_WORKER_KIND_TOOLTIP[worker.kind]}
            </TooltipContent>
          </Tooltip>
        </div>
        <TonePill tone={missionTaskStatusTone(statusLabel)} className="capitalize">
          {statusLabel}
        </TonePill>
      </div>

      {counts ? (
        <div className="flex items-baseline gap-6">
          <div>
            <div className="text-xl font-bold tabular-nums text-foreground">
              {counts.completed}
            </div>
            <div className="font-mono text-[9px] uppercase tracking-widest text-muted-foreground/60">
              Completed
            </div>
          </div>
          <div>
            <div
              className={`text-xl font-bold tabular-nums ${counts.inFlight > 0 ? "text-amber-600" : "text-foreground"}`}
            >
              {counts.inFlight}
            </div>
            <div className="font-mono text-[9px] uppercase tracking-widest text-muted-foreground/60">
              In flight
            </div>
          </div>
        </div>
      ) : null}

      <div className="mt-auto space-y-1 border-t border-border/40 pt-2">
        <div className="flex items-center justify-between gap-2">
          <span className="truncate font-mono text-[10px] text-muted-foreground/70">
            {worker.latestEventKind ?? "no events yet"}
          </span>
          {worker.latestEventAt ? (
            <span className="shrink-0 font-mono text-[10px] text-muted-foreground/60">
              {formatMissionTimestamp(worker.latestEventAt)}
            </span>
          ) : null}
        </div>
        <div className="truncate text-xs text-foreground/80">
          {deriveMissionWorkerActivityLabel(worker)}
        </div>
        <TextLink
          size="sm"
          href={worker.transcriptUrl}
          target="_blank"
          rel="noreferrer"
          onClick={onTranscriptClick}
        >
          View session
        </TextLink>
      </div>
    </div>
  );
}
