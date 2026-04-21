// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// "Running" panel -- live view of the current session's sub-agents and
// delegation children.  Loads `/v1/sessions/{id}/tree`, polls on a
// cadence that adapts to whether any child is still running, and lets
// the user open or stop each child.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "@tanstack/react-router";
import {
  ChevronDownIcon,
  ChevronRightIcon,
  SquareIcon,
  UsersIcon,
} from "lucide-react";
import { toast } from "sonner";

import { getSessionTree, stopSession } from "@/api/sessions";
import type { SessionTreeNode } from "@/types/session";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

interface SessionTreePanelProps {
  sessionId: string;
  /** Treat the root as hidden, so its children appear as top-level rows. */
  hideRoot?: boolean;
}

interface TreeEntry extends SessionTreeNode {
  children: TreeEntry[];
}

// Poll cadence: tight while any sub-agent is still running, relaxed
// when every child has settled.  The idle cadence still exists so
// newly-spawned children surface without a page reload, but without
// charging the user 15 req/min for a frozen tree.
const POLL_INTERVAL_ACTIVE_MS = 4000;
const POLL_INTERVAL_IDLE_MS = 30000;

function buildTree(nodes: SessionTreeNode[]): TreeEntry[] {
  const byId = new Map<string, TreeEntry>();
  for (const n of nodes) byId.set(n.id, { ...n, children: [] });
  const roots: TreeEntry[] = [];
  for (const n of byId.values()) {
    const parent = n.parent_id ? byId.get(n.parent_id) : undefined;
    if (parent) {
      parent.children.push(n);
    } else {
      roots.push(n);
    }
  }
  const sortRec = (e: TreeEntry) => {
    // ISO-8601 timestamps sort lexicographically; plain string compare
    // is ~10x faster than localeCompare and poll frequency keeps this
    // on the hot path for large trees.
    e.children.sort((a, b) => (a.created_at < b.created_at ? -1 : 1));
    for (const c of e.children) sortRec(c);
  };
  for (const r of roots) sortRec(r);
  return roots;
}

function treeFingerprint(nodes: SessionTreeNode[]): string {
  // Cheap signature: only the fields that affect rendering.  If the
  // server returns structurally-identical data, skip the setState and
  // the entire rebuild / re-render cascade.
  return nodes
    .map(
      (n) =>
        `${n.id}:${n.parent_id ?? ""}:${n.status}:${n.agent_type ?? ""}:${
          n.message_count
        }:${n.tool_call_count}:${n.updated_at}`,
    )
    .join("|");
}

function statusColor(
  status: SessionTreeNode["status"],
): "default" | "secondary" | "destructive" | "outline" {
  switch (status) {
    case "active":
      return "default";
    case "completed":
      return "secondary";
    case "failed":
      return "destructive";
    default:
      return "outline";
  }
}

function TreeNodeRow({
  entry,
  depth,
  activeSessionId,
  onSelect,
  onStop,
}: {
  entry: TreeEntry;
  depth: number;
  activeSessionId: string;
  onSelect: (sessionId: string) => void;
  onStop: (sessionId: string) => void;
}) {
  const [expanded, setExpanded] = useState(true);
  const hasChildren = entry.children.length > 0;
  const isActive = entry.id === activeSessionId;
  const isRunning = entry.status === "active";

  return (
    <>
      {/* eslint-disable-next-line jsx-a11y/prefer-tag-over-role --
          The row has nested interactive elements (chevron + stop); a
          button-in-button would be invalid HTML.  Match the existing
          pattern from navbar.tsx / skills-page.tsx sidebar rows. */}
      <div
        role="button"
        tabIndex={0}
        onClick={() => onSelect(entry.id)}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") onSelect(entry.id);
        }}
        className={cn(
          "group flex items-center gap-1.5 py-1.5 pr-1 rounded cursor-pointer text-sm transition-colors",
          isActive
            ? "bg-line text-foreground"
            : "hover:bg-input text-subtle hover:text-foreground",
        )}
        style={{ paddingLeft: `${depth * 12 + 4}px` }}
      >
        {hasChildren ? (
          <button
            type="button"
            className="p-0.5 rounded hover:bg-line"
            onClick={(e) => {
              e.stopPropagation();
              setExpanded(!expanded);
            }}
            aria-label={expanded ? "Collapse" : "Expand"}
            aria-expanded={expanded}
          >
            {expanded ? (
              <ChevronDownIcon className="w-3.5 h-3.5" />
            ) : (
              <ChevronRightIcon className="w-3.5 h-3.5" />
            )}
          </button>
        ) : (
          <span className="w-4 h-4 shrink-0" />
        )}
        <div className="flex-1 min-w-0 flex items-center gap-1.5">
          <span className="truncate">
            {entry.title ?? entry.channel ?? "session"}
          </span>
          {entry.agent_type && (
            <Badge variant="outline" className="h-4 px-1.5 text-[10px]">
              {entry.agent_type}
            </Badge>
          )}
          <Badge
            variant={statusColor(entry.status)}
            className="h-4 px-1.5 text-[10px]"
          >
            {entry.status}
          </Badge>
        </div>
        <span
          className="text-xs text-faint shrink-0 tabular-nums"
          aria-label={`${entry.message_count} messages, ${entry.tool_call_count} tool calls`}
          title={`${entry.message_count} messages, ${entry.tool_call_count} tool calls`}
        >
          {entry.message_count}m·{entry.tool_call_count}t
        </span>
        {isRunning && (
          <button
            type="button"
            className="p-1 rounded opacity-0 group-hover:opacity-100 hover:bg-destructive/10 hover:text-destructive transition-all"
            onClick={(e) => {
              e.stopPropagation();
              onStop(entry.id);
            }}
            aria-label="Stop sub-agent"
            title="Stop sub-agent"
          >
            <SquareIcon className="w-3 h-3" fill="currentColor" />
          </button>
        )}
      </div>
      {hasChildren && expanded &&
        entry.children.map((child) => (
          <TreeNodeRow
            key={child.id}
            entry={child}
            depth={depth + 1}
            activeSessionId={activeSessionId}
            onSelect={onSelect}
            onStop={onStop}
          />
        ))}
    </>
  );
}

export function SessionTreePanel({
  sessionId,
  hideRoot = false,
}: SessionTreePanelProps) {
  const navigate = useNavigate();
  const [nodes, setNodes] = useState<SessionTreeNode[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [hasEverLoaded, setHasEverLoaded] = useState(false);

  // ``active`` guards async setters from firing after unmount or after
  // the effect tears down on ``sessionId`` change.  The closure-captured
  // ``cancelled`` flag in older revisions didn't protect in-flight
  // fetches because the flag lived on the callback, not on a ref.
  const mounted = useRef(true);
  // Previous payload fingerprint -- skip setState when unchanged so we
  // don't rebuild the tree and re-render every row on every poll of a
  // frozen session.
  const lastFingerprint = useRef<string>("");

  const refetch = useCallback(
    async (opts?: { silent?: boolean }) => {
      if (!opts?.silent) setLoading(true);
      try {
        const res = await getSessionTree(sessionId);
        if (!mounted.current) return;
        const fp = treeFingerprint(res.nodes);
        if (fp !== lastFingerprint.current) {
          lastFingerprint.current = fp;
          setNodes(res.nodes);
        }
        if (error !== null) setError(null);
        if (!hasEverLoaded) setHasEverLoaded(true);
      } catch (e) {
        if (!mounted.current) return;
        // Silent polls must not clobber the last-known-good view --
        // one transient failure would otherwise flip the panel to a
        // red error block until the next successful poll.
        if (!opts?.silent) {
          setError(e instanceof Error ? e.message : "Failed to load tree");
        }
      } finally {
        if (mounted.current && !opts?.silent) setLoading(false);
      }
    },
    [sessionId, error, hasEverLoaded],
  );

  // Mount / unmount flag for the component lifetime.  Reset on
  // ``sessionId`` change so the fresh effect run sees ``mounted.current
  // === true`` after the cleanup above.
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  useEffect(() => {
    void refetch();
  }, [refetch]);

  // Adaptive polling: 4s while any child is running, 30s when every
  // child has settled.  Pulling the interval from ``nodes`` lets the
  // effect re-run whenever the tree changes state.
  const runningCount = useMemo(
    () => nodes.filter((n) => n.status === "active").length,
    [nodes],
  );
  useEffect(() => {
    const interval =
      runningCount > 0 ? POLL_INTERVAL_ACTIVE_MS : POLL_INTERVAL_IDLE_MS;
    const id = setInterval(() => {
      void refetch({ silent: true });
    }, interval);
    return () => clearInterval(id);
  }, [refetch, runningCount]);

  const roots = useMemo(() => buildTree(nodes), [nodes]);

  const handleSelect = useCallback(
    (id: string) => {
      void navigate({ to: "/chat/$sessionId", params: { sessionId: id } });
    },
    [navigate],
  );

  const handleStop = useCallback(
    async (id: string) => {
      try {
        await stopSession(id);
        toast.success("Sub-agent stopped.");
        await refetch({ silent: true });
      } catch (e) {
        toast.error(e instanceof Error ? e.message : "Failed to stop sub-agent");
      }
    },
    [refetch],
  );

  // Hide the panel until the first fetch has completed so we don't
  // flash an empty "Loading…" header for sessions with no sub-agents.
  if (!hasEverLoaded) return null;
  if (nodes.length <= 1) return null;

  const topLevel: TreeEntry[] = hideRoot ? roots.flatMap((r) => r.children) : roots;
  if (topLevel.length === 0) return null;

  return (
    <div className="border-t border-line">
      <div className="flex items-center gap-1.5 px-3 py-2 text-xs font-semibold uppercase tracking-wide text-faint">
        <UsersIcon className="w-3.5 h-3.5" />
        <span>Running</span>
        {runningCount > 0 && (
          <Badge variant="default" className="h-4 px-1.5 text-[10px] ml-auto">
            {runningCount}
          </Badge>
        )}
      </div>
      {loading && (
        <div className="px-3 py-2 text-xs text-faint">Loading…</div>
      )}
      {error && (
        <div className="px-3 py-2 text-xs text-destructive">{error}</div>
      )}
      {!error && (
        <div className="px-1 pb-2">
          {topLevel.map((entry) => (
            <TreeNodeRow
              key={entry.id}
              entry={entry}
              depth={0}
              activeSessionId={sessionId}
              onSelect={handleSelect}
              onStop={handleStop}
            />
          ))}
        </div>
      )}
    </div>
  );
}
