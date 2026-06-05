// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Compact one-liner renderers for lightweight tools:
// - Session search
// - Web search / crawl
// - Vision analyze
// - Research memory

import { BookOpenIcon, EyeIcon, SearchIcon } from "lucide-react";
import type { ToolCallInfo } from "../../../types";
import { parseArgs } from "./shared";

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

// ── Research memory ─────────────────────────────────────────────────
//
// One per ``research_memory`` call.  Three actions, one shape: bold
// "Research" label + muted action summary so a long deep-research
// turn (planner racks up dozens of add/retrieve calls in a row)
// stays scannable.  Failure path (``success: false``) falls back to
// a generic label so the row still reads.

interface ResearchMemoryArgs {
  action?: string;
  url?: string;
  query?: string;
}

interface ResearchMemoryResult {
  success?: boolean;
  source_id?: string;
  sources?: Array<{ source_id: string }>;
}

export function ResearchMemoryBlock({ tc }: { tc: ToolCallInfo }) {
  const args = parseArgs<ResearchMemoryArgs>(tc.args) ?? {};
  const result = tc.result
    ? parseArgs<ResearchMemoryResult>(tc.result) ?? {}
    : {};

  let detail = "";
  if (args.action === "add") {
    const host = args.url ? hostname(args.url) : "";
    const sid = result.source_id ?? "";
    detail = sid
      ? `recorded ${sid}${host ? ` · ${host}` : ""}`
      : host
        ? `recorded · ${host}`
        : "recorded";
  } else if (args.action === "retrieve") {
    const n = result.sources?.length ?? 0;
    detail = args.query
      ? `${n} source${n === 1 ? "" : "s"} for "${truncate(args.query, 40)}"`
      : `${n} source${n === 1 ? "" : "s"}`;
  } else if (args.action === "list") {
    const n = result.sources?.length ?? 0;
    detail = `${n} source${n === 1 ? "" : "s"}`;
  }

  return (
    <div className="flex items-center gap-2 text-sm py-0.5">
      <BookOpenIcon className="size-3.5 text-muted-foreground/60 shrink-0" />
      <span className="font-medium text-foreground">Research</span>
      {detail && (
        <span className="text-muted-foreground/70 truncate text-xs">
          {detail}
        </span>
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

function truncate(s: string, n: number): string {
  return s.length > n ? `${s.slice(0, n)}…` : s;
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
