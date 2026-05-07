// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Generic collapsible tool call renderer — used for tools without
// a dedicated renderer.

import { useState } from "react";
import { ChevronRightIcon, CheckCircle2Icon, Loader2Icon, XCircleIcon, WrenchIcon } from "lucide-react";
import { cn } from "../../../lib/utils";
import { formatArgs, truncate, effectiveStatus } from "./shared";
import type { ToolCallInfo } from "../../../types";

const TOOL_LABELS: Record<string, string> = {
  kb_list_pages: "Knowledge Base",
  kb_read_page: "Read KB Page",
  consult_expert: "Expert",
  delegate_task: "Delegate",
  memory: "Memory",
  skills_list: "Skills",
  skill_view: "Skill",
  create_artifact: "Artifact",
  process: "Process",
};

function toolLabel(name: string): string {
  return TOOL_LABELS[name] ?? name.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

function toolSummary(tc: ToolCallInfo): string {
  try {
    const args = JSON.parse(tc.args);
    if (tc.toolName === "kb_list_pages") return args.kb_id ? `Listing pages` : "";
    if (tc.toolName === "kb_read_page") return args.path ?? "";
    if (tc.toolName === "consult_expert") return args.question?.slice(0, 60) ?? "";
    if (tc.toolName === "memory") return args.action ?? "";
    return "";
  } catch {
    return "";
  }
}

function StatusIcon({ tc }: { tc: ToolCallInfo }) {
  const status = effectiveStatus(tc);
  if (status === "running") return <Loader2Icon className="size-3.5 animate-spin text-primary" />;
  if (status === "error") return <XCircleIcon className="size-3.5 text-red-500" />;
  if (status === "cancelled") return <XCircleIcon className="size-3.5 text-muted-foreground/40" />;
  return <CheckCircle2Icon className="size-3.5 text-emerald-500" />;
}

export function DefaultToolBlock({ tc }: { tc: ToolCallInfo }) {
  const [expanded, setExpanded] = useState(false);
  const summary = toolSummary(tc);

  return (
    <div className="rounded-lg border border-border/60 bg-muted/20 overflow-hidden">
      <button
        type="button"
        onClick={() => setExpanded(!expanded)}
        className={cn(
          "flex w-full items-center gap-2 px-3 py-2",
          "text-sm hover:bg-muted/40 transition-colors cursor-pointer",
        )}
      >
        <StatusIcon tc={tc} />
        <span className="font-medium text-foreground">{toolLabel(tc.toolName)}</span>
        {summary && (
          <span className="text-muted-foreground/70 truncate text-xs flex-1 text-left">
            {summary}
          </span>
        )}
        <ChevronRightIcon
          className={cn(
            "size-3.5 shrink-0 text-muted-foreground/50 transition-transform duration-150 ml-auto",
            expanded && "rotate-90",
          )}
        />
      </button>

      {expanded && (
        <div className="border-t border-border/40 px-3 py-2 space-y-2">
          <pre className="overflow-x-auto rounded-md bg-muted/50 px-3 py-2 text-xs text-muted-foreground font-mono whitespace-pre-wrap break-all">
            {formatArgs(tc.args)}
          </pre>
          {tc.result && (
            <pre className="overflow-x-auto rounded-md bg-muted/50 px-3 py-2 text-xs font-mono whitespace-pre-wrap break-all max-h-48 overflow-y-auto">
              <span className="text-emerald-600 dark:text-emerald-400 font-semibold">
                Result
              </span>
              {"\n"}
              <span className="text-muted-foreground">{truncate(tc.result, 2000)}</span>
            </pre>
          )}
        </div>
      )}
    </div>
  );
}
