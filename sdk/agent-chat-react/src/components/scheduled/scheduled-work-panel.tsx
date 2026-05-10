// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { formatDistanceToNow } from "date-fns";
import {
  CalendarClockIcon,
  ExternalLinkIcon,
  PlayIcon,
  Trash2Icon,
} from "lucide-react";
import { Badge } from "../ui/badge";
import { cn } from "../../lib/utils";
import type {
  AgentChatAdapter,
  AgentChatScheduledWorkItem,
} from "../../types";

export interface ScheduledWorkPanelProps {
  adapter: AgentChatAdapter;
  agentId?: string;
  title?: string;
  limit?: number;
  status?: string;
  hideHeader?: boolean;
  pollIntervalMs?: number;
  onSessionSelect?: (sessionId: string) => void;
  onScheduleCancel?: (scheduleId: string) => void;
  onScheduleRunNow?: (scheduleId: string, sessionId?: string) => void;
}

const DEFAULT_LIMIT = 50;
const DEFAULT_POLL_INTERVAL_MS = 30000;

function fingerprint(items: AgentChatScheduledWorkItem[]): string {
  return items
    .map(
      (item) =>
        `${item.id}:${item.status}:${item.kind ?? ""}:${item.name ?? ""}:${
          item.prompt
        }:${item.scheduleDisplay}:${item.runCount}:${item.nextRunAt ?? ""}:${
          item.lastRunAt ?? ""
        }:${item.lastSessionId ?? ""}:${item.lastError ?? ""}:${
          item.updatedAt
        }`,
    )
    .join("|");
}

function formatKind(value: string | null | undefined): string {
  if (value === "dynamic_loop") return "Dynamic loop";
  if (value === "cron") return "Cron";
  if (value === "one_shot") return "One-shot";
  if (value === "scheduled") return "Scheduled";
  if (!value) return "Scheduled";
  return value
    .split(/[_-]+/)
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

function formatRelative(value: string | null | undefined, label: string): string | null {
  if (!value) return null;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  return `${label} ${formatDistanceToNow(date, { addSuffix: true })}`;
}

function formatRuns(count: number): string {
  return `${count} ${count === 1 ? "run" : "runs"}`;
}

function scheduleTitle(item: AgentChatScheduledWorkItem): string {
  const title = item.name?.trim();
  if (title) return title;
  return item.prompt;
}

function sortScheduledWork(
  items: AgentChatScheduledWorkItem[],
): AgentChatScheduledWorkItem[] {
  return [...items].sort((a, b) => {
    if (a.status === "active" && b.status !== "active") return -1;
    if (a.status !== "active" && b.status === "active") return 1;
    const aNext = a.nextRunAt ?? "";
    const bNext = b.nextRunAt ?? "";
    if (aNext && bNext && aNext !== bNext) return aNext < bNext ? -1 : 1;
    if (aNext && !bNext) return -1;
    if (!aNext && bNext) return 1;
    return (a.updatedAt ?? "") > (b.updatedAt ?? "") ? -1 : 1;
  });
}

function ScheduledWorkRow({
  item,
  canRunNow,
  canCancel,
  actionScheduleId,
  onOpenLastRun,
  onRunNow,
  onCancel,
}: {
  item: AgentChatScheduledWorkItem;
  canRunNow: boolean;
  canCancel: boolean;
  actionScheduleId: string | null;
  onOpenLastRun: (sessionId: string) => void;
  onRunNow: (scheduleId: string) => void;
  onCancel: (scheduleId: string) => void;
}) {
  const isActive = item.status === "active";
  const disabled = actionScheduleId === item.id;
  const title = scheduleTitle(item);
  const statusText = item.status === "active" ? null : item.status;
  const meta = [
    item.scheduleDisplay,
    formatRelative(item.nextRunAt, "Next"),
    formatRelative(item.lastRunAt, "Last"),
    formatRuns(item.runCount),
    item.expiresAt ? formatRelative(item.expiresAt, "Expires") : null,
  ].filter(Boolean).join(" · ");

  return (
    <div className="group flex min-w-0 items-start gap-2 border-l-2 border-l-transparent px-3 py-2 text-sm transition-colors hover:border-l-primary hover:bg-input">
      <div className="mt-0.5 flex size-5 shrink-0 items-center justify-center text-faint">
        <CalendarClockIcon className="size-3.5" />
      </div>
      <div className="min-w-0 flex-1">
        <div className="flex min-w-0 items-center gap-2">
          <div className="truncate text-foreground">{title}</div>
          <Badge variant="secondary" className="shrink-0">
            {formatKind(item.kind)}
          </Badge>
          {statusText && (
            <Badge variant="destructive" className="shrink-0">
              {statusText}
            </Badge>
          )}
        </div>
        <div className="mt-0.5 truncate text-xs text-faint">{meta}</div>
        {item.lastError && (
          <div className="mt-1 line-clamp-2 text-xs text-destructive">
            {item.lastError}
          </div>
        )}
      </div>
      <div className="flex shrink-0 items-center gap-1">
        {item.lastSessionId && (
          <button
            type="button"
            className="rounded p-1 text-faint opacity-70 transition-all hover:bg-line hover:text-foreground group-hover:opacity-100"
            onClick={() => onOpenLastRun(item.lastSessionId as string)}
            aria-label="Open last run"
            title="Open last run"
          >
            <ExternalLinkIcon className="size-3.5" />
          </button>
        )}
        {canRunNow && isActive && (
          <button
            type="button"
            className="rounded p-1 text-faint opacity-70 transition-all hover:bg-line hover:text-foreground disabled:pointer-events-none disabled:opacity-40 group-hover:opacity-100"
            onClick={() => onRunNow(item.id)}
            aria-label="Run schedule now"
            title="Run schedule now"
            disabled={disabled}
          >
            <PlayIcon className="size-3.5" />
          </button>
        )}
        {canCancel && isActive && (
          <button
            type="button"
            className="rounded p-1 text-faint opacity-70 transition-all hover:bg-destructive/10 hover:text-destructive disabled:pointer-events-none disabled:opacity-40 group-hover:opacity-100"
            onClick={() => onCancel(item.id)}
            aria-label="Cancel schedule"
            title="Cancel schedule"
            disabled={disabled}
          >
            <Trash2Icon className="size-3.5" />
          </button>
        )}
      </div>
    </div>
  );
}

export function ScheduledWorkPanel({
  adapter,
  agentId,
  title = "Scheduled Work",
  limit = DEFAULT_LIMIT,
  status = "active",
  hideHeader = false,
  pollIntervalMs = DEFAULT_POLL_INTERVAL_MS,
  onSessionSelect,
  onScheduleCancel,
  onScheduleRunNow,
}: ScheduledWorkPanelProps) {
  const [items, setItems] = useState<AgentChatScheduledWorkItem[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [hasEverLoaded, setHasEverLoaded] = useState(false);
  const [actionScheduleId, setActionScheduleId] = useState<string | null>(null);
  const mounted = useRef(true);
  const requestId = useRef(0);
  const lastFingerprint = useRef("");

  const refetch = useCallback(
    async (opts?: { silent?: boolean }) => {
      if (!adapter.listScheduledWork) {
        setItems([]);
        setHasEverLoaded(true);
        return;
      }
      const currentRequestId = ++requestId.current;
      try {
        const list = await adapter.listScheduledWork({
          agentId,
          status,
          limit,
        });
        if (!mounted.current || currentRequestId !== requestId.current) return;
        const nextItems = sortScheduledWork(list.items);
        const fp = fingerprint(nextItems);
        if (fp !== lastFingerprint.current) {
          lastFingerprint.current = fp;
          setItems(nextItems);
        }
        setError(null);
        setHasEverLoaded(true);
      } catch (e) {
        if (!mounted.current || currentRequestId !== requestId.current) return;
        if (!opts?.silent) {
          setError(
            e instanceof Error ? e.message : "Failed to load scheduled work",
          );
          setHasEverLoaded(true);
        }
      }
    },
    [adapter, agentId, limit, status],
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
    if (!adapter.listScheduledWork || pollIntervalMs <= 0) return;
    const id = setInterval(() => {
      void refetch({ silent: true });
    }, pollIntervalMs);
    return () => clearInterval(id);
  }, [adapter.listScheduledWork, pollIntervalMs, refetch]);

  const activeCount = useMemo(
    () => items.filter((item) => item.status === "active").length,
    [items],
  );

  const handleOpenLastRun = useCallback(
    (sessionId: string) => {
      onSessionSelect?.(sessionId);
    },
    [onSessionSelect],
  );

  const handleRunNow = useCallback(
    async (scheduleId: string) => {
      if (!adapter.runScheduledWorkNow || actionScheduleId) return;
      setActionScheduleId(scheduleId);
      try {
        const result = await adapter.runScheduledWorkNow({ scheduleId });
        onScheduleRunNow?.(scheduleId, result?.sessionId);
        await refetch({ silent: true });
      } catch (e) {
        setError(e instanceof Error ? e.message : "Failed to run schedule");
      } finally {
        if (mounted.current) setActionScheduleId(null);
      }
    },
    [actionScheduleId, adapter, onScheduleRunNow, refetch],
  );

  const handleCancel = useCallback(
    async (scheduleId: string) => {
      if (!adapter.cancelScheduledWork || actionScheduleId) return;
      setActionScheduleId(scheduleId);
      try {
        await adapter.cancelScheduledWork({ scheduleId });
        setItems((current) => {
          const next = current.filter((item) => item.id !== scheduleId);
          lastFingerprint.current = fingerprint(next);
          return next;
        });
        onScheduleCancel?.(scheduleId);
        await refetch({ silent: true });
      } catch (e) {
        setError(e instanceof Error ? e.message : "Failed to cancel schedule");
      } finally {
        if (mounted.current) setActionScheduleId(null);
      }
    },
    [actionScheduleId, adapter, onScheduleCancel, refetch],
  );

  if (!hasEverLoaded) return null;
  if (!adapter.listScheduledWork) return null;

  return (
    <section className="min-w-0 border-t border-line">
      {!hideHeader && (
        <div className="flex items-center gap-1.5 px-3 py-2 text-xs font-semibold uppercase tracking-wide">
          <CalendarClockIcon className="size-3.5" />
          <span>{title}</span>
          {activeCount > 0 && (
            <Badge variant="default" className="ml-auto">
              {activeCount}
            </Badge>
          )}
        </div>
      )}
      {error && (
        <div className="px-3 py-2 text-xs text-destructive">{error}</div>
      )}
      {!error && items.length === 0 && (
        <div className="px-3 py-2 text-xs text-faint">No scheduled work</div>
      )}
      {!error && items.length > 0 && (
        <div className={cn("pb-2", hideHeader && "pt-1")}>
          {items.map((item) => (
            <ScheduledWorkRow
              key={item.id}
              item={item}
              canRunNow={Boolean(adapter.runScheduledWorkNow)}
              canCancel={Boolean(adapter.cancelScheduledWork)}
              actionScheduleId={actionScheduleId}
              onOpenLastRun={handleOpenLastRun}
              onRunNow={handleRunNow}
              onCancel={handleCancel}
            />
          ))}
        </div>
      )}
    </section>
  );
}
