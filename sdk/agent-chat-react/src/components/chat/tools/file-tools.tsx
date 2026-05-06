// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Renderers for file operation tools:
// - read_file: one-liner with file name
// - write_file: one-liner with file name
// - patch: diff view showing old_string → new_string
// - search_files: one-liner with pattern + result count
// - list_files: one-liner with path + result count

import { useEffect, useRef, useState } from "react";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "../../ui/tooltip";
import { Button } from "../../ui/button";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "../../ui/dialog";
import { ScrollArea } from "../../ui/scroll-area";
import { cn } from "../../../lib/utils";
import type { ToolCallInfo } from "../../../types";
import { DiffViewer } from "../diff-viewer";

// ── Shared path helpers ─────────────────────────────────────────────

function displayPath(filePath: string): string {
  if (filePath.startsWith("__WORKSPACE__")) {
    return filePath.replace(/^__WORKSPACE__\/?/, "");
  }
  if (filePath.startsWith("/")) {
    const parts = filePath.split("/").filter(Boolean);
    const uuidRe = /^[0-9a-f]{8}-/;
    const skip = new Set(["home", "tmp", "work", "var", "opt", "data", "surogates", "workspaces"]);
    for (let i = 0; i < parts.length - 1; i++) {
      if (!skip.has(parts[i]) && !uuidRe.test(parts[i])) {
        return parts.slice(i).join("/");
      }
    }
    return parts.slice(-2).join("/");
  }
  return filePath;
}

function fileName(path: string): string {
  return displayPath(path).split("/").pop() || path;
}

function FileNameWithTooltip({
  filePath,
  onFileSelect,
}: {
  filePath: string;
  onFileSelect?: (path: string) => void;
}) {
  const display = displayPath(filePath);
  const name = fileName(filePath);

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        {onFileSelect ? (
          <button
            type="button"
            onClick={() => onFileSelect(filePath)}
            className="text-primary hover:underline truncate text-left cursor-pointer underline"
          >
            {name}
          </button>
        ) : (
          <span className="text-muted-foreground truncate">{name}</span>
        )}
      </TooltipTrigger>
      <TooltipContent side="top">{display}</TooltipContent>
    </Tooltip>
  );
}

// ── Result count helper ─────────────────────────────────────────────

function parseResultCount(result: string | undefined): number | null {
  if (!result) return null;
  try {
    const parsed = JSON.parse(result);
    // search_files returns {matches: [...]} or {results: [...]}
    if (Array.isArray(parsed?.matches)) return parsed.matches.length;
    if (Array.isArray(parsed?.results)) return parsed.results.length;
    // list_files returns {entries: [...]} or an array directly
    if (Array.isArray(parsed?.entries)) return parsed.entries.length;
    if (Array.isArray(parsed)) return parsed.length;
    // Count lines in output string
    if (typeof parsed?.output === "string" && parsed.output.trim()) {
      return parsed.output.trim().split("\n").length;
    }
  } catch { /* ignore */ }
  return null;
}

// ── Read file ───────────────────────────────────────────────────────

export function ReadFileBlock({ tc, onFileSelect }: { tc: ToolCallInfo; onFileSelect?: (path: string) => void }) {
  let filePath = "";
  let detail = "";
  try {
    const args = JSON.parse(tc.args);
    filePath = args.path ?? args.file_path ?? "";
    const parts: string[] = [];
    if (args.offset != null || args.limit != null) {
      const start = (args.offset ?? 0) + 1;
      const end = args.limit ? start + args.limit - 1 : undefined;
      parts.push(end ? `lines ${start}-${end}` : `from line ${start}`);
    }
    if (parts.length) detail = `(${parts.join(", ")})`;
  } catch { /* ignore */ }

  return (
    <div className="flex items-center gap-1.5 text-sm ">
      <span className="font-semibold text-foreground">Read</span>
      <FileNameWithTooltip filePath={filePath} onFileSelect={onFileSelect} />
      {detail && <span className="text-muted-foreground ml-1">{detail}</span>}
    </div>
  );
}

// ── Write file ──────────────────────────────────────────────────────

export function WriteFileBlock({ tc, onFileSelect }: { tc: ToolCallInfo; onFileSelect?: (path: string) => void }) {
  let filePath = "";
  try {
    const args = JSON.parse(tc.args);
    filePath = args.path ?? args.file_path ?? "";
  } catch { /* ignore */ }

  return (
    <div className="flex items-center gap-1.5 text-sm ">
      <span className="font-semibold text-foreground">Write</span>
      <FileNameWithTooltip filePath={filePath} onFileSelect={onFileSelect} />
    </div>
  );
}

// ── Patch ───────────────────────────────────────────────────────────

const PATCH_COLLAPSED_HEIGHT = 192;

export function PatchBlock({ tc, onFileSelect }: { tc: ToolCallInfo; onFileSelect?: (path: string) => void }) {
  let filePath = "";
  let oldString = "";
  let newString = "";
  try {
    const args = JSON.parse(tc.args);
    filePath = args.path ?? args.file_path ?? "";
    oldString = args.old_string ?? "";
    newString = args.new_string ?? "";
  } catch { /* ignore */ }

  const contentRef = useRef<HTMLDivElement>(null);
  const [overflows, setOverflows] = useState(false);
  const [dialogOpen, setDialogOpen] = useState(false);

  useEffect(() => {
    if (contentRef.current) {
      setOverflows(contentRef.current.scrollHeight > PATCH_COLLAPSED_HEIGHT);
    }
  }, []); // eslint-disable-line react-hooks/exhaustive-deps -- measure DOM after mount

  const hasDiff = oldString || newString;
  const fName = fileName(filePath);

  return (
    <div className="space-y-1">
      <div className="flex items-center gap-1.5 text-sm ">
        <span className="font-semibold text-foreground">Patch</span>
        <FileNameWithTooltip filePath={filePath} onFileSelect={onFileSelect} />
      </div>
      {hasDiff && (
        <div className="group/patch relative">
          <div
            ref={contentRef}
            role={overflows ? "button" : undefined}
            tabIndex={overflows ? 0 : undefined}
            onClick={() => overflows && setDialogOpen(true)}
            onKeyDown={(e) => { if (e.key === "Enter" && overflows) setDialogOpen(true); }}
            className={cn(
              "overflow-hidden",
              overflows && "cursor-pointer",
            )}
            style={{ maxHeight: PATCH_COLLAPSED_HEIGHT }}
          >
            <DiffViewer
              oldValue={oldString}
              newValue={newString}
              fileName={fName}
              contextLines={3}
            />
          </div>
          {overflows && (
            <Button
              variant="outline"
              size="xs"
              onClick={() => setDialogOpen(true)}
              className="absolute bottom-1.5 right-2 opacity-0 group-hover/patch:opacity-100 transition-opacity backdrop-blur-sm"
            >
              Expand
            </Button>
          )}
        </div>
      )}

      <Dialog open={dialogOpen} onOpenChange={setDialogOpen}>
        <DialogContent className="sm:max-w-[50vw] w-full h-[70vh] flex flex-col p-0 gap-0 overflow-hidden">
          <DialogHeader className="px-4 py-3 border-b border-border shrink-0">
            <DialogTitle className="text-sm ">Patch {fName}</DialogTitle>
          </DialogHeader>
          <ScrollArea className="flex-1 min-h-0">
            <div className="p-4">
              <DiffViewer
                oldValue={oldString}
                newValue={newString}
                fileName={fName}
                contextLines={3}
              />
            </div>
          </ScrollArea>
        </DialogContent>
      </Dialog>
    </div>
  );
}

// ── Search files ────────────────────────────────────────────────────

export function SearchFilesBlock({ tc }: { tc: ToolCallInfo }) {
  let pattern = "";
  let path = "";
  try {
    const args = JSON.parse(tc.args);
    pattern = args.pattern ?? "";
    path = args.path ?? "";
  } catch { /* ignore */ }

  const count = parseResultCount(tc.result);

  return (
    <div>
      <div className="flex items-center gap-1.5 text-sm ">
        <span className="font-semibold text-foreground">Search</span>
        {pattern && <span className="text-muted-foreground truncate">&quot;{pattern}&quot;</span>}
        {path && path !== "." && (
          <span className="text-muted-foreground/60 truncate">in {displayPath(path)}</span>
        )}
      </div>
      {count !== null && tc.status === "complete" && (
        <span className={cn(
          "text-xs  shrink-0",
          count === 0 ? "text-muted-foreground/50" : "text-muted-foreground",
        )}>
          {count} result{count !== 1 ? "s" : ""}
        </span>
      )}
    </div>
  );
}

// ── List files ──────────────────────────────────────────────────────

export function ListFilesBlock({ tc }: { tc: ToolCallInfo }) {
  let path = "";
  let pattern = "";
  try {
    const args = JSON.parse(tc.args);
    path = args.path ?? "";
    pattern = args.pattern ?? "";
  } catch { /* ignore */ }

  const count = parseResultCount(tc.result);

  return (
    <div>
      <div className="flex items-center gap-1.5 text-sm ">
        <span className="font-semibold text-foreground">List</span>
        {path && <span className="text-muted-foreground truncate">{displayPath(path)}</span>}
        {pattern && pattern !== "*" && (
          <span className="text-muted-foreground/60 truncate">&quot;{pattern}&quot;</span>
        )}
      </div>
      {count !== null && tc.status === "complete" && (
        <span className={cn(
          "text-xs  shrink-0",
          count === 0 ? "text-muted-foreground/50" : "text-muted-foreground",
        )}>
          {count} file{count !== 1 ? "s" : ""}
        </span>
      )}
    </div>
  );
}
