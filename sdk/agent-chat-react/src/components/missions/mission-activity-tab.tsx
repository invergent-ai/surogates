// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Activity tab — single mission-wide timeline over the events feed,
// newest first, with client-side category filter chips. Rendered only
// when the adapter supports listMissionEvents (the dashboard hides
// the tab otherwise).
import { useMemo, useState } from "react";

import { type Tone, TagPill, TonePill } from "@invergent/ui-sdk/chip";
import { EmptyState } from "@invergent/ui-sdk/empty-state";
import { FilterPill } from "@invergent/ui-sdk/toggle";

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


const CATEGORY_TONE: Record<MissionEventCategory, Tone> = {
  spawn: "warn",
  output: "muted",
  done: "ok",
  verdict: "warn",
  system: "muted",
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
      <EmptyState className="py-8" title="No mission activity yet" />
    );
  }

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-1.5">
        {(["all", ...MISSION_EVENT_CATEGORIES] as const).map((cat) => (
          <FilterPill
            key={cat}
            on={filter === cat}
            onClick={() => setFilter(cat)}
            className="capitalize"
          >
            {cat}
          </FilterPill>
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
          <TonePill tone={CATEGORY_TONE[category]} className="capitalize">
            {category}
          </TonePill>
          <p className="shrink-0 text-xs text-foreground font-mono">
            {missionEventActorLabel(event, feed.sessions)}
          </p>
          {taskId ? (
            <TagPill label={taskId.slice(0, 8)} className="font-mono" />
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
