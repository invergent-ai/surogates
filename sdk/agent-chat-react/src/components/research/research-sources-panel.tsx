// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Compact in-thread sources panel.  Sits as a fixed strip above the
// chat composer so citations stay close to the active conversation
// without stealing horizontal space.  Citation chips (``[S#]``) in
// the report deep-link into this list via element id
// ``source-<sourceId>``; opening the chip programmatically expands
// the panel so the target row is visible.

import { useEffect, useRef, useState } from "react";
import { ChevronRightIcon } from "lucide-react";

import { cn } from "../../lib/utils";
import type { AgentChatResearchSource } from "../../types";

export function ResearchSourcesPanel({
  sources,
}: {
  sources: AgentChatResearchSource[];
}) {
  const [expanded, setExpanded] = useState(false);
  // Stash the panel element so a citation-chip click that scrolls to
  // ``#source-<id>`` can force the panel open first -- otherwise the
  // anchor sits inside a hidden ``<ul>`` and ``scrollIntoView`` is a
  // no-op.
  const rootRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const root = rootRef.current;
    if (!root) return;
    const handler = (event: Event) => {
      const target = event.target as Element | null;
      if (target?.closest('[id^="source-"]')) {
        setExpanded(true);
      }
    };
    // Captures clicks anywhere in the document so a chip in the
    // message thread can request the panel open even though the
    // anchor lives inside the (currently hidden) list.
    document.addEventListener("scroll-to-source", handler, true);
    return () =>
      document.removeEventListener("scroll-to-source", handler, true);
  }, []);

  if (sources.length === 0) return null;

  return (
    <div
      ref={rootRef}
      className="rounded-md border border-line bg-card"
    >
      <button
        type="button"
        onClick={() => setExpanded((prev) => !prev)}
        aria-expanded={expanded}
        className="flex w-full items-center gap-1.5 px-2 py-1.5 text-left hover:bg-muted/40"
      >
        <ChevronRightIcon
          className={cn(
            "size-3 shrink-0 text-muted-foreground transition-transform duration-150",
            expanded && "rotate-90",
          )}
        />
        <span className="text-[10px] font-semibold uppercase tracking-widest text-muted-foreground">
          Sources · {sources.length}
        </span>
      </button>
      {expanded && (
        <ul className="max-h-35 overflow-y-auto border-t border-line py-1">
          {sources.map((s) => (
            <li key={s.sourceId}>
              <a
                id={`source-${s.sourceId}`}
                href={s.url}
                target="_blank"
                rel="noreferrer"
                className="group flex items-baseline gap-2 px-2 py-1 hover:bg-muted/60"
              >
                <span className="shrink-0 text-[10px] font-semibold text-primary tabular-nums">
                  {s.sourceId}
                </span>
                <span className="truncate text-xs text-foreground group-hover:underline">
                  {s.title || s.url}
                </span>
                <span className="ml-auto shrink-0 truncate text-[10px] text-muted-foreground max-w-[40%]">
                  {hostname(s.url)}
                </span>
              </a>
            </li>
          ))}
        </ul>
      )}
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
