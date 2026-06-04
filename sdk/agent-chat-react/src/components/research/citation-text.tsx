// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Renders inline [S#] citation markers as clickable chips that resolve
// against the collected research sources.

import type { AgentChatResearchSource } from "../../types";

export type CitationSegment =
  | { kind: "text"; value: string }
  | { kind: "cite"; value: string };

// ``S\d+`` markers, optionally comma-grouped like ``[S2, S3]``.  Bare
// brackets without an S-prefix (e.g. an array index ``[3]``) and
// non-numeric IDs (e.g. ``[Sx]``) intentionally do not match so the
// raw text is preserved for the writer to clean up.
const CITATION_RE = /\[(S\d+(?:\s*,\s*S\d+)*)\]/g;

/**
 * Split *text* into plain-text and citation segments.
 *
 * Comma-grouped markers expand into one ``cite`` segment per ID.
 * Empty input returns an empty array.  Invalid markers are passed
 * through verbatim as ``text``.
 */
export function splitCitations(text: string): CitationSegment[] {
  if (!text) return [];
  const segments: CitationSegment[] = [];
  let lastIndex = 0;
  for (const match of text.matchAll(CITATION_RE)) {
    const start = match.index ?? 0;
    if (start > lastIndex) {
      segments.push({ kind: "text", value: text.slice(lastIndex, start) });
    }
    for (const id of match[1]!.split(",")) {
      segments.push({ kind: "cite", value: id.trim() });
    }
    lastIndex = start + match[0].length;
  }
  if (lastIndex < text.length) {
    segments.push({ kind: "text", value: text.slice(lastIndex) });
  }
  return segments;
}

/**
 * Render *text* with each ``[S#]`` marker as a chip that links to the
 * matching :class:`AgentChatResearchSource`.  Hosts pass
 * ``onCitationClick`` to scroll the sources panel to the entry.
 */
export function CitationText({
  text,
  sources,
  onCitationClick,
}: {
  text: string;
  sources: AgentChatResearchSource[];
  onCitationClick?: (sourceId: string) => void;
}) {
  const byId = new Map(sources.map((s) => [s.sourceId, s]));
  return (
    <>
      {splitCitations(text).map((seg, i) => {
        if (seg.kind === "text") {
          return <span key={i}>{seg.value}</span>;
        }
        const src = byId.get(seg.value);
        return (
          <button
            key={i}
            type="button"
            title={src ? `${src.title} — ${src.url}` : seg.value}
            onClick={() => onCitationClick?.(seg.value)}
            className="mx-0.5 inline-flex items-center rounded-sm bg-muted px-1 text-[10px] font-semibold text-primary hover:bg-primary/10"
          >
            {seg.value}
          </button>
        );
      })}
    </>
  );
}
