// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Compact one-liner renderers for the read-only skill discovery tools:
// - skills_list: enumerate available skills (optionally filtered by category)
// - skill_view: load a skill's SKILL.md (or one of its linked files)

import type { ToolCallInfo } from "@/hooks/use-session-runtime";
import { parseArgs } from "./shared";

// ── skills_list ─────────────────────────────────────────────────────

interface SkillsListArgs {
  category?: string;
}

interface SkillsListResult {
  success?: boolean;
  count?: number;
  categories?: string[];
}

export function SkillsListBlock({ tc }: { tc: ToolCallInfo }) {
  const args = parseArgs<SkillsListArgs>(tc.args) ?? {};
  const filter = args.category ? `category: ${args.category}` : "all";

  let summary = "";
  if (tc.result) {
    const parsed = parseArgs<SkillsListResult>(tc.result);
    if (parsed?.count !== undefined) {
      summary = `${parsed.count} skill${parsed.count === 1 ? "" : "s"}`;
    }
  }

  return (
    <div className="flex items-center gap-1.5 text-sm font-mono">
      <span className="font-semibold text-foreground">Skills List</span>
      <span className="text-muted-foreground truncate">{filter}</span>
      {summary && (
        <span className="text-muted-foreground/70 ml-1">→ {summary}</span>
      )}
    </div>
  );
}

// ── skill_view ──────────────────────────────────────────────────────

interface SkillViewArgs {
  name?: string;
  file_path?: string;
}

interface SkillViewResult {
  success?: boolean;
  name?: string;
  staged_at?: string;
  token_estimate?: number;
}

export function SkillViewBlock({ tc }: { tc: ToolCallInfo }) {
  const args = parseArgs<SkillViewArgs>(tc.args) ?? {};
  const target = args.file_path
    ? `${args.name ?? "?"}/${args.file_path}`
    : (args.name ?? "?");

  let summary = "";
  if (tc.result) {
    const parsed = parseArgs<SkillViewResult>(tc.result);
    if (parsed?.staged_at) {
      summary = `staged at ${parsed.staged_at}`;
    } else if (parsed?.token_estimate) {
      summary = `${parsed.token_estimate} tokens`;
    }
  }

  return (
    <div className="flex items-center gap-1.5 text-sm font-mono">
      <span className="font-semibold text-foreground">Skill View</span>
      <span className="text-muted-foreground truncate">{target}</span>
      {summary && (
        <span className="text-muted-foreground/70 ml-1">→ {summary}</span>
      )}
    </div>
  );
}
