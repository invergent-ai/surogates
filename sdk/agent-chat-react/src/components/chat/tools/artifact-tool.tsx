// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Minimal renderer for the ``create_artifact`` tool call.  The actual
// artifact content is rendered below by ``ArtifactBlock`` from the
// ``artifact.created`` event — so the tool call itself is just a
// compact status line, matching Claude's "creating …" artifact UX.
//
// Failures here are almost always transient: the LLM occasionally
// emits a malformed call (flat shape, stringified spec, missing
// fields) and immediately retries with the right shape.  Surfacing
// the failed attempt as a red "Tried to create" + raw error text
// confuses users who only care about the final rendered chart, and
// nudges the model into post-hoc "fix" loops.  We collapse the
// failed attempt into the same "still working" status as the running
// state instead.

import type { ToolCallInfo } from "../../../types";
import { parseArgs, effectiveStatus } from "./shared";

export function ArtifactToolBlock({
  tc,
  resolvedName,
}: {
  tc: ToolCallInfo;
  // Name resolved by the caller (``args.name`` or the matching
  // ``artifact.created`` fallback). When omitted, the component parses
  // ``tc.args`` inline so it remains self-contained for callers that
  // don't pre-resolve.
  resolvedName?: string;
}) {
  const status = effectiveStatus(tc);

  const label =
    status === "running"
      ? "Creating artifact…"
      : status === "error"
      ? "Creating artifact…"
      : "Created";

  const name = resolvedName
    ?? parseArgs<{ name?: string }>(tc.args)?.name;

  return (
    <div className="flex items-center gap-1.5 text-sm ">
      <span className="font-semibold text-foreground">{label}</span>
      {name && (
        <span className="text-muted-foreground truncate">{name}</span>
      )}
    </div>
  );
}
