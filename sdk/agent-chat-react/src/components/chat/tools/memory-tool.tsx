// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Memory tool widget -- compact one-liner that summarises the action
// (add / replace / remove) against the chosen store (memory / user
// profile).  Expands to show the actual entry content (and the old
// text for replace / remove).

import { useState } from "react";
import { Collapsible, CollapsibleContent } from "@invergent/ui-sdk/collapsible";
import { CollapsibleHeader } from "@invergent/ui-sdk/collapsible-header";
import { cn } from "../../../lib/utils";
import { parseArgs } from "./shared";
import type { ToolCallInfo } from "../../../types";

interface MemoryArgs {
  action?: "add" | "replace" | "remove";
  target?: "memory" | "user";
  content?: string;
  old_text?: string;
}

interface MemoryResult {
  success?: boolean;
  message?: string;
  error?: string | null;
  entries?: string[];
  usage?: string;
  entry_count?: number;
}

const ACTION_VERB: Record<string, string> = {
  add: "Saved",
  replace: "Updated",
  remove: "Removed",
};

const ACTION_PREP: Record<string, string> = {
  add: "to",
  replace: "in",
  remove: "from",
};

const TARGET_LABEL: Record<string, string> = {
  memory: "memory",
  user: "user profile",
};

export function MemoryToolBlock({ tc }: { tc: ToolCallInfo }) {
  const [isOpen, setIsOpen] = useState(false);
  const args = parseArgs<MemoryArgs>(tc.args) ?? {};
  const result = tc.result ? parseArgs<MemoryResult>(tc.result) : null;

  const action = args.action ?? "add";
  const target = args.target ?? "memory";
  const verb = ACTION_VERB[action] ?? action;
  const prep = ACTION_PREP[action] ?? "to";
  const targetLabel = TARGET_LABEL[target] ?? target;
  const failed = result?.success === false || !!result?.error;

  return (
    <Collapsible open={isOpen} onOpenChange={setIsOpen} className="not-prose w-full">
      <CollapsibleHeader className="-ml-2 w-fit max-w-full">
        <span>
          <span className="font-medium text-foreground">{verb}</span>{" "}
          <span className="text-muted-foreground">
            {prep} {targetLabel}
          </span>
          {result?.usage && !failed && (
            <span className="text-muted-foreground/60 ml-1.5 text-xs">
              {result.usage}
            </span>
          )}
          {failed && (
            <span className="text-red-500 ml-1.5">· failed</span>
          )}
        </span>
      </CollapsibleHeader>
      <CollapsibleContent
        className={cn(
          "mt-2 overflow-hidden",
          "data-[state=closed]:fade-out-0 data-[state=closed]:slide-out-to-top-2 data-[state=open]:slide-in-from-top-2 outline-none data-[state=closed]:animate-out data-[state=open]:animate-in",
        )}
      >
        <div className="space-y-1.5">
          {action === "add" && args.content && (
            <MemoryEntry content={args.content} variant="added" />
          )}
          {action === "replace" && (
            <>
              {args.old_text && (
                <MemoryEntry content={args.old_text} label="Old" variant="removed" />
              )}
              {args.content && (
                <MemoryEntry content={args.content} label="New" variant="added" />
              )}
            </>
          )}
          {action === "remove" && args.old_text && (
            <MemoryEntry content={args.old_text} variant="removed" />
          )}
          {/* Older harnesses stored a 422's pydantic error list here. */}
          {result?.error && (
            <div className="rounded bg-red-500/10 px-2 py-1 text-xs text-red-500">
              {typeof result.error === "string"
                ? result.error
                : JSON.stringify(result.error)}
            </div>
          )}
        </div>
      </CollapsibleContent>
    </Collapsible>
  );
}

function MemoryEntry({
  content,
  label,
  variant = "added",
}: {
  content: string;
  label?: string;
  variant?: "added" | "removed";
}) {
  return (
    <div
      className={cn(
        "rounded-md border-l-2 px-3 py-2 text-sm",
        variant === "added"
          ? "border-emerald-500/50 bg-emerald-500/5"
          : "border-red-500/50 bg-red-500/5",
      )}
    >
      {label && (
        <div className="mb-1 text-[10px] uppercase tracking-wide text-muted-foreground/70">
          {label}
        </div>
      )}
      <div className="whitespace-pre-wrap wrap-break-word text-foreground/90">
        {content}
      </div>
    </div>
  );
}
