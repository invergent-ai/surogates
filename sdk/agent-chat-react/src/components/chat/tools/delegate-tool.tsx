// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Compact one-liner renderer for ``delegate_task``.  The thread keeps
// every tool call as a single row; the child session's prose lands as
// a regular assistant message later in the timeline, and the parent's
// session-tree pane is the right place to drill into the child.  So
// the chip here is purely a status marker: who was delegated to, what
// the goal was, and whether the call errored.

import { GitBranchIcon } from "lucide-react";
import { parseArgs } from "./shared";
import { cn } from "../../../lib/utils";
import type { ToolCallInfo } from "../../../types";

interface DelegateArgs {
  goal?: string;
  agent_type?: string;
}

function firstLine(s: string): string {
  const idx = s.indexOf("\n");
  return idx === -1 ? s : s.slice(0, idx);
}

export function DelegateToolBlock({ tc }: { tc: ToolCallInfo }) {
  const args = parseArgs<DelegateArgs>(tc.args);
  const goal = firstLine(args?.goal ?? "").trim();
  const agentType = args?.agent_type;

  // Surface a JSON ``{"error": ...}`` envelope as a short trailing
  // tag.  Successful results are intentionally not echoed -- the
  // child's final assistant message renders downstream in the same
  // thread, and duplicating it here doubles the noise.
  let resultError: string | null = null;
  if (tc.result) {
    try {
      const parsed = JSON.parse(tc.result);
      if (parsed?.error) resultError = String(parsed.error);
    } catch {
      /* not JSON: plain prose response */
    }
  }

  return (
    <div className="flex items-center gap-2 text-sm py-0.5">
      <GitBranchIcon className="size-3.5 text-muted-foreground/60 shrink-0" />
      <span className="font-medium text-foreground">Delegate</span>
      {agentType && (
        <span className="text-muted-foreground/70 text-xs">
          · {agentType}
        </span>
      )}
      {goal && (
        <span className="text-muted-foreground/70 truncate text-xs italic">
          · &ldquo;{goal}&rdquo;
        </span>
      )}
      {resultError && (
        <span
          className={cn(
            "shrink-0 text-xs",
            "text-destructive",
          )}
          title={resultError}
        >
          · failed
        </span>
      )}
    </div>
  );
}
