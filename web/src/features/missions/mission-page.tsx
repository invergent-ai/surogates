// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Mission dashboard.
//
// Polls the /v1/missions/{id}* surface every 5s while the mission is
// active or paused; stops polling on terminal status. All API calls
// flow through the AgentChatAdapter contract (the SDK), not directly
// through fetch — see `features/chat/surogates-web-chat-adapter.ts`.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useParams } from "@tanstack/react-router";
import {
  Ban,
  Loader2,
  Pause,
  Play,
  RefreshCw,
  AlertCircle,
} from "lucide-react";

import type {
  AgentChatMissionSummary,
  AgentChatMissionTask,
  AgentChatMissionWorker,
} from "@invergent/agent-chat-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Separator } from "@/components/ui/separator";

import { surogatesWebChatAdapter } from "@/features/chat/surogates-web-chat-adapter";

import {
  ACTIVE_STATUSES,
  deriveWorkerActivityLabel,
  groupTasksByBucket,
  isTerminalStatus,
} from "./mission-derive";


// The adapter declares mission methods as optional so adapters that don't
// implement them still satisfy the contract. The surogates web adapter
// always defines them; we bind them once at module load and crash early
// if the binding is missing rather than scatter ``!`` assertions.
const missionApi = {
  getMission: surogatesWebChatAdapter.getMission,
  getMissionTasks: surogatesWebChatAdapter.getMissionTasks,
  getMissionWorkers: surogatesWebChatAdapter.getMissionWorkers,
  pauseMission: surogatesWebChatAdapter.pauseMission,
  resumeMission: surogatesWebChatAdapter.resumeMission,
  cancelMission: surogatesWebChatAdapter.cancelMission,
};
for (const [name, fn] of Object.entries(missionApi)) {
  if (typeof fn !== "function") {
    throw new Error(
      `surogatesWebChatAdapter is missing mission method ${name}; the dashboard requires the full mission surface.`,
    );
  }
}
const adapterMission = missionApi as {
  getMission: NonNullable<typeof surogatesWebChatAdapter.getMission>;
  getMissionTasks: NonNullable<typeof surogatesWebChatAdapter.getMissionTasks>;
  getMissionWorkers: NonNullable<
    typeof surogatesWebChatAdapter.getMissionWorkers
  >;
  pauseMission: NonNullable<typeof surogatesWebChatAdapter.pauseMission>;
  resumeMission: NonNullable<typeof surogatesWebChatAdapter.resumeMission>;
  cancelMission: NonNullable<typeof surogatesWebChatAdapter.cancelMission>;
};


const POLL_INTERVAL_MS = 5_000;


type MissionState = {
  mission: AgentChatMissionSummary | null;
  tasks: AgentChatMissionTask[];
  workers: AgentChatMissionWorker[];
  loading: boolean;
  error: string | null;
};


const INITIAL_STATE: MissionState = {
  mission: null,
  tasks: [],
  workers: [],
  loading: true,
  error: null,
};


export function MissionPage() {
  const { missionId } = useParams({ strict: false }) as {
    missionId: string | undefined;
  };
  const navigate = useNavigate();
  const [state, setState] = useState<MissionState>(INITIAL_STATE);
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
    if (!missionId) return;
    try {
      const [mission, tasksResp, workersResp] = await Promise.all([
        adapterMission.getMission({ missionId }),
        adapterMission.getMissionTasks({ missionId }),
        adapterMission.getMissionWorkers({ missionId }),
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
  }, [missionId]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // 5s polling — stops on terminal status.
  useEffect(() => {
    if (!state.mission) return;
    if (isTerminalStatus(state.mission.status)) return;
    const id = window.setInterval(() => {
      void refresh();
    }, POLL_INTERVAL_MS);
    return () => {
      window.clearInterval(id);
    };
  }, [refresh, state.mission]);

  const taskBuckets = useMemo(
    () => groupTasksByBucket(state.tasks),
    [state.tasks],
  );

  const runningWorkerCount = useMemo(
    () => state.workers.filter((w) => w.taskStatus === "running").length,
    [state.workers],
  );

  if (!missionId) {
    return (
      <div className="p-6 text-sm">
        Missing mission id in URL.
      </div>
    );
  }

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
      await adapterMission.pauseMission({ missionId });
      await refresh();
    } finally {
      setBusy(false);
    }
  };

  const doResume = async () => {
    setBusy(true);
    try {
      await adapterMission.resumeMission({ missionId });
      await refresh();
    } finally {
      setBusy(false);
    }
  };

  const doCancel = async () => {
    setBusy(true);
    try {
      await adapterMission.cancelMission({
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
              {ACTIVE_STATUSES.has(mission.status) ? (
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
              ) : (
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => navigate({ to: "/" })}
                >
                  Back
                </Button>
              )}
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
          {(["in_flight", "blocked", "done", "failed_or_cancelled"] as const).map(
            (bucket) => {
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
            },
          )}
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
              {state.workers.map((w) => (
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
                      {deriveWorkerActivityLabel(w)}
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
                    className="text-xs text-primary hover:underline self-start"
                  >
                    transcript
                  </a>
                </li>
              ))}
            </ul>
          )}
        </CardContent>
      </Card>

      <Separator />
      <div className="flex justify-between items-center text-xs text-muted-foreground">
        <span>
          Auto-refresh every {POLL_INTERVAL_MS / 1000}s while mission is
          active.
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

      <ConfirmDialog
        open={cancelOpen}
        title="Cancel mission?"
        description={
          <div className="space-y-2 text-sm">
            <p>
              This terminates the mission and clears its evaluator loop.
            </p>
            <label className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={cancelCascade}
                onChange={(e) => setCancelCascade(e.target.checked)}
              />
              <span>
                Also cancel <strong>{runningWorkerCount}</strong> running
                worker
                {runningWorkerCount === 1 ? "" : "s"} (sends an interrupt to
                each worker session).
              </span>
            </label>
          </div>
        }
        confirmLabel="Cancel mission"
        variant="destructive"
        onConfirm={doCancel}
        onCancel={() => setCancelOpen(false)}
      />
    </div>
  );
}
