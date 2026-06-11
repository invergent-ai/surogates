// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Workers tab — one card per mission child. Task-kind workers get
// Completed / In-flight counts computed from the mission tasks
// grouped by agentDefName; spawn_worker / delegate_task children
// (no Task rows) show session status + latest event only.
import { Badge } from "../ui/badge";
import { Tooltip, TooltipContent, TooltipTrigger } from "../ui/tooltip";

import type {
  AgentChatMissionTask,
  AgentChatMissionWorker,
} from "../../types";

import {
  deriveMissionWorkerActivityLabel,
  formatMissionTimestamp,
  missionWorkerTaskCounts,
} from "./mission-derive";


export interface MissionWorkersTabProps {
  workers: AgentChatMissionWorker[];
  tasks: AgentChatMissionTask[];
  onOpenTranscript?: (workerSessionId: string) => void;
}


// Order of mention matches the durability gradient: ``task`` is
// durable + retried, ``worker`` durable + one-shot, ``delegation``
// ephemeral.
const MISSION_WORKER_KIND_BADGE_CLASS: Record<
  AgentChatMissionWorker["kind"],
  string
> = {
  task: "border-primary/40 bg-primary/10 text-primary uppercase tracking-wide text-[10px]",
  worker:
    "border-foreground/30 bg-foreground/5 text-foreground/80 uppercase tracking-wide text-[10px]",
  delegation:
    "border-foreground/20 bg-foreground/[0.03] text-foreground/60 uppercase tracking-wide text-[10px]",
};

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
      <div className="py-8 text-center text-sm text-muted-foreground/60">
        No workers attached to this mission right now.
      </div>
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
            <TooltipTrigger asChild>
              <Badge
                variant="outline"
                className={`mt-1 ${MISSION_WORKER_KIND_BADGE_CLASS[worker.kind]}`}
              >
                {worker.kind}
              </Badge>
            </TooltipTrigger>
            <TooltipContent className="max-w-xs">
              {MISSION_WORKER_KIND_TOOLTIP[worker.kind]}
            </TooltipContent>
          </Tooltip>
        </div>
        <span className="shrink-0 font-mono text-[10px] uppercase tracking-wider text-muted-foreground/70">
          {statusLabel}
        </span>
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
        <a
          href={worker.transcriptUrl}
          target="_blank"
          rel="noreferrer"
          onClick={onTranscriptClick}
          className="inline-block text-xs text-primary hover:underline"
        >
          View session
        </a>
      </div>
    </div>
  );
}
