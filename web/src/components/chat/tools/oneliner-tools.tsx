// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Compact one-liner renderers for lightweight tools:
// - Session search
// - Web fetch / search / crawl

import type { ToolCallInfo } from "@/hooks/use-session-runtime";

// ── Session search ──────────────────────────────────────────────────

export function SessionSearchBlock({ tc }: { tc: ToolCallInfo }) {
  let query = "";
  try {
    const args = JSON.parse(tc.args);
    query = args.query ?? "";
  } catch { /* ignore */ }

  return (
    <div className="flex items-center gap-1.5 text-sm font-mono">
      <span className="font-semibold text-foreground">Session Search</span>
      <span className="text-muted-foreground truncate">{query ? `"${query}"` : ""}</span>
    </div>
  );
}

// ── Web tools ───────────────────────────────────────────────────────

export function WebToolBlock({ tc }: { tc: ToolCallInfo }) {
  let displayText = "";
  try {
    const args = JSON.parse(tc.args);
    if (tc.toolName === "web_extract") {
      const urls: string[] = args.urls ?? [];
      displayText = urls[0] ?? "";
    } else if (tc.toolName === "web_search") {
      displayText = args.query ?? "";
    } else if (tc.toolName === "web_crawl") {
      displayText = args.url ?? "";
    }
  } catch { /* ignore */ }

  const toolLabel = {
    web_extract: "Web Fetch",
    web_search: "Web Search",
    web_crawl: "Web Crawl",
  }[tc.toolName] ?? tc.toolName;

  return (
    <div className="flex items-center gap-1.5 text-sm font-mono">
      <span className="font-semibold text-foreground">{toolLabel}</span>
      <span className="text-muted-foreground truncate">{displayText}</span>
    </div>
  );
}
