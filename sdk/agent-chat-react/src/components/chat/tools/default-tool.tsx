// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Generic collapsible tool call renderer — used for tools without
// a dedicated renderer.

import { useState } from "react";
import { ChevronRightIcon } from "lucide-react";
import { cn } from "../../../lib/utils";
import { formatArgs, truncate } from "./shared";
import type { ToolCallInfo } from "../../../types";

export function DefaultToolBlock({ tc }: { tc: ToolCallInfo }) {
  const [expanded, setExpanded] = useState(false);

  return (
    <div>
      <button
        type="button"
        onClick={() => setExpanded(!expanded)}
        className={cn(
          "flex w-full items-center gap-1.5 rounded-md px-2 py-1",
          "text-sm text-muted-foreground hover:bg-muted/50 transition-colors",
        )}
      >
        <ChevronRightIcon
          className={cn(
            "size-3 shrink-0 transition-transform duration-150",
            expanded && "rotate-90",
          )}
        />
        <span className="font-medium text-foreground/80">{tc.toolName}</span>
      </button>

      {expanded && (
        <div className="ml-6 mt-0.5 space-y-1 text-sm ">
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
