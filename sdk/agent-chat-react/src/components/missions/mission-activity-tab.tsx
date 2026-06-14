// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Activity tab — single mission-wide timeline over the events feed,
// newest first, with client-side category filter chips. Rendered only
// when the adapter supports listMissionEvents (the dashboard hides
// the tab otherwise).
import { useMemo, useState } from "react";

import type { AgentChatMissionEvent } from "../../types";

import {
  MISSION_EVENT_CATEGORIES,
  formatMissionTimestamp,
  missionEventActorLabel,
  missionEventCategory,
  missionEventSummary,
  missionEventTaskId,
  type MissionEventCategory,
} from "./mission-derive";
import type { MissionEventsFeed } from "./use-mission-events";
import { renderInlineMarkdown } from "../chat/inline-markdown";


export interface MissionActivityTabProps {
  feed: MissionEventsFeed;
}


const CATEGORY_BADGE_CLASS: Record<MissionEventCategory, string> = {
  spawn: "border-primary/30 bg-primary/10 text-primary",
  output: "border-foreground/20 bg-foreground/5 text-foreground/80",
  done: "border-emerald-500/30 bg-emerald-500/10 text-emerald-600",
  verdict: "border-amber-500/30 bg-amber-500/10 text-amber-600",
  system: "border-border bg-muted text-muted-foreground",
};


export function MissionActivityTab({ feed }: MissionActivityTabProps) {
  const [filter, setFilter] = useState<"all" | MissionEventCategory>("all");

  const rows = useMemo(() => {
    const newestFirst = [...feed.events].reverse();
    if (filter === "all") return newestFirst;
    return newestFirst.filter((e) => missionEventCategory(e.type) === filter);
  }, [feed.events, filter]);

  if (feed.events.length === 0) {
    return (
      <div className="py-8 text-center text-sm text-muted-foreground/60">
        No mission activity yet.
      </div>
    );
  }

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-1.5">
        {(["all", ...MISSION_EVENT_CATEGORIES] as const).map((cat) => (
          <button
            key={cat}
            type="button"
            onClick={() => setFilter(cat)}
            className={`rounded-full border px-2.5 py-0.5 font-mono text-[10px] uppercase tracking-wide transition-colors ${
              filter === cat
                ? "border-foreground bg-foreground text-background"
                : "border-border/60 bg-background text-muted-foreground hover:bg-muted/40"
            }`}
          >
            {cat}
          </button>
        ))}
      </div>

      <ol className="space-y-1">
        {rows.map((e) => (
          <ActivityRow key={e.id} event={e} feed={feed} />
        ))}
      </ol>
    </div>
  );
}


function ActivityRow({
  event,
  feed,
}: {
  event: AgentChatMissionEvent;
  feed: MissionEventsFeed;
}) {
  const category = missionEventCategory(event.type);
  const taskId = missionEventTaskId(event, feed.sessions);
  return (
    <li className="flex items-start gap-3 rounded px-2 py-1.5 text-sm hover:bg-muted/30">
      <span className="w-20 shrink-0 font-mono text-[10px] text-muted-foreground/60">
        {formatMissionTimestamp(event.createdAt)}
      </span>
      <div className="space-y-0.5">
        <div className="flex gap-2 items-center">
          <span
              className={`shrink-0 rounded border px-1.5 font-mono uppercase ${CATEGORY_BADGE_CLASS[category]}`}
            >
            {category}
          </span>
          <p className="shrink-0 text-xs text-foreground font-mono">
            {missionEventActorLabel(event, feed.sessions)}
          </p>
          {taskId ? (
            <p className="shrink-0 rounded bg-muted px-1.5 py-0.5 font-mono text-[9px] text-muted-foreground/70">
              {taskId.slice(0, 8)}
            </p>
          ) : null}
        </div>

        <div>
          <p className="min-w-0 flex-1 truncate text-foreground/80">
            {renderInlineMarkdown(missionEventSummary(event))}
          </p>
        </div>
      </div>
    
    </li>
  );
}
