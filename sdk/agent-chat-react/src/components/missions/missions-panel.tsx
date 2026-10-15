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
import { Ban, Pause, Play, TargetIcon } from "lucide-react";
import { Badge } from "@invergent/ui-sdk/badge";
import { Button } from "@invergent/ui-sdk/button";
import { type Tone, TonePill } from "@invergent/ui-sdk/chip";
import { Collapsible, CollapsibleContent } from "@invergent/ui-sdk/collapsible";
import { CollapsibleHeader } from "@invergent/ui-sdk/collapsible-header";
import { ListRow } from "@invergent/ui-sdk/list-row";
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


// A mission's status as a pill tone: a running mission reads ok, one waiting
// on someone warns, one that gave up is bad, and a settled one is quiet.
function missionTone(status: string): Tone {
  if (status === "active") return "ok";
  if (status === "paused" || status === "blocked") return "warn";
  if (status === "failed" || status === "max_iterations_reached") return "bad";
  return "muted";
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

  // The kit row, drawn onto a div: the row holds the action buttons, and a
  // button inside a button would be invalid HTML. Top-aligned, as the
  // description stacks the status and the meta lines under the title.
  return (
    <ListRow
      asChild
      size="sm"
      icon={TargetIcon}
      label={description}
      description={
        <>
          <span className="mt-0.5 flex">
            <TonePill tone={missionTone(mission.status)}>{mission.status}</TonePill>
          </span>
          {meta.map((line) => (
            <span key={line} className="mt-0.5 block truncate">
              {line}
            </span>
          ))}
        </>
      }
      className="cursor-pointer items-start"
      trailing={
        <>
          {isActive && canMutate.pause && (
            <Button
              variant="ghost"
              size="icon-xs"
              className="text-muted-foreground hover:text-foreground md:opacity-70 md:group-hover/list-row:opacity-100"
              onClick={(e) => {
                stopActionClick(e);
                onPause(mission.id);
              }}
              aria-label="Pause mission"
              title="Pause mission"
              disabled={busy}
            >
              <Pause className="size-3.5" />
            </Button>
          )}
          {isPaused && canMutate.resume && (
            <Button
              variant="ghost"
              size="icon-xs"
              className="text-muted-foreground hover:text-foreground md:opacity-70 md:group-hover/list-row:opacity-100"
              onClick={(e) => {
                stopActionClick(e);
                onResume(mission.id);
              }}
              aria-label="Resume mission"
              title="Resume mission"
              disabled={busy}
            >
              <Play className="size-3.5" />
            </Button>
          )}
          {(isActive || isPaused) && canMutate.cancel && (
            <Button
              variant="ghost"
              size="icon-xs"
              className="text-muted-foreground hover:bg-destructive/10 hover:text-destructive md:opacity-70 md:group-hover/list-row:opacity-100"
              onClick={(e) => {
                stopActionClick(e);
                onCancel(mission.id);
              }}
              aria-label="Cancel mission"
              title="Cancel mission"
              disabled={busy}
            >
              <Ban className="size-3.5" />
            </Button>
          )}
        </>
      }
    >
      <div
        onClick={openMission}
        onKeyDown={handleRowKeyDown}
        role="button"
        tabIndex={0}
        aria-label={`Open mission ${description}`}
        title="Open mission dashboard"
      />
    </ListRow>
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

  const canMutate = {
    pause: Boolean(adapter.pauseMission),
    resume: Boolean(adapter.resumeMission),
    cancel: Boolean(adapter.cancelMission),
  };

  const body = error ? (
    <div className="px-2 py-1.5 text-xs text-destructive">{error}</div>
  ) : items.length === 0 ? (
    <div className="px-2 py-1.5 text-xs text-faint">No missions</div>
  ) : (
    <div>
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
  );

  return (
    <section className="min-w-0 border-t border-line p-1">
      {hideHeader ? (
        body
      ) : (
        <Collapsible defaultOpen>
          <CollapsibleHeader icon={TargetIcon}>
            {title}
            {activeCount > 0 && (
              <Badge variant="default" className="ml-1.5">
                {activeCount}
              </Badge>
            )}
          </CollapsibleHeader>
          <CollapsibleContent>{body}</CollapsibleContent>
        </Collapsible>
      )}
    </section>
  );
}
