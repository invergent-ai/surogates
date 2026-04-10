// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
import { useState } from "react";
import {
  CheckCircle2Icon,
  ChevronRightIcon,
  Loader2Icon,
  AlertCircleIcon,
} from "lucide-react";
import { cn } from "@/lib/utils";
import type { ToolCallInfo } from "@/hooks/use-session-runtime";

const STATUS_ICON = {
  running: <Loader2Icon className="size-3.5 animate-spin text-primary" />,
  complete: <CheckCircle2Icon className="size-3.5 text-emerald-500" />,
  error: <AlertCircleIcon className="size-3.5 text-destructive" />,
} as const;

export function ToolCallBlock({ tc }: { tc: ToolCallInfo }) {
  const [expanded, setExpanded] = useState(false);

  return (
    <div className="my-1">
      <button
        type="button"
        onClick={() => setExpanded(!expanded)}
        className={cn(
          "flex w-full items-center gap-1.5 rounded-md px-2 py-1",
          "text-xs text-muted-foreground hover:bg-muted/50 transition-colors",
          "font-mono",
        )}
      >
        <ChevronRightIcon
          className={cn(
            "size-3 shrink-0 transition-transform duration-150",
            expanded && "rotate-90",
          )}
        />
        {STATUS_ICON[tc.status]}
        <span className="font-medium text-foreground/80">{tc.toolName}</span>
      </button>

      {expanded && (
        <div className="ml-6 mt-0.5 space-y-1 text-xs font-mono">
          <pre className="overflow-x-auto rounded bg-muted/40 px-2 py-1 text-muted-foreground whitespace-pre-wrap break-all">
            {formatArgs(tc.args)}
          </pre>
          {tc.result && (
            <pre className="overflow-x-auto rounded bg-muted/40 px-2 py-1 text-muted-foreground whitespace-pre-wrap break-all max-h-64 overflow-y-auto">
              <span className="text-emerald-600 dark:text-emerald-400">
                Result:
              </span>
              {"\n"}
              {truncate(tc.result, 2000)}
            </pre>
          )}
        </div>
      )}
    </div>
  );
}

function formatArgs(args: string): string {
  try {
    return JSON.stringify(JSON.parse(args), null, 2);
  } catch {
    return args;
  }
}

function truncate(s: string, max: number): string {
  return s.length > max ? s.slice(0, max) + "\n... (truncated)" : s;
}
