// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Sidebar panel listing the curated research sources.  Citation chips
// (``[S#]``) in the report deep-link here via element id
// ``source-<sourceId>`` — the host wires that with the runtime helper
// in ``agent-chat.tsx``.

import type { AgentChatResearchSource } from "../../types";

export function ResearchSourcesPanel({
  sources,
}: {
  sources: AgentChatResearchSource[];
}) {
  if (sources.length === 0) {
    return (
      <div className="p-3 text-xs text-muted-foreground">
        No research sources yet. Sources appear here as the deep-research
        agent curates evidence.
      </div>
    );
  }
  return (
    <div className="flex flex-col gap-1 p-2">
      <div className="px-1 pb-1 text-[10px] font-semibold uppercase tracking-widest text-muted-foreground">
        Sources · {sources.length}
      </div>
      {sources.map((s) => (
        <a
          key={s.sourceId}
          id={`source-${s.sourceId}`}
          href={s.url}
          target="_blank"
          rel="noreferrer"
          className="group flex items-baseline gap-2 rounded-sm px-1 py-1 hover:bg-muted"
        >
          <span className="shrink-0 text-[10px] font-semibold text-primary">
            {s.sourceId}
          </span>
          <span className="flex flex-col overflow-hidden">
            <span className="truncate text-xs text-foreground group-hover:underline">
              {s.title || s.url}
            </span>
            <span className="truncate text-[10px] text-muted-foreground">
              {hostname(s.url)}
            </span>
          </span>
        </a>
      ))}
    </div>
  );
}

function hostname(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return url;
  }
}
