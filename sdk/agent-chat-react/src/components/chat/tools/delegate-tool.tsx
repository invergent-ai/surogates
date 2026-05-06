// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Renderer for the `delegate_task` tool — sub-agent delegation.
// Header shows the goal as the primary signal; expanding reveals the
// full goal, context, agent_type/model overrides, and the child
// session's final response.

import { useState } from "react";
import { ChevronRightIcon, GitBranchIcon } from "lucide-react";
import { cn } from "../../../lib/utils";
import { parseArgs, truncate } from "./shared";
import type { ToolCallInfo } from "../../../types";

interface DelegateArgs {
  goal?: string;
  context?: string;
  model?: string;
  agent_type?: string;
}

function firstLine(s: string): string {
  const idx = s.indexOf("\n");
  return idx === -1 ? s : s.slice(0, idx);
}

export function DelegateToolBlock({ tc }: { tc: ToolCallInfo }) {
  const [expanded, setExpanded] = useState(false);
  const args = parseArgs<DelegateArgs>(tc.args);

  const goal = args?.goal ?? "";
  const context = args?.context ?? "";
  const agentType = args?.agent_type;
  const model = args?.model;

  const summary = firstLine(goal).trim();

  // The result is usually plain text from the child's final response,
  // but errors come back as `{"error": "..."}`.
  let resultError: string | null = null;
  if (tc.result) {
    try {
      const parsed = JSON.parse(tc.result);
      if (parsed?.error) resultError = String(parsed.error);
    } catch {
      /* not JSON — treat as plain response text */
    }
  }

  return (
    <div>
      <button
        type="button"
        onClick={() => setExpanded(!expanded)}
        className={cn(
          "flex w-full items-center gap-1.5 rounded-md px-2 py-1",
          "text-sm text-muted-foreground hover:bg-muted/50 transition-colors"
        )}
      >
        <ChevronRightIcon
          className={cn(
            "size-3 shrink-0 transition-transform duration-150",
            expanded && "rotate-90",
          )}
        />
        <GitBranchIcon className="size-3.5 shrink-0 text-muted-foreground" />
        <span className="font-medium text-foreground/80">delegate_task</span>
        {agentType && (
          <span className="text-muted-foreground">· {agentType}</span>
        )}
        {summary && (
          <span className="text-muted-foreground truncate min-w-0">
            · {summary}
          </span>
        )}
      </button>

      {expanded && (
        <div className="ml-6 mt-0.5 space-y-1.5 text-sm font-mono">
          {(agentType || model) && (
            <div className="flex flex-wrap gap-x-3 gap-y-0.5 text-xs text-muted-foreground">
              {agentType && (
                <span>
                  <span className="text-muted-foreground/70">agent:</span>{" "}
                  {agentType}
                </span>
              )}
              {model && (
                <span>
                  <span className="text-muted-foreground/70">model:</span>{" "}
                  {model}
                </span>
              )}
            </div>
          )}

          {goal && (
            <div className="rounded bg-muted/40 px-2 py-1">
              <div className="text-[10px] uppercase tracking-wide text-muted-foreground/70">
                Goal
              </div>
              <pre className="mt-0.5 whitespace-pre-wrap break-words text-foreground/90">
                {goal}
              </pre>
            </div>
          )}

          {context && (
            <div className="rounded bg-muted/40 px-2 py-1">
              <div className="text-[10px] uppercase tracking-wide text-muted-foreground/70">
                Context
              </div>
              <pre className="mt-0.5 whitespace-pre-wrap break-words text-muted-foreground">
                {truncate(context, 2000)}
              </pre>
            </div>
          )}

          {tc.status === "running" && !tc.result && (
            <div className="text-xs text-muted-foreground italic">
              Sub-agent running…
            </div>
          )}

          {tc.result && (
            <div
              className={cn(
                "rounded px-2 py-1 max-h-64 overflow-y-auto",
                resultError
                  ? "bg-destructive/10"
                  : "bg-muted/40",
              )}
            >
              <div
                className={cn(
                  "text-[10px] uppercase tracking-wide",
                  resultError
                    ? "text-destructive/80"
                    : "text-emerald-600 dark:text-emerald-400",
                )}
              >
                {resultError ? "Error" : "Sub-agent response"}
              </div>
              <pre className="mt-0.5 whitespace-pre-wrap break-words text-foreground/90">
                {truncate(resultError ?? tc.result, 4000)}
              </pre>
            </div>
          )}
        </div>
      )}
    </div>
  );
}