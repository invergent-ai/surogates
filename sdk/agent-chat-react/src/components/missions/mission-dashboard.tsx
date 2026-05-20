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
import {
  Fragment,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  Ban,
  Loader2,
  Pause,
  Play,
  RefreshCw,
  AlertCircle,
  ChevronRight,
} from "lucide-react";

import { Badge } from "../ui/badge";
import { Button } from "../ui/button";
import { Card, CardContent } from "../ui/card";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "../ui/collapsible";
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
  deriveMissionWorkerActivityLabel,
  groupMissionTasksByBucket,
  isTerminalMissionStatus,
} from "./mission-derive";


const DEFAULT_POLL_INTERVAL_MS = 5_000;

// Worker-row "kind" badge — visually distinguishes the three
// delegation primitives the coordinator can use.  Order of mention
// matches the durability gradient: ``task`` is durable + retried,
// ``worker`` is durable + one-shot, ``delegation`` is ephemeral.
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


/** First non-empty line of *text*, normalised + truncated to *max* chars.
 * Used for collapsed-row previews of task goals + result blobs. */
function firstLine(text: string, max = 140): string {
  const trimmed = text.replace(/\s+/g, " ").trim();
  if (trimmed.length <= max) return trimmed;
  return `${trimmed.slice(0, max).trimEnd()}…`;
}


/** Mission activity event derived from the existing data set — we
 * synthesise these from ``mission.createdAt``, ``task.createdAt``,
 * ``task.completedAt``, ``worker.latestEventAt``, and
 * ``mission.lastEvaluationAt``.  No new adapter calls required.
 *
 * Sorted newest-first by the dashboard so the timeline reads top-down. */
type ActivityEntry = {
  at: string;
  kind:
    | "mission.defined"
    | "mission.paused"
    | "mission.cancelled"
    | "task.spawned"
    | "task.completed"
    | "task.failed"
    | "task.cancelled"
    | "worker.latest"
    | "evaluator.verdict";
  label: string;
  detail?: string;
};


const ACTIVITY_KIND_TONE: Record<ActivityEntry["kind"], string> = {
  "mission.defined": "bg-primary/10 text-primary border-primary/30",
  "mission.paused": "bg-amber-500/10 text-amber-600 border-amber-500/30",
  "mission.cancelled": "bg-destructive/10 text-destructive border-destructive/30",
  "task.spawned": "bg-foreground/5 text-foreground/80 border-foreground/20",
  "task.completed": "bg-emerald-500/10 text-emerald-600 border-emerald-500/30",
  "task.failed": "bg-destructive/10 text-destructive border-destructive/30",
  "task.cancelled": "bg-muted text-muted-foreground border-border",
  "worker.latest": "bg-foreground/5 text-foreground/70 border-foreground/15",
  "evaluator.verdict": "bg-primary/10 text-primary border-primary/30",
};


function buildMissionActivity(
  mission: AgentChatMissionSummary,
  tasks: AgentChatMissionTask[],
  workers: AgentChatMissionWorker[],
): ActivityEntry[] {
  const entries: ActivityEntry[] = [];

  entries.push({
    at: mission.createdAt,
    kind: "mission.defined",
    label: "Mission defined",
    detail: mission.description,
  });

  if (mission.status === "paused" && mission.pausedReason) {
    entries.push({
      at: mission.updatedAt,
      kind: "mission.paused",
      label: "Paused",
      detail: mission.pausedReason,
    });
  }
  if (mission.status === "cancelled" && mission.cancelledReason) {
    entries.push({
      at: mission.updatedAt,
      kind: "mission.cancelled",
      label: "Cancelled",
      detail: mission.cancelledReason,
    });
  }

  for (const task of tasks) {
    if (task.createdAt) {
      entries.push({
        at: task.createdAt,
        kind: "task.spawned",
        label: `Spawn · ${task.agentDefName ?? "task"}`,
        detail: firstLine(task.goal, 200),
      });
    }
    if (task.completedAt) {
      const kind: ActivityEntry["kind"] =
        task.status === "done"
          ? "task.completed"
          : task.status === "failed"
            ? "task.failed"
            : task.status === "cancelled"
              ? "task.cancelled"
              : "task.completed";
      entries.push({
        at: task.completedAt,
        kind,
        label: `${task.status} · ${task.agentDefName ?? "task"}`,
        detail: task.result ? firstLine(task.result, 200) : undefined,
      });
    }
  }

  for (const worker of workers) {
    if (worker.latestEventAt && worker.latestEventKind) {
      entries.push({
        at: worker.latestEventAt,
        kind: "worker.latest",
        label: `${worker.kind} · ${worker.latestEventKind}`,
        detail: deriveMissionWorkerActivityLabel(worker),
      });
    }
  }

  if (mission.lastEvaluationAt && mission.lastEvaluationResult) {
    entries.push({
      at: mission.lastEvaluationAt,
      kind: "evaluator.verdict",
      label: `Verdict · ${mission.lastEvaluationResult}`,
      detail: mission.lastEvaluationFeedback ?? undefined,
    });
  }

  entries.sort((a, b) => b.at.localeCompare(a.at));
  return entries;
}


function formatTimestamp(iso: string): string {
  try {
    return new Date(iso).toLocaleTimeString();
  } catch {
    return iso;
  }
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

  const taskBuckets = useMemo(
    () => groupMissionTasksByBucket(state.tasks),
    [state.tasks],
  );

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

  const activity = buildMissionActivity(mission, state.tasks, state.workers);
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
          defaultValue="activity"
          className="mt-5 flex flex-1 flex-col overflow-hidden sm:mt-6"
        >
          <div className="shrink-0 border-b border-border bg-background px-5 sm:px-6">
            <TabsList variant="line">
              <TabsTrigger value="activity">
                Activity
                <CountBadge n={activity.length} />
              </TabsTrigger>
              <TabsTrigger value="tasks">
                Tasks
                <CountBadge n={state.tasks.length} />
              </TabsTrigger>
              <TabsTrigger value="workers">
                Workers
                <CountBadge n={state.workers.length} />
              </TabsTrigger>
              <TabsTrigger value="metadata">Metadata</TabsTrigger>
            </TabsList>
          </div>

          <div className="flex-1 overflow-y-auto bg-background px-5 py-4 sm:px-6">
            <TabsContent value="activity" className="mt-0">
              <ActivityTimeline entries={activity} />
            </TabsContent>
            <TabsContent value="tasks" className="mt-0">
              <TasksList
                taskBuckets={taskBuckets}
                totalTasks={state.tasks.length}
              />
            </TabsContent>
            <TabsContent value="workers" className="mt-0">
              <WorkersList
                workers={state.workers}
                onOpenTranscript={onOpenTranscript}
              />
            </TabsContent>
            <TabsContent value="metadata" className="mt-0">
              <MetadataPane mission={mission} />
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


// ===========================================================================
// Tab content components — split out so the outer dashboard stays readable.
// ===========================================================================


function ActivityTimeline({ entries }: { entries: ActivityEntry[] }) {
  if (entries.length === 0) {
    return (
      <div className="py-8 text-center text-sm text-muted-foreground/60">
        No mission activity yet.
      </div>
    );
  }
  return (
    <ol className="space-y-1">
      {entries.map((e, i) => (
        <li
          key={`${e.kind}-${e.at}-${i}`}
          className="flex items-start gap-3 rounded px-2 py-1.5 text-sm hover:bg-muted/30"
        >
          <span className="w-20 shrink-0 font-mono text-[10px] text-muted-foreground/60">
            {formatTimestamp(e.at)}
          </span>
          <span
            className={`shrink-0 rounded border px-1.5 py-0.5 font-mono text-[9px] uppercase tracking-wide ${ACTIVITY_KIND_TONE[e.kind]}`}
          >
            {e.label}
          </span>
          {e.detail ? (
            <span className="min-w-0 flex-1 truncate text-foreground/80">
              {e.detail}
            </span>
          ) : null}
        </li>
      ))}
    </ol>
  );
}


function TaskRow({ task }: { task: AgentChatMissionTask }) {
  const [open, setOpen] = useState(false);
  const goalPreview = firstLine(task.goal, 160);
  const hasMore =
    task.goal.length > 160 ||
    Boolean(task.result) ||
    Boolean(task.resultMetadata);

  const statusTone =
    task.status === "done"
      ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-600"
      : task.status === "failed"
        ? "border-destructive/30 bg-destructive/10 text-destructive"
        : task.status === "cancelled"
          ? "border-border bg-muted text-muted-foreground"
          : task.status === "running"
            ? "border-primary/30 bg-primary/10 text-primary"
            : "border-foreground/20 bg-foreground/5 text-foreground/70";

  return (
    <Collapsible open={open} onOpenChange={setOpen} asChild>
      <li className="rounded border border-border/40 text-sm">
        <CollapsibleTrigger
          asChild
          disabled={!hasMore}
        >
          <button
            type="button"
            className="flex w-full items-start gap-3 px-3 py-2 text-left hover:bg-muted/30 disabled:hover:bg-transparent"
          >
            <ChevronRight
              className={`mt-0.5 size-3.5 shrink-0 text-muted-foreground/60 transition-transform ${
                open ? "rotate-90" : ""
              } ${hasMore ? "" : "opacity-0"}`}
            />
            <span
              className={`shrink-0 rounded border px-1.5 py-0.5 font-mono text-[9px] uppercase tracking-wide ${statusTone}`}
            >
              {task.status}
            </span>
            {task.agentDefName ? (
              <span className="shrink-0 text-[10px] text-muted-foreground/70">
                {task.agentDefName}
              </span>
            ) : null}
            {task.attemptCount > 1 ? (
              <span className="shrink-0 text-[10px] text-muted-foreground/60">
                ({task.attemptCount}/{task.maxAttempts})
              </span>
            ) : null}
            <span className="min-w-0 flex-1 truncate text-foreground/80">
              {goalPreview}
            </span>
          </button>
        </CollapsibleTrigger>
        <CollapsibleContent className="border-t border-border/40 px-3 py-2 text-xs">
          <div className="mb-2">
            <div className="mb-1 font-mono text-[9px] uppercase tracking-wide text-muted-foreground/60">
              Goal
            </div>
            <pre className="max-h-64 overflow-auto whitespace-pre-wrap rounded bg-muted/30 px-2 py-1.5 text-[11px] text-foreground/90">
              {task.goal}
            </pre>
          </div>
          {task.parentIds.length > 0 ? (
            <div className="mb-2 text-[10px] text-muted-foreground/70">
              after: {task.parentIds.map((p) => p.slice(0, 8)).join(", ")}
            </div>
          ) : null}
          {task.result ? (
            <div className="mb-2">
              <div className="mb-1 font-mono text-[9px] uppercase tracking-wide text-muted-foreground/60">
                Result
              </div>
              <pre className="max-h-64 overflow-auto whitespace-pre-wrap rounded bg-muted/30 px-2 py-1.5 text-[11px] text-foreground/90">
                {task.result}
              </pre>
            </div>
          ) : null}
          {task.resultMetadata ? (
            <div>
              <div className="mb-1 font-mono text-[9px] uppercase tracking-wide text-muted-foreground/60">
                Result metadata
              </div>
              <pre className="max-h-64 overflow-auto whitespace-pre-wrap rounded bg-muted/30 px-2 py-1.5 text-[11px] text-foreground/90">
                {JSON.stringify(task.resultMetadata, null, 2)}
              </pre>
            </div>
          ) : null}
        </CollapsibleContent>
      </li>
    </Collapsible>
  );
}


function TasksList({
  taskBuckets,
  totalTasks,
}: {
  taskBuckets: ReturnType<typeof groupMissionTasksByBucket>;
  totalTasks: number;
}) {
  if (totalTasks === 0) {
    return (
      <div className="py-8 text-center text-sm text-muted-foreground/60">
        No tasks spawned yet.
      </div>
    );
  }
  const headings: Record<keyof typeof taskBuckets, string> = {
    in_flight: "In flight",
    blocked: "Blocked",
    done: "Done",
    failed_or_cancelled: "Failed / cancelled",
  };
  return (
    <div className="space-y-4">
      {(Object.keys(headings) as (keyof typeof taskBuckets)[]).map((bucket) => {
        const rows = taskBuckets[bucket];
        if (rows.length === 0) return null;
        return (
          <Fragment key={bucket}>
            <div className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground/70">
              {headings[bucket]} ({rows.length})
            </div>
            <ul className="space-y-1">
              {rows.map((task) => (
                <TaskRow key={task.id} task={task} />
              ))}
            </ul>
          </Fragment>
        );
      })}
    </div>
  );
}


function WorkersList({
  workers,
  onOpenTranscript,
}: {
  workers: AgentChatMissionWorker[];
  onOpenTranscript?: (workerSessionId: string) => void;
}) {
  if (workers.length === 0) {
    return (
      <div className="py-8 text-center text-sm text-muted-foreground/60">
        No workers attached to this mission right now.
      </div>
    );
  }
  return (
    <ul className="space-y-1">
      {workers.map((w) => {
        const onTranscriptClick = (
          e: React.MouseEvent<HTMLAnchorElement>,
        ) => {
          if (onOpenTranscript) {
            e.preventDefault();
            onOpenTranscript(w.workerSessionId);
          }
        };
        return (
          <li
            key={w.workerSessionId}
            className="flex flex-col gap-2 rounded border border-border/40 px-3 py-2 text-sm sm:flex-row sm:items-start sm:justify-between"
          >
            <div className="min-w-0 flex-1 space-y-1">
              <div className="flex flex-wrap items-center gap-2">
                <Badge
                  variant="outline"
                  className={MISSION_WORKER_KIND_BADGE_CLASS[w.kind]}
                  title={MISSION_WORKER_KIND_TOOLTIP[w.kind]}
                >
                  {w.kind}
                </Badge>
                <Badge className="text-foreground/70">
                  {w.kind === "task" ? w.taskStatus ?? "—" : w.sessionStatus}
                </Badge>
                {w.agentDefName ? (
                  <span className="text-[10px] text-muted-foreground/70">
                    {w.agentDefName}
                  </span>
                ) : null}
                {w.latestEventAt ? (
                  <span className="ml-auto font-mono text-[10px] text-muted-foreground/60">
                    {formatTimestamp(w.latestEventAt)}
                  </span>
                ) : null}
              </div>
              <div className="truncate text-sm text-foreground/80">
                {deriveMissionWorkerActivityLabel(w)}
              </div>
            </div>
            <a
              href={w.transcriptUrl}
              target="_blank"
              rel="noreferrer"
              onClick={onTranscriptClick}
              className="self-start text-xs text-primary hover:underline"
            >
              View session
            </a>
          </li>
        );
      })}
    </ul>
  );
}


function MetadataPane({ mission }: { mission: AgentChatMissionSummary }) {
  type Row = { label: string; value: React.ReactNode };
  const rows: Row[] = [];
  rows.push({
    label: "Mission ID",
    value: <code className="text-[11px]">{mission.id}</code>,
  });
  rows.push({
    label: "Session ID",
    value: <code className="text-[11px]">{mission.sessionId}</code>,
  });
  rows.push({
    label: "Agent ID",
    value: <code className="text-[11px]">{mission.agentId}</code>,
  });
  rows.push({ label: "Status", value: mission.status });
  rows.push({
    label: "Iteration",
    value: `${mission.iteration} / ${mission.maxIterations}`,
  });
  rows.push({
    label: "Owner",
    value: mission.userId
      ? <>user <code className="text-[11px]">{mission.userId}</code></>
      : mission.serviceAccountId
        ? <>service account <code className="text-[11px]">{mission.serviceAccountId}</code></>
        : "—",
  });
  rows.push({
    label: "Created",
    value: (
      <span className="font-mono text-[11px]">
        {new Date(mission.createdAt).toLocaleString()}
      </span>
    ),
  });
  rows.push({
    label: "Updated",
    value: (
      <span className="font-mono text-[11px]">
        {new Date(mission.updatedAt).toLocaleString()}
      </span>
    ),
  });
  if (mission.pausedReason) {
    rows.push({ label: "Paused reason", value: mission.pausedReason });
  }
  if (mission.cancelledReason) {
    rows.push({ label: "Cancelled reason", value: mission.cancelledReason });
  }

  return (
    <div className="space-y-6">
      {/* Description and Rubric live in the hero card — they don't
          repeat here.  Metadata is reserved for the things the hero
          doesn't show: evaluator detail + identifiers. */}
      {mission.lastEvaluationResult ? (
        <section className="space-y-2">
          <div className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground/70">
            Last verdict — {mission.lastEvaluationResult}
          </div>
          {mission.lastEvaluationFeedback ? (
            <p className="whitespace-pre-wrap text-sm text-foreground/90">
              {mission.lastEvaluationFeedback}
            </p>
          ) : null}
          {mission.lastEvaluationExplanation ? (
            <p className="whitespace-pre-wrap text-xs text-muted-foreground/70">
              {mission.lastEvaluationExplanation}
            </p>
          ) : null}
        </section>
      ) : null}
      <section>
        <div className="mb-2 font-mono text-[10px] uppercase tracking-widest text-muted-foreground/70">
          Identifiers
        </div>
        <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1 text-xs">
          {rows.map((r) => (
            <Fragment key={r.label}>
              <dt className="text-muted-foreground/70">{r.label}</dt>
              <dd className="break-all text-foreground/90">{r.value}</dd>
            </Fragment>
          ))}
        </dl>
      </section>
    </div>
  );
}
