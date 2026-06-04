// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Renderers for the deep-research tools:
//
//   * ``research_outline``: a living outline card showing the section
//     list and a foldable view of the persisted markdown.
//   * ``research_memory``: a compact one-liner that summarises the
//     latest add/retrieve/list call.

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

// ── research_memory ─────────────────────────────────────────────────

interface MemoryArgs {
  action?: string;
  url?: string;
  query?: string;
}

interface MemoryResult {
  success?: boolean;
  source_id?: string;
  sources?: Array<{ source_id: string }>;
}

export function ResearchMemoryBlock({ tc }: { tc: ToolCallInfo }) {
  const args = parseArgs<MemoryArgs>(tc.args) ?? {};
  const result = tc.result
    ? parseArgs<MemoryResult>(tc.result) ?? {}
    : {};

  let label: string;
  if (args.action === "add") {
    const host = args.url ? hostname(args.url) : "";
    label = `Recorded source ${result.source_id ?? ""}${
      host ? ` · ${host}` : ""
    }`;
  } else if (args.action === "retrieve") {
    const n = result.sources?.length ?? 0;
    label = `Retrieved ${n} source${n === 1 ? "" : "s"}${
      args.query ? ` for "${truncate(args.query)}"` : ""
    }`;
  } else if (args.action === "list") {
    const n = result.sources?.length ?? 0;
    label = `Listed ${n} source${n === 1 ? "" : "s"}`;
  } else {
    label = "research_memory";
  }

  return (
    <div className="text-xs text-muted-foreground">
      <span className="font-semibold text-foreground">research</span> {label}
    </div>
  );
}

// ── helpers ─────────────────────────────────────────────────────────

function hostname(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return url;
  }
}

function truncate(s: string, n = 40): string {
  return s.length > n ? `${s.slice(0, n)}…` : s;
}
