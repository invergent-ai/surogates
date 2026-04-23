// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Minimal renderer for the ``create_artifact`` tool call.  The actual
// artifact content is rendered below by ``ArtifactBlock`` from the
// ``artifact.created`` event — so the tool call itself is just a
// compact status line, matching Claude's "creating …" artifact UX.

import type { ToolCallInfo } from "@/hooks/use-session-runtime";
import { parseArgs, effectiveStatus } from "./shared";

export function ArtifactToolBlock({ tc }: { tc: ToolCallInfo }) {
  const args = parseArgs<{ name?: string; kind?: string }>(tc.args) ?? {};
  const status = effectiveStatus(tc);

  const label =
    status === "running"
      ? "Creating artifact…"
      : status === "error"
      ? "Tried to create"
      : "Created";

  return (
    <div className="flex items-center gap-1.5 text-sm ">
      <span className="font-semibold text-foreground">{label}</span>
      {args.name && (
        <span className="text-muted-foreground truncate">{args.name}</span>
      )}
    </div>
  );
}
