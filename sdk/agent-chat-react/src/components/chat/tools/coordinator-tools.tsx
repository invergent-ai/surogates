// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Compact renderers for coordinator worker-management tools.

import { parseArgs } from "./shared";
import type { ToolCallInfo } from "../../../types";

interface CoordinatorArgs {
  goal?: string;
  context?: string;
  worker_id?: string;
  message?: string;
  reason?: string;
  agent_type?: string;
  model?: string;
}

interface CoordinatorResult {
  worker_id?: string;
  session_id?: string;
  status?: string;
  error?: string;
}

function firstLine(value: string | undefined) {
  if (!value) return "";
  const index = value.indexOf("\n");
  return (index === -1 ? value : value.slice(0, index)).trim();
}

export function CoordinatorToolBlock({ tc }: { tc: ToolCallInfo }) {
  const args = parseArgs<CoordinatorArgs>(tc.args) ?? {};
  const result = tc.result ? parseArgs<CoordinatorResult>(tc.result) : null;
  const failed = Boolean(result?.error);

  let label = "Worker";
  let target = "";
  let detail = "";

  if (tc.toolName === "spawn_worker") {
    label = tc.status === "running" ? "Spawning worker" : "Spawned worker";
    target = args.agent_type ?? args.model ?? "";
    detail = firstLine(args.goal);
  } else if (tc.toolName === "send_worker_message") {
    label = "Message worker";
    target = args.worker_id ?? "";
    detail = firstLine(args.message);
  } else if (tc.toolName === "stop_worker") {
    label = "Stop worker";
    target = args.worker_id ?? "";
    detail = firstLine(args.reason);
  }

  const resultId = result?.worker_id ?? result?.session_id;
  const summary = result?.error ?? resultId ?? result?.status;

  return (
    <div className="flex items-center gap-1.5 text-sm">
      <span className="font-semibold text-foreground">{label}</span>
      {target && <span className="text-muted-foreground truncate">{target}</span>}
      {detail && <span className="text-muted-foreground/70 truncate">· {detail}</span>}
      {summary && (
        <span className={failed ? "text-red-500 truncate" : "text-muted-foreground/70 truncate"}>
          → {summary}
        </span>
      )}
    </div>
  );
}
