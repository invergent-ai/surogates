// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// MissionDashboard — host-agnostic component that renders the full
// mission state.  Layout mirrors SessionDetail: a compact header (one
// line of identity + flags + actions, one line of dense metadata) and
// a tab-style body so a long task goal or long rubric never blows out
// the page.  Hosts wrap this in their own page shell (sidebar, layout,
// route title).  The dashboard owns its data layer: polls the adapter
// every 5s while active/paused, stops on terminal status.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Ban,
  Loader2,
  Pause,
  Play,
  RefreshCw,
  AlertCircle,
} from "lucide-react";

import { Button } from "../ui/button";
import { Card, CardContent } from "../ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "../ui/dialog";
import { Progress } from "../ui/progress";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "../ui/tabs";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "../ui/tooltip";

import type {
  AgentChatAdapter,
  AgentChatMissionSummary,
  AgentChatMissionTask,
  AgentChatMissionWorker,
} from "../../types";

import {
  ACTIVE_MISSION_STATUSES,
  isTerminalMissionStatus,
} from "./mission-derive";
import { MissionActivityTab } from "./mission-activity-tab";
import { MissionMetadataTab } from "./mission-metadata-tab";
import { MissionTasksTab } from "./mission-tasks-tab";
import { MissionWorkersTab } from "./mission-workers-tab";
import { useMissionEvents } from "./use-mission-events";


const DEFAULT_POLL_INTERVAL_MS = 5_000;


/** Small count chip shown next to tab labels — same shape as
 * ``frontend/src/components/sessions/session-detail.tsx``'s CountBadge.
 * Hidden when ``n === 0`` so empty tabs don't look noisy.
 */
function CountBadge({ n }: { n: number }) {
  if (n === 0) return null;
  return (
    <span className="ml-1 rounded bg-muted px-1 py-px text-[9px] text-muted-foreground/70">
      {n}
    </span>
  );
}


export interface MissionDashboardProps {
  adapter: AgentChatAdapter;
  missionId: string;
  /** Override the 5s default while testing or in lower-traffic embeddings. */
  pollIntervalMs?: number;
  /** Optional click handler for the "Back" button shown on terminal missions. */
  onNavigateBack?: () => void;
  /** Optional click handler for a worker row's transcript link.
   * When unset, the dashboard renders the raw URL the server provided as
   * an `<a target="_blank">` link — appropriate for hosts that haven't
   * registered an in-app session view. */
  onOpenTranscript?: (workerSessionId: string) => void;
}


type DashboardState = {
  mission: AgentChatMissionSummary | null;
  tasks: AgentChatMissionTask[];
  workers: AgentChatMissionWorker[];
  loading: boolean;
  error: string | null;
};


const INITIAL_STATE: DashboardState = {
  mission: null,
  tasks: [],
  workers: [],
  loading: true,
  error: null,
};


/** Bind the adapter's optional mission methods so the dashboard can fail
 * loud at mount time rather than late at click time with a cryptic
 * "undefined is not a function". */
function requireMissionApi(adapter: AgentChatAdapter) {
  const required = {
    getMission: adapter.getMission,
    getMissionTasks: adapter.getMissionTasks,
    getMissionWorkers: adapter.getMissionWorkers,
    pauseMission: adapter.pauseMission,
    resumeMission: adapter.resumeMission,
    cancelMission: adapter.cancelMission,
  };
  for (const [name, fn] of Object.entries(required)) {
    if (typeof fn !== "function") {
      throw new Error(
        `MissionDashboard requires adapter.${name} to be implemented; this adapter is missing the mission surface.`,
      );
    }
  }
  return required as {
    getMission: NonNullable<AgentChatAdapter["getMission"]>;
    getMissionTasks: NonNullable<AgentChatAdapter["getMissionTasks"]>;
    getMissionWorkers: NonNullable<AgentChatAdapter["getMissionWorkers"]>;
    pauseMission: NonNullable<AgentChatAdapter["pauseMission"]>;
    resumeMission: NonNullable<AgentChatAdapter["resumeMission"]>;
    cancelMission: NonNullable<AgentChatAdapter["cancelMission"]>;
  };
}


export function MissionDashboard({
  adapter,
  missionId,
  pollIntervalMs = DEFAULT_POLL_INTERVAL_MS,
  onNavigateBack,
  onOpenTranscript,
}: MissionDashboardProps) {
  const api = useMemo(() => requireMissionApi(adapter), [adapter]);

  const [state, setState] = useState<DashboardState>(INITIAL_STATE);
  const [cancelOpen, setCancelOpen] = useState(false);
  const [cancelCascade, setCancelCascade] = useState(false);
  const [busy, setBusy] = useState(false);
  const isMounted = useRef(true);

  useEffect(() => {
    isMounted.current = true;
    return () => {
      isMounted.current = false;
    };
  }, []);

  const refresh = useCallback(async () => {
    try {
      const [mission, tasksResp, workersResp] = await Promise.all([
        api.getMission({ missionId }),
        api.getMissionTasks({ missionId }),
        api.getMissionWorkers({ missionId }),
      ]);
      if (!isMounted.current) return;
      setState({
        mission,
        tasks: tasksResp.tasks,
        workers: workersResp.workers,
        loading: false,
        error: null,
      });
    } catch (err) {
      if (!isMounted.current) return;
      setState((prev) => ({
        ...prev,
        loading: false,
        error: err instanceof Error ? err.message : String(err),
      }));
    }
  }, [api, missionId]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // 5s polling — stops on terminal status.
  useEffect(() => {
    if (!state.mission) return;
    if (isTerminalMissionStatus(state.mission.status)) return;
    const id = window.setInterval(() => {
      void refresh();
    }, pollIntervalMs);
    return () => {
      window.clearInterval(id);
    };
  }, [refresh, state.mission, pollIntervalMs]);

  const feed = useMissionEvents({
    adapter,
    missionId,
    missionStatus: state.mission?.status ?? null,
    isTerminal: state.mission
      ? isTerminalMissionStatus(state.mission.status)
      : false,
    pollIntervalMs,
  });

  const runningWorkerCount = useMemo(
    // "running" means the child is still consuming compute right now.
    // For task-backed children that's ``taskStatus === "running"``; for
    // spawn_worker / delegate_task direct children the Task row doesn't
    // exist, so fall back to the session-level signal.
    () => state.workers.filter((w) => (
      w.kind === "task"
        ? w.taskStatus === "running"
        : w.sessionStatus === "active"
    )).length,
    [state.workers],
  );

  if (state.loading && !state.mission) {
    return (
      <div className="flex h-full items-center justify-center p-6">
        <Loader2 className="size-5 animate-spin text-foreground/70" />
      </div>
    );
  }

  if (state.error && !state.mission) {
    return (
      <div className="p-6 space-y-3">
        <div className="flex items-center gap-2 text-destructive">
          <AlertCircle className="size-4" />
          Failed to load mission: {state.error}
        </div>
        <Button
          variant="outline"
          size="sm"
          onClick={() => {
            setState(INITIAL_STATE);
            void refresh();
          }}
        >
          <RefreshCw className="size-4" /> Retry
        </Button>
      </div>
    );
  }

  const mission = state.mission!;

  const doPause = async () => {
    setBusy(true);
    try {
      await api.pauseMission({ missionId });
      await refresh();
    } finally {
      setBusy(false);
    }
  };
  const doResume = async () => {
    setBusy(true);
    try {
      await api.resumeMission({ missionId });
      await refresh();
    } finally {
      setBusy(false);
    }
  };
  const doCancel = async () => {
    setBusy(true);
    try {
      await api.cancelMission({
        missionId,
        cascadeToWorkers: cancelCascade,
      });
      await refresh();
      setCancelOpen(false);
    } finally {
      setBusy(false);
    }
  };

  const statusTone =
    mission.status === "satisfied"
      ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-700 dark:text-emerald-400"
      : mission.status === "failed" || mission.status === "cancelled"
        ? "border-destructive/40 bg-destructive/10 text-destructive"
        : mission.status === "paused"
          ? "border-amber-500/40 bg-amber-500/10 text-amber-700 dark:text-amber-400"
          : "border-primary/40 bg-primary/10 text-primary";

  const isActive = ACTIVE_MISSION_STATUSES.has(mission.status);
  const iterationPct =
    mission.maxIterations > 0
      ? Math.min(100, (mission.iteration / mission.maxIterations) * 100)
      : 0;

  return (
    <TooltipProvider delayDuration={300}>
      <div className="flex h-full flex-1 flex-col overflow-hidden bg-muted/30">
        {/* ----- Hero card -------------------------------------------- */}
        <div className="shrink-0 px-5 pt-5 sm:px-6 sm:pt-6">
          <Card className="border-border/60 bg-card shadow-sm">
            <CardContent className="space-y-4 p-5 sm:p-6">
              {/* Top row: status pill + iteration metric + actions */}
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="flex flex-wrap items-center gap-3">
                  <span
                    className={`rounded-md border px-2 py-0.5 font-mono text-[10px] font-semibold uppercase tracking-wider ${statusTone}`}
                  >
                    {mission.status}
                  </span>
                  <div className="flex items-baseline gap-1.5">
                    <span className="font-display text-2xl font-bold tracking-tight tabular-nums text-foreground">
                      {mission.iteration}
                    </span>
                    <span className="text-sm text-muted-foreground/70">
                      / {mission.maxIterations}
                    </span>
                    <span className="ml-1 text-[10px] uppercase tracking-wider text-muted-foreground/60">
                      iterations
                    </span>
                  </div>
                  {mission.lastEvaluationResult ? (
                    <div className="flex items-center gap-1.5 text-[11px] text-muted-foreground/70">
                      <span className="opacity-70">last verdict</span>
                      <span className="rounded border border-primary/30 bg-primary/10 px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wider text-primary">
                        {mission.lastEvaluationResult}
                      </span>
                    </div>
                  ) : null}
                </div>
                <div className="flex shrink-0 flex-wrap items-center gap-2">
                  {isActive ? (
                    <>
                      {mission.status === "active" ? (
                        <Button
                          size="sm"
                          variant="outline"
                          onClick={doPause}
                          disabled={busy}
                        >
                          <Pause className="size-4" /> Pause
                        </Button>
                      ) : (
                        <Button
                          size="sm"
                          variant="outline"
                          onClick={doResume}
                          disabled={busy}
                        >
                          <Play className="size-4" /> Resume
                        </Button>
                      )}
                      <Button
                        size="sm"
                        variant="destructive"
                        onClick={() => {
                          setCancelCascade(false);
                          setCancelOpen(true);
                        }}
                        disabled={busy}
                      >
                        <Ban className="size-4" /> Cancel
                      </Button>
                    </>
                  ) : onNavigateBack ? (
                    <Button size="sm" variant="outline" onClick={onNavigateBack}>
                      Back
                    </Button>
                  ) : null}
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => void refresh()}
                    disabled={busy}
                    aria-label="Refresh"
                    title="Refresh"
                  >
                    <RefreshCw className="size-4" />
                  </Button>
                </div>
              </div>

              {/* Iteration progress bar — fades when terminal. */}
              <Progress
                value={iterationPct}
                className={`h-1 ${isActive ? "" : "opacity-40"}`}
              />

              {/* Description — full text, generously sized.  Long
                  descriptions clamp to ~4 lines with the rest revealed
                  on hover via tooltip. */}
              <div>
                <Tooltip>
                  <TooltipTrigger asChild>
                    <h1 className="line-clamp-4 cursor-default font-display text-lg font-semibold leading-snug text-foreground wrap-break-word">
                      {mission.description}
                    </h1>
                  </TooltipTrigger>
                  {mission.description.length > 240 ? (
                    <TooltipContent className="max-w-lg" side="bottom">
                      <p className="whitespace-pre-wrap text-xs">
                        {mission.description}
                      </p>
                    </TooltipContent>
                  ) : null}
                </Tooltip>
              </div>

              {/* Rubric — clearly labelled, also clamped */}
              <div className="rounded-md border border-border/60 bg-muted/40 px-3 py-2">
                <div className="mb-0.5 font-mono text-[9px] uppercase tracking-widest text-muted-foreground/70">
                  Rubric
                </div>
                <Tooltip>
                  <TooltipTrigger asChild>
                    <p className="line-clamp-3 cursor-default text-sm leading-relaxed text-foreground/90 wrap-break-word">
                      {mission.rubric}
                    </p>
                  </TooltipTrigger>
                  {mission.rubric.length > 200 ? (
                    <TooltipContent className="max-w-lg" side="bottom">
                      <p className="whitespace-pre-wrap text-xs">
                        {mission.rubric}
                      </p>
                    </TooltipContent>
                  ) : null}
                </Tooltip>
              </div>

            </CardContent>
          </Card>
        </div>

        {/* ----- Tabs ------------------------------------------------ */}
        <Tabs
          defaultValue="tasks"
          className="mt-5 flex flex-1 flex-col overflow-hidden sm:mt-6"
        >
          <div className="shrink-0 border-b border-border bg-background px-5 sm:px-6">
            <TabsList variant="line">
              <TabsTrigger value="tasks">
                Tasks
                <CountBadge n={state.tasks.length} />
              </TabsTrigger>
              {feed.supported ? (
                <TabsTrigger value="activity">
                  Activity
                  <CountBadge n={feed.events.length} />
                </TabsTrigger>
              ) : null}
              <TabsTrigger value="workers">
                Workers
                <CountBadge n={state.workers.length} />
              </TabsTrigger>
              <TabsTrigger value="metadata">Metadata</TabsTrigger>
            </TabsList>
          </div>

          <div className="flex-1 overflow-y-auto bg-background px-5 py-4 sm:px-6">
            <TabsContent value="tasks" className="mt-0 h-full">
              <MissionTasksTab
                tasks={state.tasks}
                feed={feed}
                onOpenTranscript={onOpenTranscript}
              />
            </TabsContent>
            {feed.supported ? (
              <TabsContent value="activity" className="mt-0">
                <MissionActivityTab feed={feed} />
              </TabsContent>
            ) : null}
            <TabsContent value="workers" className="mt-0">
              <MissionWorkersTab
                workers={state.workers}
                tasks={state.tasks}
                onOpenTranscript={onOpenTranscript}
              />
            </TabsContent>
            <TabsContent value="metadata" className="mt-0">
              <MissionMetadataTab mission={mission} />
            </TabsContent>
          </div>
        </Tabs>

        {/* ----- Cancel confirm dialog --------------------------------- */}
        <Dialog
          open={cancelOpen}
          onOpenChange={(open) => {
            if (!open && !busy) setCancelOpen(false);
          }}
        >
          <DialogContent className="sm:max-w-md">
            <DialogHeader>
              <DialogTitle>Cancel mission?</DialogTitle>
              <DialogDescription>
                This terminates the mission and clears its evaluator loop.
              </DialogDescription>
            </DialogHeader>
            <label className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={cancelCascade}
                onChange={(e) => setCancelCascade(e.target.checked)}
              />
              <span>
                Also cancel <strong>{runningWorkerCount}</strong> running worker
                {runningWorkerCount === 1 ? "" : "s"} (sends an interrupt to each
                worker session).
              </span>
            </label>
            <DialogFooter>
              <Button
                variant="outline"
                size="sm"
                onClick={() => setCancelOpen(false)}
                disabled={busy}
              >
                Keep mission
              </Button>
              <Button
                variant="destructive"
                size="sm"
                onClick={doCancel}
                disabled={busy}
              >
                {busy ? (
                  <Loader2 className="size-4 animate-spin" />
                ) : (
                  <Ban className="size-4" />
                )}
                Cancel mission
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      </div>
    </TooltipProvider>
  );
}

