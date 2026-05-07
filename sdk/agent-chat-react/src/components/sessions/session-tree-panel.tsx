// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// "Running" panel -- live view of the current session's sub-agents and
// delegation children.  Loads `/v1/sessions/{id}/tree`, polls on a
// cadence that adapts to whether any child is still running, and lets
// the user open or stop each child.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ChevronDownIcon,
  ChevronRightIcon,
  SquareIcon,
  Trash2Icon,
  UsersIcon,
} from "lucide-react";
import { Badge } from "../ui/badge";
import { cn } from "../../lib/utils";
import type {
  AgentChatAdapter,
  AgentChatSession,
  AgentChatSessionTreeNode,
} from "../../types";

export interface SessionTreePanelProps {
  adapter: AgentChatAdapter;
  sessionId?: string | null;
  activeSessionId?: string;
  agentId?: string;
  title?: string;
  sessionListLimit?: number;
  /** Treat the root as hidden, so its children appear as top-level rows. */
  hideRoot?: boolean;
  onSessionSelect?: (sessionId: string) => void;
  onSessionDelete?: (sessionId: string) => void;
}

interface TreeEntry extends AgentChatSessionTreeNode {
  children: TreeEntry[];
}

// Poll cadence: tight while any sub-agent is still running, relaxed
// when every child has settled.  The idle cadence still exists so
// newly-spawned children surface without a page reload, but without
// charging the user 15 req/min for a frozen tree.
const POLL_INTERVAL_ACTIVE_MS = 4000;
const POLL_INTERVAL_IDLE_MS = 30000;
const DEFAULT_SESSION_LIST_LIMIT = 50;

function buildTree(nodes: AgentChatSessionTreeNode[]): TreeEntry[] {
  const byId = new Map<string, TreeEntry>();
  for (const n of nodes) byId.set(n.id, { ...n, children: [] });
  const roots: TreeEntry[] = [];
  for (const n of byId.values()) {
    const parent = n.parentId ? byId.get(n.parentId) : undefined;
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
    e.children.sort((a, b) => (a.createdAt < b.createdAt ? -1 : 1));
    for (const c of e.children) sortRec(c);
  };
  for (const r of roots) sortRec(r);
  return roots;
}

function treeFingerprint(nodes: AgentChatSessionTreeNode[]): string {
  // Cheap signature: only the fields that affect rendering.  If the
  // server returns structurally-identical data, skip the setState and
  // the entire rebuild / re-render cascade.
  return nodes
    .map(
      (n) =>
        `${n.id}:${n.parentId ?? ""}:${n.status}:${n.agentType ?? ""}:${
          n.messageCount ?? 0
        }:${n.toolCallCount ?? 0}:${n.updatedAt}`,
    )
    .join("|");
}

function sessionToTreeNode(session: AgentChatSession): AgentChatSessionTreeNode {
  const timestamp = session.updatedAt ?? session.createdAt ?? "";
  return {
    id: session.id,
    parentId: session.parentId ?? null,
    agentId: session.agentId,
    channel: session.channel,
    status: session.status,
    title: session.title,
    model: session.model,
    messageCount: session.messageCount,
    toolCallCount: session.toolCallCount,
    createdAt: session.createdAt ?? timestamp,
    updatedAt: session.updatedAt ?? timestamp,
  };
}

function mergeNodeFields(
  current: AgentChatSessionTreeNode,
  next: AgentChatSessionTreeNode,
): AgentChatSessionTreeNode {
  return {
    ...current,
    ...Object.fromEntries(
      Object.entries(next).filter(([, value]) => value !== undefined),
    ),
    messageCount: next.messageCount ?? current.messageCount,
    toolCallCount: next.toolCallCount ?? current.toolCallCount,
  } as AgentChatSessionTreeNode;
}

function mergeTreeNodes(
  groups: AgentChatSessionTreeNode[][],
): AgentChatSessionTreeNode[] {
  const byId = new Map<string, AgentChatSessionTreeNode>();
  for (const group of groups) {
    for (const node of group) {
      const current = byId.get(node.id);
      byId.set(node.id, current ? mergeNodeFields(current, node) : node);
    }
  }
  return Array.from(byId.values());
}

function statusColor(
  status: AgentChatSessionTreeNode["status"],
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
  canStop,
  canDelete,
  deletingSessionId,
  onSelect,
  onStop,
  onDelete,
}: {
  entry: TreeEntry;
  depth: number;
  activeSessionId: string;
  canStop: boolean;
  canDelete: boolean;
  deletingSessionId: string | null;
  onSelect: (sessionId: string) => void;
  onStop: (sessionId: string) => void;
  onDelete: (sessionId: string) => void;
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
          <span className="truncate">{entry.title ?? "session"}</span>
          {entry.agentType && (
            <Badge variant="outline" className="h-4 px-1.5 text-[10px]">
              {entry.agentType}
            </Badge>
          )}
          <Badge
            variant={statusColor(entry.status)}
            className="h-4 px-1.5 text-[10px]"
          >
            {entry.status}
          </Badge>
        </div>
        {isRunning && canStop && (
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
        {canDelete && (
          <button
            type="button"
            className="p-1 rounded opacity-0 group-hover:opacity-100 focus-visible:opacity-100 hover:bg-destructive/10 hover:text-destructive disabled:pointer-events-none disabled:opacity-50 transition-all"
            onClick={(e) => {
              e.stopPropagation();
              onDelete(entry.id);
            }}
            aria-label="Delete session"
            title="Delete session"
            disabled={deletingSessionId === entry.id}
          >
            <Trash2Icon className="w-3 h-3" />
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
            canStop={canStop}
            canDelete={canDelete}
            deletingSessionId={deletingSessionId}
            onSelect={onSelect}
            onStop={onStop}
            onDelete={onDelete}
          />
        ))}
    </>
  );
}

export function SessionTreePanel({
  adapter,
  sessionId = null,
  activeSessionId = sessionId ?? undefined,
  agentId,
  title = "Running",
  sessionListLimit = DEFAULT_SESSION_LIST_LIMIT,
  hideRoot = false,
  onSessionSelect,
  onSessionDelete,
}: SessionTreePanelProps) {
  const [nodes, setNodes] = useState<AgentChatSessionTreeNode[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [hasEverLoaded, setHasEverLoaded] = useState(false);
  const [deletingSessionId, setDeletingSessionId] = useState<string | null>(
    null,
  );

  // Guard async setters from firing after unmount.
  const mounted = useRef(true);
  // Monotonic fetch id. If a route change or newer poll starts while an
  // older request is in flight, only the newest response may update state.
  const requestId = useRef(0);
  // Previous payload fingerprint -- skip setState when unchanged so we
  // don't rebuild the tree and re-render every row on every poll of a
  // frozen session.
  const lastFingerprint = useRef<string>("");

  const refetch = useCallback(
    async (opts?: { silent?: boolean }) => {
      const canLoadSessionList = Boolean(agentId && adapter.listSessions);
      const canLoadSessionTree = Boolean(sessionId && adapter.getSessionTree);
      if (!canLoadSessionList && !canLoadSessionTree) {
        setNodes([]);
        setHasEverLoaded(true);
        setLoading(false);
        return;
      }
      const currentRequestId = ++requestId.current;
      if (!opts?.silent) setLoading(true);
      try {
        const sessionListPromise =
          agentId && adapter.listSessions
            ? adapter.listSessions({
                agentId,
                limit: sessionListLimit,
              })
            : Promise.resolve(null);
        const sessionTreePromise =
          sessionId && adapter.getSessionTree
            ? adapter.getSessionTree({ sessionId })
            : Promise.resolve(null);
        const [sessionList, sessionTree] = await Promise.all([
          sessionListPromise,
          sessionTreePromise,
        ]);
        if (!mounted.current || currentRequestId !== requestId.current) return;
        const nextNodes = mergeTreeNodes([
          sessionList?.sessions.map(sessionToTreeNode) ?? [],
          sessionTree?.nodes ?? [],
        ]);
        const fp = treeFingerprint(nextNodes);
        if (fp !== lastFingerprint.current) {
          lastFingerprint.current = fp;
          setNodes(nextNodes);
        }
        setError(null);
        setHasEverLoaded(true);
      } catch (e) {
        if (!mounted.current || currentRequestId !== requestId.current) return;
        // Silent polls must not clobber the last-known-good view --
        // one transient failure would otherwise flip the panel to a
        // red error block until the next successful poll.
        if (!opts?.silent) {
          setError(e instanceof Error ? e.message : "Failed to load tree");
        }
      } finally {
        if (mounted.current && currentRequestId === requestId.current && !opts?.silent) {
          setLoading(false);
        }
      }
    },
    [adapter, agentId, sessionId, sessionListLimit],
  );

  // Mount / unmount flag for the component lifetime.
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  useEffect(() => {
    setNodes([]);
    setError(null);
    setHasEverLoaded(false);
    lastFingerprint.current = "";
    void refetch();
  }, [refetch, sessionId]);

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
      onSessionSelect?.(id);
    },
    [onSessionSelect],
  );

  const handleStop = useCallback(
    async (id: string) => {
      if (!adapter.stopSession) return;
      try {
        await adapter.stopSession({ sessionId: id });
        await refetch({ silent: true });
      } catch (e) {
        setError(e instanceof Error ? e.message : "Failed to stop sub-agent");
      }
    },
    [adapter, refetch],
  );

  const handleDelete = useCallback(
    async (id: string) => {
      if (!adapter.deleteSession || deletingSessionId) return;
      setDeletingSessionId(id);
      try {
        await adapter.deleteSession({ sessionId: id });
        onSessionDelete?.(id);
        await refetch({ silent: true });
      } catch (e) {
        setError(e instanceof Error ? e.message : "Failed to delete session");
      } finally {
        if (mounted.current) setDeletingSessionId(null);
      }
    },
    [adapter, deletingSessionId, onSessionDelete, refetch],
  );

  // Hide the panel until the first fetch has completed so we don't
  // flash an empty "Loading..." header for sessions with no sub-agents.
  if (!hasEverLoaded) return null;
  if (nodes.length === 0) return null;

  const topLevel: TreeEntry[] = hideRoot
    ? roots.flatMap((r) => r.children)
    : roots;
  if (topLevel.length === 0) return null;

  return (
    <div className="border-t border-line">
      <div className="flex items-center gap-1.5 px-3 py-2 text-xs font-semibold uppercase tracking-wide text-faint">
        <UsersIcon className="w-3.5 h-3.5" />
        <span>{title}</span>
        {runningCount > 0 && (
          <Badge variant="default" className="h-4 px-1.5 text-[10px] ml-auto">
            {runningCount}
          </Badge>
        )}
      </div>
      {loading && (
        <div className="px-3 py-2 text-xs text-faint">Loading...</div>
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
              activeSessionId={activeSessionId ?? ""}
              canStop={Boolean(adapter.stopSession)}
              canDelete={Boolean(adapter.deleteSession)}
              deletingSessionId={deletingSessionId}
              onSelect={handleSelect}
              onStop={handleStop}
              onDelete={handleDelete}
            />
          ))}
        </div>
      )}
    </div>
  );
}
