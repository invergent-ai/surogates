// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Renders a batched web_extract call: the full list of URLs being
// fetched, with per-URL status sourced from the tool result.

import { GlobeIcon, CheckIcon, XIcon } from "lucide-react";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "../../ui/tooltip";
import { cn } from "../../../lib/utils";
import type { ToolCallInfo } from "../../../types";

interface ResultEntry {
  url?: string;
  title?: string;
  content?: string;
  error?: string;
}

function parseUrls(rawArgs: string): string[] {
  try {
    const args = JSON.parse(rawArgs);
    if (Array.isArray(args.urls)) {
      return args.urls.filter((u: unknown): u is string => typeof u === "string");
    }
  } catch {
    /* ignore */
  }
  return [];
}

function parseResult(rawResult: string | undefined): {
  byUrl: Map<string, ResultEntry>;
  toolError: string | null;
} {
  const byUrl = new Map<string, ResultEntry>();
  if (!rawResult) return { byUrl, toolError: null };
  try {
    const parsed = JSON.parse(rawResult);
    const entries: ResultEntry[] = Array.isArray(parsed?.results)
      ? parsed.results
      : [];
    for (const entry of entries) {
      if (entry && typeof entry.url === "string") {
        byUrl.set(entry.url, entry);
      }
    }
    if (byUrl.size === 0 && typeof parsed?.error === "string") {
      return { byUrl, toolError: parsed.error };
    }
  } catch {
    /* ignore */
  }
  return { byUrl, toolError: null };
}

function domain(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return url;
  }
}

export function WebExtractToolBlock({ tc }: { tc: ToolCallInfo }) {
  const urls = parseUrls(tc.args);
  const { byUrl, toolError } = parseResult(tc.result);
  const isComplete = tc.status === "complete" || tc.status === "error";

  if (urls.length === 0) {
    return (
      <div className="flex items-center gap-2 text-sm py-0.5">
        <GlobeIcon className="size-3.5 text-muted-foreground/60 shrink-0" />
        <span className="font-medium text-foreground">Web Fetch</span>
      </div>
    );
  }

  return (
    <div>
      <div className="flex items-center gap-2 text-sm">
        <GlobeIcon className="size-3.5 text-muted-foreground/60 shrink-0" />
        <span className="font-medium text-foreground">Web Fetch</span>
        <span className="text-muted-foreground/70 text-xs">
          {urls.length} URL{urls.length !== 1 ? "s" : ""}
        </span>
      </div>
      <ul className="mt-1 ml-5 space-y-0.5">
        {urls.map((url, i) => {
          const entry = byUrl.get(url);
          const perUrlError = entry?.error;
          const failure = perUrlError ?? (isComplete && !entry ? toolError : null);
          const fetched = entry && !perUrlError;
          const pending = !isComplete && !entry;

          const row = (
            <div className="flex items-center gap-2 text-xs">
              <span
                className={cn(
                  "truncate",
                  pending ? "text-muted-foreground/50" : "text-muted-foreground",
                )}
              >
                {domain(url)}
              </span>
              {fetched && (
                <CheckIcon className="size-3 text-green-500/70 shrink-0" />
              )}
              {failure && (
                <XIcon className="size-3 text-red-500/80 shrink-0" />
              )}
            </div>
          );

          return (
            <li key={`${url}-${i}`}>
              {failure ? (
                <Tooltip>
                  <TooltipTrigger asChild>{row}</TooltipTrigger>
                  <TooltipContent side="top">{failure}</TooltipContent>
                </Tooltip>
              ) : (
                row
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}
