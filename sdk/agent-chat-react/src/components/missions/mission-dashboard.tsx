// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// MissionDashboard — host-agnostic component that renders the full
// mission state: header (status + iteration + last verdict), task DAG,
// live workers, and the controls (pause/resume/cancel-with-cascade).
//
// Hosts wrap this in their own page shell (sidebar, layout, route
// title). The dashboard owns its data layer: polls the adapter every
// 5s while active/paused, stops on terminal status.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Ban,
  Loader2,
  Pause,
  Play,
  RefreshCw,
  AlertCircle,
} from "lucide-react";

import { Badge } from "../ui/badge";
import { Button } from "../ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "../ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "../ui/dialog";
import { Separator } from "../ui/separator";

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
    () => state.workers.filter((w) => w.taskStatus === "running").length,
    [state.workers],
  );

  if (state.loading && !state.mission) {
    return (
      <div className="flex h-full items-center justify-center p-6">
        <Loader2 className="size-5 animate-spin text-muted-foreground" />
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

  const statusVariant: "default" | "destructive" | "secondary" =
    mission.status === "satisfied"
      ? "default"
      : mission.status === "failed" || mission.status === "cancelled"
        ? "destructive"
        : "secondary";

  return (
    <div className="flex flex-col gap-6 p-6 max-w-6xl mx-auto">
      {/* ----- Header ------------------------------------------------ */}
      <Card>
        <CardHeader>
          <div className="flex items-start justify-between gap-4">
            <div className="space-y-1">
              <CardTitle className="text-lg wrap-break-word">
                {mission.description}
              </CardTitle>
              <CardDescription className="wrap-break-word">
                Rubric: {mission.rubric}
              </CardDescription>
              <div className="flex items-center gap-3 pt-2 text-xs text-muted-foreground">
                <Badge variant={statusVariant}>{mission.status}</Badge>
                <span>
                  Iteration {mission.iteration}/{mission.maxIterations}
                </span>
                {mission.lastEvaluationResult ? (
                  <span>
                    Last verdict:{" "}
                    <span className="font-mono">
                      {mission.lastEvaluationResult}
                    </span>
                  </span>
                ) : null}
              </div>
            </div>
            <div className="flex items-center gap-2">
              {ACTIVE_MISSION_STATUSES.has(mission.status) ? (
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
            </div>
          </div>
        </CardHeader>
        {mission.lastEvaluationFeedback ? (
          <CardContent className="space-y-2">
            <div className="text-xs font-semibold uppercase tracking-widest text-muted-foreground">
              Evaluator feedback
            </div>
            <p className="text-sm whitespace-pre-wrap wrap-break-word">
              {mission.lastEvaluationFeedback}
            </p>
            {mission.lastEvaluationExplanation ? (
              <p className="text-xs text-muted-foreground whitespace-pre-wrap wrap-break-word">
                {mission.lastEvaluationExplanation}
              </p>
            ) : null}
          </CardContent>
        ) : null}
      </Card>

      {/* ----- Tasks -------------------------------------------------- */}
      <Card>
        <CardHeader>
          <CardTitle>Tasks ({state.tasks.length})</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          {(
            ["in_flight", "blocked", "done", "failed_or_cancelled"] as const
          ).map((bucket) => {
            const rows = taskBuckets[bucket];
            if (rows.length === 0) return null;
            const heading = (
              {
                in_flight: "In flight",
                blocked: "Blocked",
                done: "Done",
                failed_or_cancelled: "Failed / cancelled",
              } as const
            )[bucket];
            return (
              <div key={bucket} className="space-y-2">
                <div className="text-xs font-semibold uppercase tracking-widest text-muted-foreground">
                  {heading} ({rows.length})
                </div>
                <ul className="space-y-1.5">
                  {rows.map((task) => (
                    <li
                      key={task.id}
                      className="flex items-start justify-between gap-3 rounded border border-border/40 px-3 py-2 text-sm"
                    >
                      <div className="min-w-0 flex-1 space-y-1">
                        <div className="flex items-center gap-2">
                          <span className="font-mono text-xs text-muted-foreground">
                            {task.id.slice(0, 8)}
                          </span>
                          <Badge variant="secondary">{task.status}</Badge>
                          {task.agentDefName ? (
                            <span className="text-xs text-muted-foreground">
                              {task.agentDefName}
                            </span>
                          ) : null}
                          {task.attemptCount > 1 ? (
                            <span className="text-xs text-muted-foreground">
                              attempt {task.attemptCount}/{task.maxAttempts}
                            </span>
                          ) : null}
                        </div>
                        <div className="text-sm wrap-break-word">
                          {task.goal}
                        </div>
                        {task.parentIds.length > 0 ? (
                          <div className="text-xs text-muted-foreground">
                            after:{" "}
                            {task.parentIds
                              .map((p) => p.slice(0, 8))
                              .join(", ")}
                          </div>
                        ) : null}
                        {task.result ? (
                          <pre className="text-xs whitespace-pre-wrap bg-muted/30 rounded px-2 py-1 max-h-32 overflow-auto">
                            {task.result}
                          </pre>
                        ) : null}
                        {task.resultMetadata ? (
                          <pre className="text-xs whitespace-pre-wrap bg-muted/30 rounded px-2 py-1 max-h-32 overflow-auto">
                            {JSON.stringify(task.resultMetadata, null, 2)}
                          </pre>
                        ) : null}
                      </div>
                    </li>
                  ))}
                </ul>
              </div>
            );
          })}
          {state.tasks.length === 0 ? (
            <div className="text-sm text-muted-foreground">
              No tasks spawned yet.
            </div>
          ) : null}
        </CardContent>
      </Card>

      {/* ----- Live workers ------------------------------------------ */}
      <Card>
        <CardHeader>
          <CardTitle>Live workers ({state.workers.length})</CardTitle>
        </CardHeader>
        <CardContent>
          {state.workers.length === 0 ? (
            <div className="text-sm text-muted-foreground">
              No workers attached to this mission right now.
            </div>
          ) : (
            <ul className="space-y-1.5">
              {state.workers.map((w) => {
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
                    className="flex items-start justify-between gap-3 rounded border border-border/40 px-3 py-2 text-sm"
                  >
                    <div className="min-w-0 flex-1 space-y-1">
                      <div className="flex items-center gap-2">
                        <span className="font-mono text-xs text-muted-foreground">
                          T{w.taskId.slice(0, 8)}
                        </span>
                        <Badge variant="secondary">{w.taskStatus}</Badge>
                        {w.agentDefName ? (
                          <span className="text-xs text-muted-foreground">
                            {w.agentDefName}
                          </span>
                        ) : null}
                      </div>
                      <div className="text-sm wrap-break-word">
                        {deriveMissionWorkerActivityLabel(w)}
                      </div>
                      {w.latestEventAt ? (
                        <div className="text-xs text-muted-foreground">
                          {new Date(w.latestEventAt).toLocaleTimeString()}
                        </div>
                      ) : null}
                    </div>
                    <a
                      href={w.transcriptUrl}
                      target="_blank"
                      rel="noreferrer"
                      onClick={onTranscriptClick}
                      className="text-xs text-primary hover:underline self-start"
                    >
                      transcript
                    </a>
                  </li>
                );
              })}
            </ul>
          )}
        </CardContent>
      </Card>

      <Separator />
      <div className="flex justify-between items-center text-xs text-muted-foreground">
        <span>
          Auto-refresh every {pollIntervalMs / 1000}s while mission is active.
        </span>
        <Button
          variant="ghost"
          size="sm"
          onClick={() => {
            void refresh();
          }}
          disabled={busy}
        >
          <RefreshCw className="size-4" /> Refresh
        </Button>
      </div>

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
  );
}
