// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Renders a run of consecutive web_search calls as one grouped card,
// with each query on its own row plus an optional result count.

import { SearchIcon } from "lucide-react";
import type { ToolCallInfo } from "../../../types";

function parseQuery(rawArgs: string): string {
  try {
    const args = JSON.parse(rawArgs);
    return typeof args.query === "string" ? args.query : "";
  } catch {
    return "";
  }
}

function parseResultCount(rawResult: string | undefined): number | null {
  if (!rawResult) return null;
  try {
    const parsed = JSON.parse(rawResult);
    if (Array.isArray(parsed?.data?.web)) return parsed.data.web.length;
    if (Array.isArray(parsed?.results)) return parsed.results.length;
  } catch {
    /* ignore */
  }
  return null;
}

function WebSearchRow({ tc }: { tc: ToolCallInfo }) {
  const query = parseQuery(tc.args);
  const count = parseResultCount(tc.result);
  const showCount = count !== null && tc.status === "complete";

  return (
    <div className="flex items-center gap-2 text-xs min-w-0">
      <span className="text-muted-foreground truncate">{query || "—"}</span>
      {showCount && (
        <span className="text-muted-foreground/50 shrink-0">
          {count} result{count !== 1 ? "s" : ""}
        </span>
      )}
    </div>
  );
}

export function WebSearchGroupBlock({ tcs }: { tcs: ToolCallInfo[] }) {
  return (
    <div>
      <div className="flex items-center gap-2 text-sm">
        <SearchIcon className="size-3.5 text-muted-foreground/60 shrink-0" />
        <span className="font-medium text-foreground">Web Search</span>
        <span className="text-muted-foreground/70 text-xs">
          {tcs.length} {tcs.length === 1 ? "query" : "queries"}
        </span>
      </div>
      <ul className="mt-1 ml-5 space-y-0.5">
        {tcs.map((tc) => (
          <li key={tc.id}>
            <WebSearchRow tc={tc} />
          </li>
        ))}
      </ul>
    </div>
  );
}
