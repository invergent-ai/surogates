// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Compact one-liner renderers for lightweight tools:
// - Session search
// - Web search / crawl
// - Vision analyze

import { SearchIcon, EyeIcon } from "lucide-react";
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
    if (tc.toolName === "web_search") {
      displayText = args.query ?? "";
    } else if (tc.toolName === "web_crawl") {
      displayText = args.url ?? "";
    }
  } catch { /* ignore */ }

  const toolLabel = {
    web_search: "Web Search",
    web_crawl: "Web Crawl",
  }[tc.toolName] ?? tc.toolName;

  return (
    <div className="flex items-center gap-2 text-sm py-0.5">
      <span className="font-medium text-foreground">{toolLabel}</span>
      {displayText && (
        <span className="text-muted-foreground/70 truncate text-xs">
          {displayText}
        </span>
      )}
    </div>
  );
}

// ── MCP tools ───────────────────────────────────────────────────────

export function MCPToolBlock({ tc }: { tc: ToolCallInfo }) {
  // Tool names are emitted as `mcp__{server}__{tool}`. Split on the
  // double-underscore separator; the last segment is the tool name.
  const segments = tc.toolName.replace(/^mcp__/, "").split("__");
  const toolName = segments[segments.length - 1] ?? tc.toolName;
  const label = toolName
    .split("_")
    .map((p) => p.charAt(0).toUpperCase() + p.slice(1))
    .join(" ");

  let summary = "";
  try {
    const args = JSON.parse(tc.args);
    if (args && typeof args === "object") {
      const entries = Object.entries(args).filter(([, v]) => v !== undefined && v !== null && v !== "");
      if (entries.length > 0) {
        summary = entries
          .map(([k, v]) => {
            const val = typeof v === "string" ? v : JSON.stringify(v);
            return `${k}=${val}`;
          })
          .join(", ");
      }
    }
  } catch { /* ignore */ }

  return (
    <div className="flex items-center gap-2 text-sm py-0.5">
      <span className="font-medium text-foreground">{label}</span>
      {summary && (
        <span className="text-muted-foreground/60 truncate text-xs">
          {summary}
        </span>
      )}
    </div>
  );
}

// ── Vision analyze ──────────────────────────────────────────────────

export function VisionAnalyzeBlock({ tc }: { tc: ToolCallInfo }) {
  let image = "";
  try {
    const args = JSON.parse(tc.args);
    const ref = String(args.image ?? args.image_url ?? args.image_path ?? "");
    image = ref.split("/").pop() ?? ref;
  } catch { /* ignore */ }

  return (
    <div className="flex items-center gap-2 text-sm py-0.5">
      <EyeIcon className="size-3.5 text-muted-foreground/60 shrink-0" />
      <span className="font-medium text-foreground">Vision Analyze</span>
      {image && (
        <span className="text-muted-foreground/70 truncate text-xs">
          {image}
        </span>
      )}
    </div>
  );
}
