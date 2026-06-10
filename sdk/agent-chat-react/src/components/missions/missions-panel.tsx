// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// MissionsPanel — sidebar entry for the user's missions.
//
// Layout/behaviour mirrors ``ScheduledWorkPanel`` so the two sit
// together in the sidebar with a consistent expand/collapse rhythm.
// Polls ``adapter.listMissions`` on a 30s cadence; renders one row per
// mission with a destination link the host wires to the
// mission dashboard route.
import {
  type KeyboardEvent,
  type MouseEvent,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { formatDistanceToNow } from "date-fns";
import {
  Ban,
  ChevronDownIcon,
  ChevronRightIcon,
  Pause,
  Play,
  TargetIcon,
} from "lucide-react";
import { Badge } from "../ui/badge";
import { cn } from "../../lib/utils";
import type {
  AgentChatAdapter,
  AgentChatMissionSummary,
} from "../../types";


export interface MissionsPanelProps {
  adapter: AgentChatAdapter;
  agentId?: string;
  title?: string;
  /** Comma-separated status filter passed to ``listMissions``. */
  status?: string;
  hideHeader?: boolean;
  pollIntervalMs?: number;
  onMissionSelect?: (missionId: string) => void;
  onMissionPause?: (missionId: string) => void;
  onMissionResume?: (missionId: string) => void;
  onMissionCancel?: (missionId: string) => void;
}


const DEFAULT_POLL_INTERVAL_MS = 30_000;


function fingerprint(items: AgentChatMissionSummary[]): string {
  return items
    .map(
      (m) =>
        `${m.id}:${m.status}:${m.iteration}:${m.lastEvaluationResult ?? ""}:` +
        `${m.lastEvaluationAt ?? ""}:${m.pausedReason ?? ""}:${m.updatedAt}`,
    )
    .join("|");
}


function sortMissions(
  items: AgentChatMissionSummary[],
): AgentChatMissionSummary[] {
  return [...items].sort((a, b) => {
    // Active first, then paused, then everything else (terminal rows
    // shouldn't normally appear here but we don't drop them silently).
    const rank = (s: string) =>
      s === "active" ? 0 : s === "paused" ? 1 : 2;
    const diff = rank(a.status) - rank(b.status);
    if (diff !== 0) return diff;
    return (a.updatedAt ?? "") > (b.updatedAt ?? "") ? -1 : 1;
  });
}


function MissionRow({
  mission,
  onOpen,
  onPause,
  onResume,
  onCancel,
  busy,
  canMutate,
}: {
  mission: AgentChatMissionSummary;
  onOpen: (id: string) => void;
  onPause: (id: string) => void;
  onResume: (id: string) => void;
  onCancel: (id: string) => void;
  busy: boolean;
  canMutate: {
    pause: boolean;
    resume: boolean;
    cancel: boolean;
  };
}) {
  const isActive = mission.status === "active";
  const isPaused = mission.status === "paused";
  const description = mission.description.trim() || "(no description)";
  const verdict = mission.lastEvaluationResult;
  const verdictAt = mission.lastEvaluationAt
    ? formatDistanceToNow(new Date(mission.lastEvaluationAt), {
        addSuffix: true,
      })
    : null;
  const meta = [
    `Iter ${mission.iteration}/${mission.maxIterations}`,
    verdict ? `Verdict: ${verdict}${verdictAt ? ` (${verdictAt})` : ""}` : null,
    isPaused && mission.pausedReason
      ? `Paused: ${mission.pausedReason}`
      : null,
  ].filter(Boolean) as string[];

  const stopActionClick = (e: MouseEvent<HTMLButtonElement>) => {
    e.stopPropagation();
  };
  const openMission = () => onOpen(mission.id);
  const handleRowKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key !== "Enter" && e.key !== " ") return;
    e.preventDefault();
    openMission();
  };

  return (
    <div
      className={cn(
        "group flex min-w-0 items-start gap-2 border-l-2 border-l-transparent px-3 py-2 text-sm transition-colors cursor-pointer hover:border-l-primary hover:bg-input",
        "min-h-11 md:min-h-0",
      )}
      onClick={openMission}
      onKeyDown={handleRowKeyDown}
      role="button"
      tabIndex={0}
      aria-label={`Open mission ${description}`}
      title="Open mission dashboard"
    >
      <div className="mt-0.5 flex size-5 shrink-0 items-center justify-center text-faint">
        <TargetIcon className="size-3.5" />
      </div>
      <div className="min-w-0 flex-1">
        <div className="flex min-w-0 items-start gap-2">
          <div className="min-w-0 flex-1">
            <div className="truncate text-foreground">{description}</div>
            <div className="mt-0.5 flex min-w-0 items-center gap-2">
              <Badge
                variant={isActive ? "default" : "secondary"}
                className="shrink-0"
              >
                {mission.status}
              </Badge>
            </div>
            {meta.length > 0 && (
              <div className="mt-0.5 space-y-0.5 text-xs text-faint">
                {meta.map((line) => (
                  <div key={line} className="truncate">
                    {line}
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      </div>
      <div className="flex shrink-0 items-center gap-1">
        {isActive && canMutate.pause && (
          <button
            type="button"
            className="rounded p-2 md:p-1 text-faint opacity-100 md:opacity-70 transition-all hover:bg-line hover:text-foreground disabled:pointer-events-none disabled:opacity-40 md:group-hover:opacity-100"
            onClick={(e) => {
              stopActionClick(e);
              onPause(mission.id);
            }}
            aria-label="Pause mission"
            title="Pause mission"
            disabled={busy}
          >
            <Pause className="size-3.5" />
          </button>
        )}
        {isPaused && canMutate.resume && (
          <button
            type="button"
            className="rounded p-2 md:p-1 text-faint opacity-100 md:opacity-70 transition-all hover:bg-line hover:text-foreground disabled:pointer-events-none disabled:opacity-40 md:group-hover:opacity-100"
            onClick={(e) => {
              stopActionClick(e);
              onResume(mission.id);
            }}
            aria-label="Resume mission"
            title="Resume mission"
            disabled={busy}
          >
            <Play className="size-3.5" />
          </button>
        )}
        {(isActive || isPaused) && canMutate.cancel && (
          <button
            type="button"
            className="rounded p-2 md:p-1 text-faint opacity-100 md:opacity-70 transition-all hover:bg-destructive/10 hover:text-destructive disabled:pointer-events-none disabled:opacity-40 md:group-hover:opacity-100"
            onClick={(e) => {
              stopActionClick(e);
              onCancel(mission.id);
            }}
            aria-label="Cancel mission"
            title="Cancel mission"
            disabled={busy}
          >
            <Ban className="size-3.5" />
          </button>
        )}
      </div>
    </div>
  );
}


export function MissionsPanel({
  adapter,
  agentId,
  title = "Missions",
  status,
  hideHeader = false,
  pollIntervalMs = DEFAULT_POLL_INTERVAL_MS,
  onMissionSelect,
  onMissionPause,
  onMissionResume,
  onMissionCancel,
}: MissionsPanelProps) {
  const [items, setItems] = useState<AgentChatMissionSummary[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [hasEverLoaded, setHasEverLoaded] = useState(false);
  const [busyMissionId, setBusyMissionId] = useState<string | null>(null);
  const [collapsed, setCollapsed] = useState(false);
  const mounted = useRef(true);
  const requestId = useRef(0);
  const lastFingerprint = useRef("");

  const refetch = useCallback(
    async (opts?: { silent?: boolean }) => {
      if (!adapter.listMissions) {
        setItems([]);
        setHasEverLoaded(true);
        return;
      }
      const currentRequestId = ++requestId.current;
      try {
        const list = await adapter.listMissions({ agentId, status });
        if (!mounted.current || currentRequestId !== requestId.current) return;
        const next = sortMissions(list.missions);
        const fp = fingerprint(next);
        if (fp !== lastFingerprint.current) {
          lastFingerprint.current = fp;
          setItems(next);
        }
        setError(null);
        setHasEverLoaded(true);
      } catch (e) {
        if (!mounted.current || currentRequestId !== requestId.current) return;
        if (!opts?.silent) {
          setError(
            e instanceof Error ? e.message : "Failed to load missions",
          );
          setHasEverLoaded(true);
        }
      }
    },
    [adapter, agentId, status],
  );

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  useEffect(() => {
    setError(null);
    lastFingerprint.current = "";
    void refetch();
  }, [refetch]);

  useEffect(() => {
    if (!adapter.listMissions || pollIntervalMs <= 0) return;
    const id = setInterval(() => {
      void refetch({ silent: true });
    }, pollIntervalMs);
    return () => clearInterval(id);
  }, [adapter.listMissions, pollIntervalMs, refetch]);

  const activeCount = useMemo(
    () => items.filter((m) => m.status === "active").length,
    [items],
  );

  const handleOpen = useCallback(
    (missionId: string) => {
      onMissionSelect?.(missionId);
    },
    [onMissionSelect],
  );

  const handlePause = useCallback(
    async (missionId: string) => {
      if (!adapter.pauseMission || busyMissionId) return;
      setBusyMissionId(missionId);
      try {
        await adapter.pauseMission({ missionId });
        onMissionPause?.(missionId);
        await refetch({ silent: true });
      } catch (e) {
        setError(e instanceof Error ? e.message : "Failed to pause mission");
      } finally {
        if (mounted.current) setBusyMissionId(null);
      }
    },
    [adapter, busyMissionId, onMissionPause, refetch],
  );

  const handleResume = useCallback(
    async (missionId: string) => {
      if (!adapter.resumeMission || busyMissionId) return;
      setBusyMissionId(missionId);
      try {
        await adapter.resumeMission({ missionId });
        onMissionResume?.(missionId);
        await refetch({ silent: true });
      } catch (e) {
        setError(e instanceof Error ? e.message : "Failed to resume mission");
      } finally {
        if (mounted.current) setBusyMissionId(null);
      }
    },
    [adapter, busyMissionId, onMissionResume, refetch],
  );

  const handleCancel = useCallback(
    async (missionId: string) => {
      if (!adapter.cancelMission || busyMissionId) return;
      setBusyMissionId(missionId);
      try {
        // Cascade so cancelling the mission also stops its in-flight workers
        // (otherwise sub-agent runs keep going long after cancellation).
        await adapter.cancelMission({ missionId, cascadeToWorkers: true });
        setItems((current) => {
          const next = current.filter((m) => m.id !== missionId);
          lastFingerprint.current = fingerprint(next);
          return next;
        });
        onMissionCancel?.(missionId);
        await refetch({ silent: true });
      } catch (e) {
        setError(e instanceof Error ? e.message : "Failed to cancel mission");
      } finally {
        if (mounted.current) setBusyMissionId(null);
      }
    },
    [adapter, busyMissionId, onMissionCancel, refetch],
  );

  if (!hasEverLoaded) return null;
  if (!adapter.listMissions) return null;

  const showBody = hideHeader || !collapsed;
  const canMutate = {
    pause: Boolean(adapter.pauseMission),
    resume: Boolean(adapter.resumeMission),
    cancel: Boolean(adapter.cancelMission),
  };

  return (
    <section className="min-w-0 border-t border-line">
      {!hideHeader && (
        <button
          type="button"
          className="flex w-full items-center gap-1.5 px-3 py-2 text-left text-xs font-semibold uppercase tracking-wide text-foreground transition-colors hover:bg-input"
          onClick={() => setCollapsed((c) => !c)}
          aria-expanded={!collapsed}
        >
          <TargetIcon className="size-3.5" />
          <span className="truncate">{title}</span>
          {activeCount > 0 && (
            <Badge variant="default" className="ml-auto">
              {activeCount}
            </Badge>
          )}
          {collapsed ? (
            <ChevronRightIcon className="size-3.5 shrink-0 text-faint" />
          ) : (
            <ChevronDownIcon className="size-3.5 shrink-0 text-faint" />
          )}
        </button>
      )}
      {showBody && error && (
        <div className="px-3 py-2 text-xs text-destructive">{error}</div>
      )}
      {showBody && !error && items.length === 0 && (
        <div className="px-3 py-2 text-xs text-faint">No missions</div>
      )}
      {showBody && !error && items.length > 0 && (
        <div className={cn("pb-2", hideHeader && "pt-1")}>
          {items.map((mission) => (
            <MissionRow
              key={mission.id}
              mission={mission}
              busy={busyMissionId === mission.id}
              canMutate={canMutate}
              onOpen={handleOpen}
              onPause={handlePause}
              onResume={handleResume}
              onCancel={handleCancel}
            />
          ))}
        </div>
      )}
    </section>
  );
}
