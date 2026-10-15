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

import {
  Collapsible,
  CollapsibleContent,
} from "@invergent/ui-sdk/collapsible";
import { CollapsibleHeader } from "@invergent/ui-sdk/collapsible-header";
import { ListRow } from "@invergent/ui-sdk/list-row";

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
    <Collapsible
      ref={rootRef}
      open={expanded}
      onOpenChange={setExpanded}
      className="rounded-[10px] border border-foreground/[0.08] bg-card"
    >
      <CollapsibleHeader variant="section">
        Sources · {sources.length}
      </CollapsibleHeader>
      <CollapsibleContent>
        <ul className="max-h-35 overflow-y-auto border-t border-foreground/[0.08] p-1">
          {sources.map((s) => (
            <li key={s.sourceId}>
              <ListRow
                asChild
                size="sm"
                label={s.title || s.url}
                // Capped: an unparseable URL comes back whole and would
                // otherwise squeeze the title out of the row.
                meta={
                  <span className="block max-w-40 truncate">
                    {hostname(s.url)}
                  </span>
                }
              >
                <a
                  id={`source-${s.sourceId}`}
                  href={s.url}
                  target="_blank"
                  rel="noreferrer"
                >
                  <span className="shrink-0 text-[12px] font-medium text-amber-500 tabular-nums">
                    {s.sourceId}
                  </span>
                </a>
              </ListRow>
            </li>
          ))}
        </ul>
      </CollapsibleContent>
    </Collapsible>
  );
}

function hostname(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return url;
  }
}
