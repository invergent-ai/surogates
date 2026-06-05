// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Renderer for the deep-research ``research_outline`` tool: a living
// outline card showing the section list and a foldable view of the
// persisted markdown.
//
// ``research_memory`` is rendered as a compact one-liner alongside
// the other lightweight tools -- see
// ``./oneliner-tools.tsx::ResearchMemoryBlock``.

import type { ToolCallInfo } from "../../../types";
import { parseArgs } from "./shared";

// ── research_outline ────────────────────────────────────────────────

interface OutlineArgs {
  action?: string;
  outline?: string;
}

interface OutlineResult {
  success?: boolean;
  outline?: string;
  sections?: string[];
}

export function ResearchOutlineBlock({ tc }: { tc: ToolCallInfo }) {
  const args = parseArgs<OutlineArgs>(tc.args) ?? {};
  const result = tc.result
    ? parseArgs<OutlineResult>(tc.result) ?? {}
    : {};
  const outline =
    args.action === "set"
      ? args.outline ?? ""
      : result.outline ?? "";
  const sections = result.sections ?? [];

  return (
    <div className="rounded-sm border border-border bg-muted/40 p-2 text-xs">
      <div className="mb-1 font-semibold uppercase tracking-widest text-muted-foreground">
        Research outline
        {sections.length ? ` · ${sections.length} sections` : ""}
      </div>
      {outline ? (
        <pre className="max-h-48 overflow-auto whitespace-pre-wrap font-mono text-[11px] leading-snug">
          {outline}
        </pre>
      ) : (
        <span className="text-muted-foreground">updated</span>
      )}
    </div>
  );
}
