// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only

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
import { CalendarClockIcon, Trash2Icon } from "lucide-react";
import { Badge } from "@invergent/ui-sdk/badge";
import { Button } from "@invergent/ui-sdk/button";
import { type Tone, TonePill } from "@invergent/ui-sdk/chip";
import { Collapsible, CollapsibleContent } from "@invergent/ui-sdk/collapsible";
import { CollapsibleHeader } from "@invergent/ui-sdk/collapsible-header";
import { ListRow } from "@invergent/ui-sdk/list-row";
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
  // `dynamic_loop` and `one_shot` are what runtimes older than the cadence kinds send.
  if (value === "dynamic" || value === "dynamic_loop") return "Dynamic loop";
  if (value === "cron") return "Cron";
  if (value === "once" || value === "one_shot") return "One-shot";
  if (value === "scheduled") return "Scheduled";
  if (!value) return "Scheduled";
  return value
    .split(/[_-]+/)
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

function formatRelative(
  value: string | null | undefined,
  label: string,
  future = false,
): string | null {
  if (!value) return null;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  if (future && date.getTime() <= Date.now()) return `${label} due now`;
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

function canRunScheduleNow(item: AgentChatScheduledWorkItem): boolean {
  return item.repeatLimit === 1;
}

function scheduleDisplay(item: AgentChatScheduledWorkItem): string | null {
  if (item.kind === "dynamic" || item.kind === "dynamic_loop") return null;
  return item.scheduleDisplay;
}

// A stopped schedule's status as a pill tone (an active one shows none): a
// paused one warns, a failed one is bad, and a finished one is quiet.
function statusTone(status: string): Tone {
  if (status === "paused") return "warn";
  if (status === "failed") return "bad";
  return "muted";
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
  const showRunNow = canRunNow && isActive && canRunScheduleNow(item);
  const lastSessionId = item.lastSessionId ?? null;
  const meta = [
    scheduleDisplay(item),
    formatRelative(item.nextRunAt, "Next", true),
    formatRelative(item.lastRunAt, "Last"),
    formatRuns(item.runCount),
    item.expiresAt ? formatRelative(item.expiresAt, "Expires", true) : null,
  ].filter(Boolean);
  const openLastRun = () => {
    if (lastSessionId) onOpenLastRun(lastSessionId);
  };
  const handleRowKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if (!lastSessionId) return;
    if (e.key !== "Enter" && e.key !== " ") return;
    e.preventDefault();
    openLastRun();
  };
  const stopActionClick = (e: MouseEvent<HTMLButtonElement>) => {
    e.stopPropagation();
  };

  // The kit row, drawn onto a div: the row holds the action buttons, and a
  // button inside a button would be invalid HTML. Top-aligned, as the
  // description stacks the kind, the meta lines and the last error under the
  // title. It is only a button when there is a last run to open.
  return (
    <ListRow
      asChild
      size="sm"
      icon={CalendarClockIcon}
      label={title}
      description={
        <>
          <span className="mt-0.5 flex gap-1">
            <TonePill>{formatKind(item.kind)}</TonePill>
            {statusText && (
              <TonePill tone={statusTone(statusText)}>{statusText}</TonePill>
            )}
          </span>
          {meta.map((line) => (
            <span key={line} className="mt-0.5 block truncate">
              {line}
            </span>
          ))}
          {item.lastError && (
            <span className="mt-1 line-clamp-2 whitespace-normal text-destructive">
              {item.lastError}
            </span>
          )}
        </>
      }
      className={cn("items-start", lastSessionId && "cursor-pointer")}
      trailing={
        <>
          {showRunNow && (
            <Button
              variant="ghost"
              size="icon-xs"
              className="text-muted-foreground hover:text-foreground md:opacity-70 md:group-hover/list-row:opacity-100"
              onClick={(e) => {
                stopActionClick(e);
                onRunNow(item.id);
              }}
              aria-label="Run schedule now"
              title="Run schedule now"
              disabled={disabled}
            >
              <CalendarClockIcon className="size-3.5" />
            </Button>
          )}
          {canCancel && isActive && (
            <Button
              variant="ghost"
              size="icon-xs"
              className="text-muted-foreground hover:bg-destructive/10 hover:text-destructive md:opacity-70 md:group-hover/list-row:opacity-100"
              onClick={(e) => {
                stopActionClick(e);
                onCancel(item.id);
              }}
              aria-label="Cancel schedule"
              title="Cancel schedule"
              disabled={disabled}
            >
              <Trash2Icon className="size-3.5" />
            </Button>
          )}
        </>
      }
    >
      <div
        onClick={openLastRun}
        onKeyDown={handleRowKeyDown}
        role={lastSessionId ? "button" : undefined}
        tabIndex={lastSessionId ? 0 : undefined}
        aria-label={lastSessionId ? `Open last run for ${title}` : undefined}
        title={lastSessionId ? "Open last run" : undefined}
      />
    </ListRow>
  );
}

export function ScheduledWorkPanel({
  adapter,
  agentId,
  title = "Scheduled work",
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

  const body = error ? (
    <div className="px-2 py-1.5 text-xs text-destructive">{error}</div>
  ) : items.length === 0 ? (
    <div className="px-2 py-1.5 text-xs text-faint">No scheduled work</div>
  ) : (
    <div>
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
  );

  return (
    <section className="min-w-0 border-t border-line p-1">
      {hideHeader ? (
        body
      ) : (
        <Collapsible defaultOpen>
          <CollapsibleHeader icon={CalendarClockIcon}>
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
