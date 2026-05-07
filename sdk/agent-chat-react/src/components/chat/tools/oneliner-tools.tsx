// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Compact one-liner renderers for lightweight tools:
// - Session search
// - Web fetch / search / crawl

import { SearchIcon, GlobeIcon } from "lucide-react";
import type { ToolCallInfo } from "../../../types";

// ── Session search ──────────────────────────────────────────────────

export function SessionSearchBlock({ tc }: { tc: ToolCallInfo }) {
  let query = "";
  try {
    const args = JSON.parse(tc.args);
    query = args.query ?? "";
  } catch { /* ignore */ }

  return (
    <div className="flex items-center gap-2 text-sm py-0.5">
      <SearchIcon className="size-3.5 text-muted-foreground/60 shrink-0" />
      <span className="font-medium text-foreground">Session Search</span>
      {query && (
        <span className="text-muted-foreground/70 truncate text-xs italic">
          &ldquo;{query}&rdquo;
        </span>
      )}
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
    <div className="flex items-center gap-2 text-sm py-0.5">
      <GlobeIcon className="size-3.5 text-muted-foreground/60 shrink-0" />
      <span className="font-medium text-foreground">{toolLabel}</span>
      {displayText && (
        <span className="text-muted-foreground/70 truncate text-xs">
          {displayText}
        </span>
      )}
    </div>
  );
}
